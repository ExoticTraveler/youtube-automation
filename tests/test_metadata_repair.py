import contextlib
import io
import json
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools'))
import metadata as m
import final_subtitle_autogen as f
import youtube_caption_fetch as caption

SRT = '1\n00:00:00,000 --> 00:00:02,000\nWe walk through town.\n'
GOOD = dict(title='A Walk Through Town',
            title_alternatives=['Meeting Local People', 'Life Along the Street', 'An Afternoon on Foot', 'Conversations in Town'],
            description='A walk through town.', tags=['#Travel', '#Documentary', '#Walking'])


def bad(**changes):
    data = json.loads(json.dumps(GOOD))
    data.update(changes)
    return data


class RepairLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.project = self.base / 'done' / 'P'
        (self.project / 'subs').mkdir(parents=True)
        (self.project / 'subs/final.srt').write_text(SRT)
        (self.project / 'youtube_video_id.txt').write_text('abcdefghijk')
        notes = self.base / 'notes'; notes.mkdir()
        for name, value in (('BASE', self.base), ('EDITORIAL_DIR', notes)):
            p = patch.object(m, name, value); p.start(); self.addCleanup(p.stop)
        patch.object(m.time, 'sleep').start()
        self.addCleanup(patch.stopall)

    def run_process(self, side_effect):
        self.api = patch.object(m, 'request_metadata', side_effect=side_effect).start()
        with contextlib.redirect_stdout(io.StringIO()):
            return m.process(self.project, self.project, run=True, key='k')

    # --- validator messages / fixed content --------------------------------
    def test_fixed_links_and_intro_are_not_validated_and_stay_exact(self):
        text = m.render(GOOD)
        self.assertIn(m.INTRO, text)
        self.assertIn(m.LINKS, text)
        self.assertTrue(text.index(m.INTRO) < text.index(m.LINKS))

    def test_errors_name_field_and_rule(self):
        for field, data, rule in [
            ('description', bad(description='Subscribe https://x.example'), 'http(s)://'),
            ('description', bad(description='see www.x.example'), 'http(s)://'),
            ('description', bad(description='a -> b'), '< or >'),
            ('description', bad(description='<b>x</b>'), '< or >'),
            ('tags[1]', bad(tags=['#A', 'x>y', '#B']), '< or >'),
            ('title_alternatives[1]', bad(title_alternatives=['One two', 'Three <b>', 'Four five', 'Six seven']), 'Title links/markup'),
        ]:
            with self.subTest(field=field, rule=rule):
                with self.assertRaises(m.ValidationError) as ctx:
                    m.render(data)
                self.assertEqual(ctx.exception.field, field)
                self.assertIn(rule, ctx.exception.rule)
                self.assertIn(field, str(ctx.exception))

    # --- lifecycle ----------------------------------------------------------
    def test_initial_pass_makes_one_call_no_artifacts(self):
        result = self.run_process([GOOD])
        self.assertTrue(result.startswith('CREATED'))
        self.assertEqual(self.api.call_count, 1)
        self.assertFalse((self.base / 'logs/metadata_rejected').exists())

    def test_rejection_then_repair_pass(self):
        rejected = bad(description='Subscribe: https://example.com now')
        repaired = bad(description='A walk through town.')
        result = self.run_process([rejected, repaired])
        self.assertTrue(result.startswith('CREATED'))
        self.assertEqual(self.api.call_count, 2)
        kwargs = self.api.call_args_list[1].kwargs
        data, error = kwargs['repair']
        self.assertEqual(data, rejected)
        self.assertEqual((error.field, 'http(s)://' in error.rule), ('description', True))
        meta = (self.project / 'meta.txt').read_text()
        self.assertNotIn('example.com', meta)
        self.assertIn(m.LINKS, meta)
        self.assertFalse((self.project / '.metadata_needs_review').exists())
        files = list((self.base / 'logs/metadata_rejected').glob('P_*_initial.json'))
        self.assertEqual(len(files), 1)
        record = json.loads(files[0].read_text())
        self.assertEqual((record['project'], record['field'], record['stage']), ('P', 'description', 'initial'))
        self.assertIn('example.com', record['rejected_value'])
        self.assertIn('rejected_output', record); self.assertIn('timestamp', record)
        self.assertEqual(stat.S_IMODE(files[0].stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(files[0].parent.stat().st_mode), 0o700)
        self.assertFalse(any(self.project.rglob('*metadata_rejected*')))  # nothing publishable-side

    def test_repair_fail_creates_review_marker_and_stops(self):
        first = bad(description='go to https://a.example')
        second = bad(description='still www.b.example')
        result = self.run_process([first, second])
        self.assertTrue(result.startswith('NEEDS REVIEW'))
        self.assertEqual(self.api.call_count, 2)           # exactly ONE repair call
        self.assertFalse((self.project / 'meta.txt').exists())
        self.assertTrue((self.project / '.metadata_needs_review').is_file())
        stages = sorted(p.name.rsplit('_', 1)[1] for p in (self.base / 'logs/metadata_rejected').glob('P_*'))
        self.assertEqual(stages, ['initial.json', 'repair.json'])
        # later runs make no API calls
        with patch.object(m, 'request_metadata') as api, contextlib.redirect_stdout(io.StringIO()):
            self.assertTrue(m.process(self.project, self.project, run=True, key='k').startswith('SKIP needs review'))
        api.assert_not_called()

    def test_review_marker_blocks_scan_without_touching_retry_file(self):
        (self.project / '.metadata_needs_review').write_text('x')
        with patch.object(m, 'request_metadata') as api, patch.object(m, 'api_key', return_value='k'), \
             patch.object(m, 'paths_lock', return_value=contextlib.nullcontext()), \
             patch.object(sys, 'argv', ['metadata.py', '--scan', '--run']), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(m.main(), 0)
        api.assert_not_called()
        self.assertFalse((self.project / '.metadata_retry_after').exists())

    def test_existing_meta_never_overwritten_by_repair(self):
        def appear(*args, **kwargs):
            if kwargs.get('repair'):
                (self.project / 'meta.txt').write_text('manual')
                return GOOD
            return bad(description='http://x.example')
        result = self.run_process(appear)
        self.assertTrue(result.startswith('SKIP'))
        self.assertEqual((self.project / 'meta.txt').read_text(), 'manual')

    # --- retry classes -------------------------------------------------------
    def test_transient_failure_keeps_one_hour_backoff_and_no_review(self):
        argv = ['metadata.py', '--scan', '--run']
        with patch.object(m, 'request_metadata', side_effect=RuntimeError('OpenAI HTTP 503; response body omitted')) as api, \
             patch.object(m, 'api_key', return_value='k'), patch.object(m, 'paths_lock', return_value=contextlib.nullcontext()), \
             patch.object(sys, 'argv', argv), contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(m.main(), 1)
            self.assertEqual(m.main(), 0)   # inside the 1 h window: skipped
        self.assertEqual(api.call_count, 1)
        self.assertFalse((self.project / '.metadata_needs_review').exists())
        self.assertGreater(float((self.project / '.metadata_retry_after').read_text()), __import__('time').time() + 3000)


class BoilerplateRuleTests(unittest.TestCase):
    BASE_DESC = 'I cross the border into the country and walk through the market.'

    def desc(self, extra):
        return bad(description=self.BASE_DESC + ' ' + extra)

    def assert_refused(self, extra, fragment):
        with self.assertRaises(m.ValidationError) as ctx:
            m.render(self.desc(extra))
        self.assertEqual(ctx.exception.field, 'description')
        self.assertIn('Pipeline-owned boilerplate refused', ctx.exception.rule)
        self.assertIn(fragment, ctx.exception.rule)

    def test_exact_fixed_biography_refused(self):
        self.assert_refused(m.INTRO, 'channel introduction')

    def test_lightly_paraphrased_biography_refused(self):
        self.assert_refused('As a solo traveler exploring the world’s least visited countries, my goal is to show the authentic side '
                            'of the world: the people, the challenges and the atmosphere nobody talks about.', 'channel introduction')

    def test_fixed_cta_refused(self):
        self.assert_refused('🎥 Subscribe for more untold journeys', 'call to action')
        self.assert_refused('Subscribe for more untold journeys on YouTube, follow on Instagram, or support via Patreon.', 'call to action')

    def test_cta_naming_platform_or_opening_with_subscribe_refused(self):
        self.assert_refused('Follow me on Instagram for daily updates.', 'call to action')
        self.assert_refused('Please support me on Patreon if you enjoy this.', 'call to action')
        self.assert_refused('Subscribe and come along.', 'Subscribe')

    def test_model_generated_social_links_and_handles_refused(self):
        for extra in ('Find me at instagram.com/someone.', 'Visit patreon.com/someone.', 'Say hi @exotic_traveler.'):
            with self.subTest(extra=extra), self.assertRaises(m.ValidationError) as ctx:
                m.render(self.desc(extra))
            self.assertIn('link or handle', ctx.exception.rule)
        with self.assertRaises(m.ValidationError) as ctx:
            m.render(self.desc('See https://www.instagram.com/x'))
        self.assertIn('http(s):// or www.', ctx.exception.rule)   # existing URL protection kept

    def test_legitimate_episode_prose_passes(self):
        for extra in (
            'Instagram is blocked in the country, so most locals reach it through a VPN.',
            'My host asked me to follow him on a walk to the old fortress.',
            'A shopkeeper told me to subscribe to a local newspaper to learn the language.',
            'I upload the footage to YouTube whenever I find a stable connection.',
            'Patreon supporters of the channel helped me afford this trip.',
            'The tour guide ran a popular Patreon-style crowdfunding page for the village school.',
        ):
            with self.subTest(extra=extra):
                text = m.render(self.desc(extra))
                self.assertIn(m.LINKS, text)
                self.assertEqual(text.count(m.INTRO), 1)   # only the pipeline's own copy

    def test_boilerplate_not_applied_to_titles_or_tags_text(self):
        data = bad(title='Subscribe to Silk: Carpets of Tabriz')
        self.assertIn('Subscribe to Silk', m.render(data))


class BoilerplateLifecycleTests(RepairLifecycleTests):
    """Same fixtures; boilerplate rejections go through the one-repair-then-review lifecycle."""

    def test_boilerplate_then_repair_success(self):
        dup = bad(description='A walk through town. ' + m.INTRO + '\n\nSubscribe for more untold journeys.')
        result = self.run_process([dup, GOOD])
        self.assertTrue(result.startswith('CREATED'))
        self.assertEqual(self.api.call_count, 2)
        self.assertIn('boilerplate', self.api.call_args_list[1].kwargs['repair'][1].rule.lower())
        self.assertEqual((self.project / 'meta.txt').read_text().count(m.INTRO), 1)
        self.assertFalse((self.project / '.metadata_needs_review').exists())

    def test_boilerplate_repair_failure_goes_to_review_without_hourly_retry(self):
        dup = bad(description='A walk. ' + m.INTRO)
        result = self.run_process([dup, dup])
        self.assertTrue(result.startswith('NEEDS REVIEW'))
        self.assertEqual(self.api.call_count, 2)
        self.assertTrue((self.project / '.metadata_needs_review').exists())
        self.assertFalse((self.project / 'meta.txt').exists())

    def test_prompts_state_boilerplate_is_pipeline_owned(self):
        for text in (m.PROMPT, m.REPAIR_PROMPT):
            self.assertRegex(text.lower(), r'calls? to action|call-to-action')
            self.assertIn('append', text.lower())


class CaptionQuietTests(unittest.TestCase):
    def test_valid_final_without_en_srt_is_silent_and_runs_nothing(self):
        with tempfile.TemporaryDirectory() as d:
            folder = Path(d) / 'done' / 'R'
            subs = folder / 'subs'; subs.mkdir(parents=True)
            (subs / 'final.srt').write_text(SRT)
            (subs / 'youtube.srt').write_text(SRT)
            with patch.object(caption, 'log') as log, patch.object(caption.subprocess, 'run') as run:
                caption.run_hybrid(folder)
            log.assert_not_called(); run.assert_not_called()

    def test_without_final_still_reports_waiting(self):
        with tempfile.TemporaryDirectory() as d:
            folder = Path(d) / 'done' / 'R'
            subs = folder / 'subs'; subs.mkdir(parents=True)
            (subs / 'youtube.srt').write_text(SRT)
            with patch.object(caption, 'log') as log:
                caption.run_hybrid(folder)
            self.assertIn('en.srt not ready', log.call_args[0][0])


if __name__ == '__main__':
    unittest.main()
