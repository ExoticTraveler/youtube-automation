"""Thumbnail generation is an OPTIONAL MANUAL tool: nothing in the normal pipeline may start it."""
import contextlib
import hashlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'tools'))
import thumbnail_auto as ta
import thumbnail_production as tp
import yt_autowatch as w

PIPELINE_FILES = ['yt_autowatch.py', 'yt_autoupload.py', 'yt_check_auth.py', 'prepare.py',
                  'tools/final_subtitle_autogen.py', 'tools/metadata.py', 'tools/publishing.py', 'tools/sub_autogen.py',
                  'tools/youtube_caption_fetch.py', 'tools/pipeline_state.py', 'tools/capture_narration.py',
                  'tools/make_hybrid_srt.py', 'tools/clean_srt.py', 'tools/yt_status.py', 'tools/upload_throttle.py']
FORBIDDEN = ('thumbnail_auto', 'thumbnail.py', 'thumbnail_production', 'thumbnail_compose', 'gpt-image', 'images/edits')
SRT = '1\n00:00:00,000 --> 00:00:02,000\nWe walk through town.\n'
META = 'title:\nA Walk\n\ndescription:\nA walk through town.\n\nprivacy: unlisted\n'


class NoAutomaticThumbnailTests(unittest.TestCase):
    def test_no_pipeline_module_references_thumbnail_generation(self):
        for name in PIPELINE_FILES:
            path = ROOT / name
            if not path.exists():
                continue
            text = path.read_text()
            for needle in FORBIDDEN:
                self.assertNotIn(needle, text, f'{name} must not reference {needle}')

    def test_no_launchd_job_or_cron_runs_thumbnail_generation(self):
        agents = Path.home() / 'Library/LaunchAgents'
        if not agents.is_dir():
            self.skipTest('no LaunchAgents directory')
        for plist in agents.glob('com.exotictraveler.*.plist'):
            self.assertNotIn('thumbnail', plist.read_text().lower(), plist.name)

    def test_watcher_scan_with_fully_valid_project_does_not_generate_thumbnails(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            project = base / 'done' / 'P'
            (project / 'subs').mkdir(parents=True)
            (project / 'subs/final.srt').write_text(SRT)
            (project / 'meta.txt').write_text(META)
            (project / 'youtube_video_id.txt').write_text('abcdefghijk')
            (project / 'video.mov').write_bytes(b'x')
            for name in ('tools',):
                (base / name).mkdir()
            for f in ('metadata.py', 'publishing.py', 'thumbnail_auto.py'):   # even if the old runner file exists
                (base / 'tools' / f).touch()
            (base / 'prepared').mkdir(); (base / 'failed').mkdir(); (base / 'logs').mkdir()
            before = {str(p): p.read_bytes() for p in project.rglob('*') if p.is_file()}
            with patch.object(w, 'BASE', base), patch.object(sys, 'argv', ['yt_autowatch.py']), \
                 patch.object(w, 'paths_lock', return_value=contextlib.nullcontext()), \
                 patch.object(w.subprocess, 'Popen') as spawn, patch.object(w.subprocess, 'run') as run, \
                 patch.object(ta, 'run_job', side_effect=AssertionError('thumbnail job started')), \
                 patch.object(tp, 'run_production', side_effect=AssertionError('thumbnail production started')), \
                 patch.object(tp.urllib.request, 'urlopen', side_effect=AssertionError('paid API call')):
                w.main()
                w.main()   # repeated scans
            started = [c.args[0] for c in spawn.call_args_list] + [c.args[0] for c in run.call_args_list]
            self.assertFalse(any('thumbnail' in ' '.join(map(str, c)).lower() for c in started))
            after = {str(p): p.read_bytes() for p in project.rglob('*') if p.is_file()}
            self.assertEqual(before, after)                         # nothing written into the project
            self.assertFalse((project / 'thumbnail').exists())
            self.assertFalse((base / '.publishing_updates_enabled').exists())   # publishing not enabled/changed

    def test_batch_mode_is_disabled_without_the_optin_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            project = base / 'done' / 'P'
            (project / 'subs').mkdir(parents=True)
            (project / 'subs/final.srt').write_text(SRT)
            (project / 'meta.txt').write_text(META)
            (project / 'youtube_video_id.txt').write_text('abcdefghijk')
            for n in ('subs/final.srt', 'meta.txt', 'youtube_video_id.txt'):
                os.utime(project / n, (1, 1))
            with patch.object(ta, 'BASE', base), patch.object(ta, 'default_runner', side_effect=AssertionError('started')), \
                 patch.object(sys, 'argv', ['thumbnail_auto.py', '--scan', '--run']), patch.object(ta, 'log'):
                self.assertEqual(ta.main(), 0)
            with patch.object(ta, 'BASE', base):
                self.assertEqual(ta.evaluate(project)[0], 'SKIP')
                self.assertIn('disabled', ta.evaluate(project)[1])
            self.assertTrue((ROOT / '.thumbnail_auto_since').exists() is False)   # the real opt-in file is off


class ManualToolStillWorksTests(unittest.TestCase):
    def make(self, tmp):
        base = Path(tmp)
        project = base / 'done' / 'P'
        (project / 'subs').mkdir(parents=True)
        (project / 'subs/final.srt').write_text(SRT)
        (project / 'meta.txt').write_text(META)
        (project / 'youtube_video_id.txt').write_text('abcdefghijk')
        for n in ('subs/final.srt', 'meta.txt', 'youtube_video_id.txt'):
            os.utime(project / n, (1, 1))     # ancient: settled, and older than any cut-off
        return base, project

    def test_explicit_manual_project_run_works_without_the_batch_optin(self):
        with tempfile.TemporaryDirectory() as tmp:
            base, project = self.make(tmp)
            calls = []
            def runner(command, log_path, timeout):
                calls.append(command)
                Path(log_path).write_text('ok')
                out = project / 'thumbnail/production'; out.mkdir(parents=True); (out / 'manifest.json').write_text('{}')
                return 0
            with patch.object(ta, 'BASE', base), patch.object(ta, 'preflight', return_value=[]), patch.object(ta, 'log'):
                self.assertEqual(ta.evaluate(project, manual=True)[0], 'READY')
                self.assertTrue(ta.run_job(project, runner, manual=True).startswith('COMPLETE'))
            self.assertEqual(len(calls), 1)
            self.assertEqual(calls[0][-2:], [str(project), '--run'])

    def test_manual_run_keeps_the_billing_and_completed_set_protections(self):
        with tempfile.TemporaryDirectory() as tmp:
            base, project = self.make(tmp)
            (project / 'thumbnail/production').mkdir(parents=True)
            (project / 'thumbnail/production/manifest.json').write_text('{"old": 1}')
            with patch.object(ta, 'BASE', base):
                self.assertEqual(ta.evaluate(project, manual=True)[0], 'SKIP')       # completed set never regenerated
            (project / 'thumbnail/production/manifest.json').unlink()
            (project / '.thumbnail_state.json').write_text(json.dumps(
                {'status': 'FAILED', 'attempts': 1, 'retry_after': 0, 'log_tail': 'HTTP 429 credit_balance_exhausted'}))
            with patch.object(ta, 'BASE', base):
                self.assertEqual(ta.evaluate(project, manual=True)[0], 'REVIEW')     # billing stop still applies

    def test_manual_resolve_accepts_name_and_queue_relative_path(self):
        with patch.object(ta, 'BASE', Path('/q')):
            self.assertEqual(ta.resolve('IRAN1R'), Path('/q/done/IRAN1R'))
            self.assertEqual(ta.resolve('done/IRAN1R'), Path('/q/done/IRAN1R'))
            self.assertEqual(ta.resolve('/x/y'), Path('/x/y'))

    def test_existing_thumbnail_cli_dry_run_still_works_and_calls_no_api(self):
        venv = ROOT / 'tools/.thumbnail_venv/bin/python'
        if not venv.exists():
            self.skipTest('thumbnail runtime not installed')
        with tempfile.TemporaryDirectory() as tmp:
            base, project = self.make(tmp)
            (project / 'video.mov').write_bytes(b'x')
            result = subprocess.run([str(venv), str(ROOT / 'tools/thumbnail.py'), str(project), '--dry-run'],
                                    capture_output=True, text=True, env={**os.environ, 'OPENAI_API_KEY': ''}, timeout=120)
        self.assertEqual(result.returncode, 0, result.stderr[-500:])
        report = json.loads(result.stdout[result.stdout.index('{'):])
        self.assertEqual(report['status'], 'READY')
        self.assertEqual(report['external_calls_if_run']['image_edits'], 4)    # four_concept_v1 intact


class ExistingOutputsUntouchedTests(unittest.TestCase):
    def test_real_iran1r_outputs_unchanged_by_status_checks(self):
        project = ROOT / 'done/IRAN1R'
        if not (project / 'thumbnail/production/manifest.json').is_file():
            self.skipTest('IRAN1R outputs not present')
        def digest():
            return hashlib.sha256(b''.join(sorted((str(p) + hashlib.sha256(p.read_bytes()).hexdigest()).encode()
                                  for p in (project / 'thumbnail').rglob('*') if p.is_file()))).hexdigest()
        before = digest()
        with contextlib.redirect_stdout(io.StringIO()):
            ta.evaluate(project, manual=True)
        self.assertEqual(digest(), before)


if __name__ == '__main__':
    unittest.main()
