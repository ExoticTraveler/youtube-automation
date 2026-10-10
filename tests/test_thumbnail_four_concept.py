import contextlib
import inspect
import io
import json
import sys
import tempfile
import types
import unittest
import urllib.error
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools'))
import thumbnail_compose as tc
import thumbnail_production as tp
import thumbnail_auto as ta

IDS = ['A', 'B', 'C', 'D']


def plan():
    return {'country_name': 'Iran', 'country_code': 'IR', 'episode_summary': 'x',
            'concepts': [{'id': i, 'proposition': f'prop {i}', 'evidence': f'ev {i}', 'attention_object': f'obj {i}',
                          'timestamp_ranges': [{'start_seconds': 0, 'end_seconds': 10, 'reason': 'r'}],
                          'editorial_prompt': 'p'} for i in IDS]}


def selection(extra=None):
    out = [{'id': i, 'ingredients': [{'index': n, 'ingredient': f'thing {i}', 'role': 'base'}],
            'composition_brief': f'brief {i}', 'attention_object': f'object {i}'} for n, i in enumerate(IDS)]
    return out if extra is None else extra


def response(concepts):
    return {'status': 'completed', 'usage': {}, 'output': [{'type': 'message', 'content': [
        {'type': 'output_text', 'text': json.dumps({'concepts': concepts})}]}]}


class FakeImage:
    class Resampling:
        LANCZOS = 1

    class _Img:
        def __init__(self, data=b''): self.data = data
        def convert(self, mode): return self
        def resize(self, size, resample): return self
        def save(self, path, **kwargs): Path(path).write_bytes(b'JPEG')

    @classmethod
    def open(cls, fh): return cls._Img(fh.read())


NUMPY = types.SimpleNamespace(asarray=lambda im: types.SimpleNamespace(std=lambda: 25.0))
FRAMES = [{'timestamp': f'00:00:{n:02d}.000', 'timestamp_seconds': float(n)} for n in range(6)]


class FourConceptTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)

    def compose(self, edit):
        def extract(ffmpeg, video, seconds, target): Path(target).write_bytes(b'frame')
        chosen = {s['id']: s for s in selection()}
        with patch.object(tc, 'edit_image', side_effect=edit) as paid, patch.object(tc, 'extract_source', side_effect=extract), \
             patch.dict(sys.modules, {'numpy': NUMPY}):
            records = tc.compose_backgrounds(plan(), chosen, FRAMES, Path('v.mov'), 'ffmpeg', self.tmp, (FakeImage,), 'k', 'gpt-image-2', 'high', (1280, 720))
        return records, paid

    # 1/2: plan and expected candidates ---------------------------------------
    def test_plan_has_exactly_four_concepts_a_b_c_d(self):
        schema = tp.PLAN_SCHEMA['properties']['concepts']
        self.assertEqual((schema['minItems'], schema['maxItems']), (4, 4))
        self.assertEqual(schema['items']['properties']['id']['enum'], IDS)
        tp.validate_plan(plan())
        for bad in (plan()['concepts'][:3], plan()['concepts'] + plan()['concepts'][:1]):
            with self.assertRaises(ValueError):
                tp.validate_plan({**plan(), 'concepts': bad})
        self.assertEqual(tp.CONCEPTS, tuple(IDS))

    def test_selection_schema_is_one_final_candidate_per_concept(self):
        item = tc.SELECT_SCHEMA['properties']['concepts']
        self.assertEqual((item['minItems'], item['maxItems']), (4, 4))
        self.assertNotIn('variants', item['items']['properties'])
        self.assertIn('attention_object', item['items']['required'])

    def test_prompts_describe_four_final_candidates_and_no_baselines(self):
        text = tc.SELECT_PROMPT.lower()
        self.assertIn('four final', text)
        self.assertIn('no baseline', text)
        self.assertNotIn('variant 1', text)
        self.assertFalse(hasattr(tc, 'EDITORIAL'))
        self.assertFalse(hasattr(tc, 'variant_kind'))
        self.assertIn('third-party', text)                       # B must not lean on news footage
        self.assertIn('one final paid thumbnail candidate', tp.PLAN_PROMPT.lower())

    # 3/4: paid-call mapping -----------------------------------------------------
    def test_normal_run_makes_exactly_four_paid_image_edits(self):
        records, paid = self.compose(lambda *a, **k: (b'png', {'usage': 1}))
        self.assertEqual(paid.call_count, 4)
        self.assertEqual([r['id'] for r in records], IDS)
        raws = sorted(p.name for p in (self.tmp / 'backgrounds_raw').iterdir())
        self.assertEqual(raws, ['A.jpg', 'B.jpg', 'C.jpg', 'D.jpg'])      # exactly four, no hidden extras
        self.assertEqual(len(list((self.tmp / 'sources').iterdir())), 4)
        self.assertTrue(all(r['usage'] for r in records))                  # every record is a paid final, none local baseline
        self.assertEqual({r['kind'] for r in records}, {'attention', 'high_attention'})
        self.assertEqual([r['kind'] for r in records if r['kind'] == 'high_attention'], ['high_attention'])
        self.assertEqual(records[3]['kind'], 'high_attention')

    def test_prompts_are_final_candidate_prompts_and_d_is_most_aggressive(self):
        records, paid = self.compose(lambda *a, **k: (b'png', {}))
        by_id = {r['id']: r['prompt'] for r in records}
        for i in 'ABC':
            self.assertIn('FINAL THUMBNAIL', by_id[i])
            self.assertNotIn('MAXIMUM-ATTENTION', by_id[i])
        self.assertIn('MAXIMUM-ATTENTION', by_id['D'])
        self.assertIn('absolutely no invented explosions', by_id['D'])
        self.assertTrue(all('captions' in p.lower() for p in by_id.values()))

    def test_legacy_three_variant_selection_is_rejected(self):
        legacy = [{'id': i, 'variants': [{'variant': n, 'ingredients': [], 'composition_brief': '', 'attention_object': 'x'} for n in (1, 2, 3)]} for i in IDS]
        with patch.object(tc, '_post_json', return_value=response(legacy)):
            with self.assertRaises((RuntimeError, KeyError)):
                tc.select_ingredients([], FRAMES, plan(), 'k', 'm')

    def test_selection_requires_all_concepts_and_attention_objects(self):
        with patch.object(tc, '_post_json', return_value=response(selection())):
            out, _ = tc.select_ingredients([], FRAMES, plan(), 'k', 'm')
        self.assertEqual(sorted(out), IDS)
        missing = selection(); missing[2]['attention_object'] = ' '
        with patch.object(tc, '_post_json', return_value=response(missing)), self.assertRaisesRegex(RuntimeError, 'attention_object'):
            tc.select_ingredients([], FRAMES, plan(), 'k', 'm')
        with patch.object(tc, '_post_json', return_value=response(selection()[:3])), self.assertRaisesRegex(RuntimeError, 'A/B/C/D'):
            tc.select_ingredients([], FRAMES, plan(), 'k', 'm')

    # 5: local figures -------------------------------------------------------------
    def test_figure_candidates_are_local_and_not_part_of_image_generation(self):
        source = inspect.getsource(tp.select_figures) + inspect.getsource(tp.creator_pool) + inspect.getsource(tp.clean_cutout)
        for needle in ('edit_image', 'urllib', 'api.openai', 'tc.'):
            self.assertNotIn(needle, source)
        records, paid = self.compose(lambda *a, **k: (b'png', {}))
        self.assertEqual(paid.call_count, 4)                      # unaffected by the five figures (produced elsewhere, locally)

    # 6/7: dry run, idempotency, old completed sets ----------------------------------
    def project(self, name='P'):
        folder = self.tmp / 'done' / name
        (folder / 'subs').mkdir(parents=True)
        (folder / 'video.mov').write_bytes(b'v')
        (folder / 'subs/final.srt').write_text('1\n00:00:00,000 --> 00:00:02,000\nHello.\n')
        (folder / 'meta.txt').write_text('title:\nT\n\ndescription:\nD\n')
        (folder / 'youtube_video_id.txt').write_text('abcdefghijk')
        return folder

    def args(self, folder, **kw):
        return SimpleNamespace(project=folder, output_root=None, dry_run=kw.get('dry_run', False), image_mode='api', country_code=None)

    def test_dry_run_reports_four_image_edits(self):
        folder = self.project()
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            tp.run_production(self.args(folder, dry_run=True), None)
        calls = json.loads(out.getvalue())['external_calls_if_run']
        self.assertEqual(calls['image_edits'], 4)
        self.assertEqual(calls['vision_ingredient_selection'], 1)

    def test_manifest_audit_counts_actual_paid_edits_not_a_constant(self):
        source = inspect.getsource(tp.run_production)
        self.assertNotIn('10 if', source)
        self.assertIn('image_edit_calls', source)
        self.assertIn('four_concept_v1', source)

    def test_completed_set_is_never_regenerated_even_if_old_twelve_candidate_architecture(self):
        folder = self.project()
        out = folder / 'thumbnail/production'
        (out / 'candidates').mkdir(parents=True)
        for n in ('A1', 'A2', 'A3', 'B1'):
            (out / 'candidates' / f'{n}.jpg').write_bytes(b'old')
        manifest = json.dumps({'concepts': [{'id': f'{c}{v}'} for c in IDS for v in (1, 2, 3)]})
        (out / 'manifest.json').write_text(manifest)
        with patch.object(tp, 'api_key', side_effect=AssertionError('API key must not be read')), \
             patch.object(tp, 'load_deps', side_effect=AssertionError('no work for a completed set')), contextlib.redirect_stdout(io.StringIO()) as o:
            self.assertEqual(tp.run_production(self.args(folder), None), 0)
        self.assertIn('SKIP existing', o.getvalue())
        self.assertEqual((out / 'manifest.json').read_text(), manifest)
        self.assertEqual(sorted(p.name for p in (out / 'candidates').iterdir()), ['A1.jpg', 'A2.jpg', 'A3.jpg', 'B1.jpg'])
        # the watcher trigger also treats it as complete and would not start a job
        with patch.object(ta, 'BASE', self.tmp), patch.object(ta, 'cutoff', return_value=0):
            self.assertIn('complete', ta.evaluate(folder)[1])

    # 8: billing/API failure -------------------------------------------------------
    def test_billing_error_surfaces_in_form_the_trigger_classifies_as_billing(self):
        body = io.BytesIO(json.dumps({'error': {'code': 'credit_balance_exhausted'}}).encode())
        error = urllib.error.HTTPError('https://api.openai.com/v1/images/edits', 429, 'Too Many', {}, body)
        with patch.object(tc.urllib.request, 'urlopen', side_effect=error), patch.object(tc.time, 'sleep'):
            with self.assertRaises(RuntimeError) as ctx:
                tc.edit_image('p', [Path(__file__)], 'k', 'gpt-image-2', 'high', attempts=1)
        self.assertIn('credit_balance_exhausted', str(ctx.exception))
        self.assertIn('no degraded output', str(ctx.exception))
        self.assertTrue(ta.BILLING.search(str(ctx.exception)))

    def test_failed_paid_call_aborts_the_run_without_degraded_candidates(self):
        def edit(prompt, *a, **k):
            if 'MAXIMUM-ATTENTION' in prompt:
                raise RuntimeError('OpenAI image edit failed (HTTP 429 credit_balance_exhausted); no degraded output produced')
            return b'png', {}
        with self.assertRaisesRegex(RuntimeError, 'credit_balance_exhausted'):
            self.compose(edit)


if __name__ == '__main__':
    unittest.main()
