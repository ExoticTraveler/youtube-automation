import contextlib
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools'))
import metadata as m
import final_subtitle_autogen as f

DATA = dict(title='A Walk Through Town', title_alternatives=['Meeting Local People', 'Life Along the Street', 'An Afternoon on Foot', 'Conversations in Town'], description='A walk through town.', tags=['#Travel', '#Documentary', '#SoloTravel'])
SRT = '1\n00:00:00,000 --> 00:00:02,000\nWe walk through town.\n'

class MetadataTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.project = self.base / 'done' / 'NEXT'
        (self.project / 'subs').mkdir(parents=True)
        (self.project / 'youtube_video_id.txt').write_text('abcdefghijk')
        self.notes = self.base / 'notes'
        self.notes.mkdir()
        for name in m.EDITORIAL_FILES:
            (self.notes / name).write_text('Editorial framing only: ' + name)
        self.dest = self.project
        for name, value in [('BASE', self.base), ('EDITORIAL_DIR', self.notes)]:
            p = patch.object(m, name, value); p.start(); self.addCleanup(p.stop)
        self.api = patch.object(m, 'request_metadata', return_value=DATA).start()
        self.addCleanup(patch.stopall)
        patch.object(m.time, 'sleep').start()

    def final(self):
        (self.project / 'subs/final.srt').write_text(SRT)
        (self.project / 'subs/youtube.srt').write_text(SRT)

    def run_process(self):
        return m.process(self.project, self.dest, run=True, key='test')

    def test_final_context_generation(self):
        for name in f.SOURCES:
            (self.project / 'subs' / name).write_text(SRT)
        with patch.object(f, 'log'):
            status = f.process(self.project / 'subs')
        with patch.object(f.subprocess, 'run') as run:
            f.trigger_metadata(self.project, status)
            self.assertIn('--run', run.call_args.args[0])
            self.assertIn(str(self.project), run.call_args.args[0])
        self.assertTrue(self.run_process().startswith('CREATED'))
        transcript, _, _, notes = self.api.call_args.args
        self.assertEqual(transcript, 'We walk through town.')
        self.assertEqual([n['file'] for n in notes], list(m.EDITORIAL_FILES))
        payload = json.loads(m.assemble_input(transcript, notes))
        self.assertEqual(payload['FACT SOURCE']['transcript'], transcript)
        self.assertEqual(len(payload['EDITORIAL CONTEXT']['notes']), 4)
        self.assertIn('sole factual source', m.PROMPT)
        self.assertIn('privacy: unlisted', (self.dest / 'meta.txt').read_text())
        self.assertTrue((self.project / 'meta.txt').exists())

    def test_pipeline_main_triggers_after_atomic_final(self):
        (self.base / 'logs').mkdir()
        for name in f.SOURCES:
            (self.project / 'subs' / name).write_text(SRT)
        def run(command, **kwargs):
            self.assertTrue((self.project / 'subs/final.srt').is_file())
            self.assertTrue(self.run_process().startswith('CREATED'))
        with patch.object(f, 'BASE', self.base), patch.object(f.subprocess, 'run', side_effect=run), patch.object(sys, 'argv', ['final_subtitle_autogen.py']):
            f.main()
        self.api.assert_called_once()

    def test_scan_direct_and_one_request(self):
        self.final()
        second = self.base / 'done/SECOND'
        (second / 'subs').mkdir(parents=True)
        (second / 'subs/final.srt').write_text(SRT)
        with patch.object(sys, 'argv', ['metadata.py', '--scan', '--run']), patch.object(m, 'paths_lock', return_value=contextlib.nullcontext()), patch.object(m, 'api_key', return_value='test'), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(m.main(), 0)
        self.api.assert_called_once()
        self.assertTrue((self.dest / 'meta.txt').is_file())
        self.assertTrue((self.project / 'meta.txt').exists())
        self.assertFalse((second / 'meta.txt').exists())

    def test_missing_note_fallback(self):
        self.final()
        (self.notes / m.EDITORIAL_FILES[0]).unlink()
        with contextlib.redirect_stderr(io.StringIO()) as err:
            self.run_process()
        self.assertIn('WARNING editorial note unavailable', err.getvalue())
        self.assertEqual(len(self.api.call_args.args[3]), 3)

    def test_all_notes_missing_fallback(self):
        self.final()
        for path in self.notes.iterdir(): path.unlink()
        with contextlib.redirect_stderr(io.StringIO()): self.run_process()
        self.assertEqual(self.api.call_args.args[3], [])

    def test_existing_approved_skip_even_without_final(self):
        (self.project / 'meta.txt').write_text('manual override')
        self.assertTrue(self.run_process().startswith('SKIP'))
        self.api.assert_not_called()
        self.assertEqual((self.project / 'meta.txt').read_text(), 'manual override')
        self.assertFalse((self.base / 'meta_review_NEXT').exists())

    def test_existing_project_skip(self):
        self.dest.mkdir(exist_ok=True); (self.dest / 'meta.txt').write_text('review')
        self.assertTrue(self.run_process().startswith('SKIP'))
        self.api.assert_not_called()
        self.assertEqual((self.dest / 'meta.txt').read_text(), 'review')

    def test_no_final_no_generation(self):
        self.assertEqual(self.run_process(), 'WAIT final.srt')
        self.api.assert_not_called()
        with patch.object(f.subprocess, 'run') as run:
            for status in ['FINAL WAITING', 'FINAL FAILED ValueError: invalid']:
                f.trigger_metadata(self.project, status)
            run.assert_not_called()

    def test_invalid_final_no_generation(self):
        self.final(); (self.project / 'subs/final.srt').write_text('broken')
        with self.assertRaises(ValueError): self.run_process()
        self.api.assert_not_called()

    def test_timing_mismatch_no_generation(self):
        self.final()
        (self.project / 'subs/final.srt').write_text(SRT.replace('02,000', '03,000'))
        with self.assertRaisesRegex(ValueError, 'timing mismatch'): self.run_process()
        self.api.assert_not_called()

    def test_source_changes_no_publication(self):
        self.final()
        def change(*args):
            (self.project / 'subs/final.srt').write_text(SRT.replace('town', 'village'))
            return DATA
        self.api.side_effect = change
        with self.assertRaisesRegex(ValueError, 'changed'): self.run_process()
        self.assertFalse((self.project / 'meta.txt').exists())

    def test_unstable_source_no_api(self):
        self.final()
        def change(*args):
            (self.project / 'subs/final.srt').write_text(SRT.replace('town', 'village'))
        m.time.sleep.side_effect = change
        with self.assertRaisesRegex(ValueError, 'stability'): self.run_process()
        self.api.assert_not_called()

    def test_manual_metadata_appears_during_request(self):
        self.final()
        def change(*args):
            (self.project / 'meta.txt').write_text('approved')
            return DATA
        self.api.side_effect = change
        self.assertTrue(self.run_process().startswith('SKIP'))
        self.assertFalse((self.base / 'meta_review_NEXT').exists())

    def test_resolution_independent_of_cwd(self):
        self.assertEqual(m.resolve_project('NEXT'), self.project)
        self.assertEqual(m.resolve_project('done/NEXT'), self.project)
        self.assertEqual(m.resolve_project(self.project), self.project)
        (self.base / 'prepared/NEXT').mkdir(parents=True)
        with self.assertRaisesRegex(ValueError, 'found 2'): m.resolve_project('NEXT')
        with self.assertRaisesRegex(ValueError, 'found 0'): m.resolve_project('MISSING')

    def test_title_and_tag_guards(self):
        with self.assertRaisesRegex(ValueError, 'distinct'):
            m.render(dict(DATA, title_alternatives=[DATA['title']] * 4))
        with self.assertRaisesRegex(ValueError, 'hashtag'):
            m.render(dict(DATA, tags=['#Travel', '#bad tag', '#Film']))

    def test_cli_default_direct_and_lazy_key(self):
        self.final()
        with patch.object(sys, 'argv', ['metadata.py', '--project', 'NEXT', '--dry-run']), contextlib.redirect_stdout(io.StringIO()) as out, patch.object(m, 'api_key') as key:
            self.assertEqual(m.main(), 0)
        self.assertIn(str(self.dest / 'meta.txt'), out.getvalue())
        key.assert_not_called(); self.api.assert_not_called()
        self.assertFalse((self.base / 'meta_review_NEXT').exists())

    def test_exclusive_publish(self):
        self.dest.mkdir(exist_ok=True); out = self.dest / 'meta.txt'; out.write_text('manual')
        with self.assertRaises(FileExistsError): m.publish_new(out, 'new')
        self.assertEqual(out.read_text(), 'manual')

    def test_cli_failure_observable(self):
        self.final(); (self.project / 'subs/final.srt').write_text('broken')
        with patch.object(sys, 'argv', ['metadata.py', '--project', 'NEXT']), contextlib.redirect_stdout(io.StringIO()) as out, contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertEqual(m.main(), 1)
        self.assertIn('Invalid SRT cue structure', out.getvalue())
        self.assertIn('Traceback', err.getvalue())

if __name__ == '__main__': unittest.main()
