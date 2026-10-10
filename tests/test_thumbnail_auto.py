import hashlib
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools'))
import thumbnail_auto as t

SRT = '1\n00:00:00,000 --> 00:00:02,000\nWe walk through town.\n'
META = 'title:\nA Walk Through Town\n\ndescription:\nA walk through town.\n\nprivacy: unlisted\n\ntags:\n#Travel, #Documentary\n'
OLD = time.time() - 3600


class Fake:
    """Stand-in for tools/thumbnail.py --run: counts calls, optionally produces the package."""
    def __init__(self, project, outcome='ok', delay=0.0):
        self.project, self.outcome, self.delay, self.calls = project, outcome, delay, []

    def __call__(self, command, log_path, timeout):
        self.calls.append(command)
        Path(log_path).write_text('fake thumbnail run\nsome traceback line\n')
        time.sleep(self.delay)
        if self.outcome == 'ok':
            out = self.project / 'thumbnail/production'
            out.mkdir(parents=True)
            (out / 'manifest.json').write_text('{}')
            return 0
        if self.outcome == 'raise':
            raise TimeoutError('timed out')
        return 2


class AutoThumbnailTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name)
        patch.object(t, 'BASE', self.base).start()
        patch.object(t, 'log').start()
        self.addCleanup(patch.stopall)
        self.project = self.base / 'done' / 'P'
        (self.project / 'subs').mkdir(parents=True)
        (self.project / 'subs/final.srt').write_text(SRT)
        (self.project / 'meta.txt').write_text(META)
        (self.project / 'youtube_video_id.txt').write_text('abcdefghijk')
        for name in ('subs/final.srt', 'meta.txt', 'youtube_video_id.txt'):
            os.utime(self.project / name, (OLD, OLD))
        (self.base / '.thumbnail_auto_since').write_text(str(OLD - 86400))
        self.fake = Fake(self.project)
        patch.object(t, 'preflight', return_value=[]).start()

    def digest(self):
        return {n: hashlib.sha256((self.project / n).read_bytes()).hexdigest()
                for n in ('subs/final.srt', 'meta.txt', 'youtube_video_id.txt')}

    def run_job(self, runner=None):
        return t.run_job(self.project, runner or self.fake)

    # --- starts once ---------------------------------------------------------
    def test_valid_final_and_meta_without_thumbnails_starts_once(self):
        before = self.digest()
        self.assertEqual(t.evaluate(self.project)[0], 'READY')
        result = self.run_job()
        self.assertTrue(result.startswith('COMPLETE'))
        self.assertEqual(len(self.fake.calls), 1)
        command = self.fake.calls[0]
        self.assertTrue(command[2].endswith('tools/thumbnail.py'))   # the EXISTING CLI, nothing else
        self.assertEqual(command[3:], [str(self.project), '--run'])
        state = json.loads((self.project / '.thumbnail_state.json').read_text())
        self.assertEqual((state['status'], state['attempts']), ('COMPLETE', 1))
        self.assertEqual(self.digest(), before)                       # inputs untouched
        self.assertFalse((self.project / '.thumbnail_needs_review').exists())

    # --- gating --------------------------------------------------------------
    def test_missing_or_invalid_final_srt_does_not_start(self):
        (self.project / 'subs/final.srt').write_text('garbage')
        os.utime(self.project / 'subs/final.srt', (OLD, OLD))
        self.assertEqual(t.evaluate(self.project)[0], 'WAIT')
        self.assertNotIn('COMPLETE', self.run_job())
        (self.project / 'subs/final.srt').unlink()
        self.assertEqual(t.evaluate(self.project)[0], 'WAIT')
        self.assertEqual(self.fake.calls, [])

    def test_missing_or_invalid_meta_does_not_start(self):
        for text in ('', 'title:\n\ndescription:\n\n', 'title:\nBad <b>\n\ndescription:\nx\n'):
            (self.project / 'meta.txt').write_text(text)
            os.utime(self.project / 'meta.txt', (OLD, OLD))
            self.assertEqual(t.evaluate(self.project)[0], 'WAIT', text)
        (self.project / 'meta.txt').unlink()
        self.assertEqual(t.evaluate(self.project)[0], 'WAIT')
        self.run_job()
        self.assertEqual(self.fake.calls, [])

    def test_unsettled_inputs_wait(self):
        os.utime(self.project / 'meta.txt', None)   # just written
        self.assertEqual(t.evaluate(self.project)[0], 'WAIT')
        self.assertEqual(self.fake.calls, [])

    def test_not_verified_upload_or_legacy_or_not_done_do_not_start(self):
        (self.project / 'youtube_video_id.txt').write_text('short')
        self.assertEqual(t.evaluate(self.project)[0], 'SKIP')
        (self.project / 'youtube_video_id.txt').write_text('abcdefghijk')
        os.utime(self.project / 'youtube_video_id.txt', (OLD - 10 * 86400, OLD - 10 * 86400))
        self.assertIn('legacy', t.evaluate(self.project)[1])
        prepared = self.base / 'prepared' / 'Q'
        prepared.mkdir(parents=True)
        self.assertEqual(t.evaluate(prepared)[0], 'SKIP')
        (self.base / '.thumbnail_auto_since').unlink()
        os.utime(self.project / 'youtube_video_id.txt', (OLD, OLD))
        self.assertIn('disabled', t.evaluate(self.project)[1])
        self.assertEqual(self.fake.calls, [])

    # --- idempotency ----------------------------------------------------------
    def test_completed_candidate_set_is_not_regenerated(self):
        out = self.project / 'thumbnail/production'
        out.mkdir(parents=True)
        (out / 'manifest.json').write_text('{"keep": true}')
        self.assertEqual(t.evaluate(self.project)[0], 'SKIP')
        self.run_job()
        self.assertEqual(self.fake.calls, [])
        self.assertEqual((out / 'manifest.json').read_text(), '{"keep": true}')

    def test_human_supplied_thumbnail_is_respected(self):
        (self.project / 'Thumbnail - 27.PNG').write_bytes(b'x')
        self.assertEqual(t.evaluate(self.project)[0], 'SKIP')

    def test_repeated_scans_do_no_duplicate_work(self):
        for _ in range(5):
            self.run_job()
        self.assertEqual(len(self.fake.calls), 1)

    def test_main_scan_run_starts_once_and_dry_scan_never(self):
        with patch.object(t, 'default_runner', self.fake), patch.object(sys, 'argv', ['x', '--scan']):
            t.main()
        self.assertEqual(self.fake.calls, [])                    # read-only by default
        self.run_job()
        self.assertEqual(len(self.fake.calls), 1)

    # --- concurrency ----------------------------------------------------------
    def test_global_lock_blocks_second_job(self):
        import fcntl
        with (self.base / '.thumbnail_auto.lock').open('a') as held:
            fcntl.flock(held, fcntl.LOCK_EX)
            self.assertIn('another thumbnail job', self.run_job())
        self.assertEqual(self.fake.calls, [])

    def test_concurrent_scans_launch_exactly_one_job(self):
        slow = Fake(self.project, delay=0.4)
        results = []
        threads = [threading.Thread(target=lambda: results.append(self.run_job(slow))) for _ in range(6)]
        [th.start() for th in threads]
        [th.join() for th in threads]
        self.assertEqual(len(slow.calls), 1)
        self.assertEqual(sum(r.startswith('COMPLETE') for r in results), 1)

    # --- failure --------------------------------------------------------------
    def test_failure_is_safe_resumable_and_capped(self):
        before = self.digest()
        keep = self.project / 'thumbnail/production_old'
        keep.mkdir(parents=True)
        (keep / 'x.jpg').write_bytes(b'precious')
        bad = Fake(self.project, outcome='fail')
        first = self.run_job(bad)
        self.assertTrue(first.startswith('FAILED'))
        state = json.loads((self.project / '.thumbnail_state.json').read_text())
        self.assertEqual((state['status'], state['attempts']), ('FAILED', 1))
        self.assertIn('fake thumbnail run', state['log_tail'])
        self.assertTrue(Path(state['log']).is_file())
        self.assertGreater(state['retry_after'], time.time() + 3000)
        self.assertFalse((self.project / '.thumbnail_needs_review').exists())
        # inside the back-off window: nothing runs
        self.run_job(bad)
        self.assertEqual(len(bad.calls), 1)
        # after back-off: one more attempt (resume), then needs review and no more paid retries
        state['retry_after'] = time.time() - 1
        (self.project / '.thumbnail_state.json').write_text(json.dumps(state))
        second = self.run_job(bad)
        self.assertIn('needs review', second)
        self.assertTrue((self.project / '.thumbnail_needs_review').exists())
        self.run_job(bad)
        self.assertEqual(len(bad.calls), 2)
        self.assertEqual(self.digest(), before)
        self.assertEqual((keep / 'x.jpg').read_bytes(), b'precious')   # nothing deleted
        self.assertFalse((self.project / 'thumbnail/production').exists())

    def test_billing_failure_stops_paid_retries_and_reset_reenables(self):
        def broke(command, log_path, timeout):
            Path(log_path).write_text('ERROR: OpenAI image edit failed (HTTP 429 credit_balance_exhausted)\n')
            return 2
        runner = Fake(self.project)
        runner.__call__ = broke
        result = self.run_job(broke)
        self.assertIn('billing', result)
        self.assertTrue((self.project / '.thumbnail_needs_review').exists())
        self.assertEqual(t.evaluate(self.project)[0], 'REVIEW')
        # even after the back-off the paid pipeline is not re-run
        state = json.loads((self.project / '.thumbnail_state.json').read_text())
        state['retry_after'] = 0
        (self.project / '.thumbnail_state.json').write_text(json.dumps(state))
        self.run_job()
        self.assertEqual(self.fake.calls, [])
        # human adds credits and resets: state archived (not deleted), eligible again, runs once
        moved = t.reset(self.project)
        self.assertEqual(len(moved), 2)
        self.assertTrue(all(Path(m).exists() for m in moved))
        self.assertEqual(t.evaluate(self.project)[0], 'READY')
        self.assertTrue(self.run_job().startswith('COMPLETE'))
        self.assertEqual(len(self.fake.calls), 1)

    def test_existing_billing_failed_state_without_marker_is_review(self):
        (self.project / '.thumbnail_state.json').write_text(json.dumps({
            'status': 'FAILED', 'attempts': 1, 'retry_after': 0,
            'log_tail': 'ERROR: OpenAI image edit failed (HTTP 429 credit_balance_exhausted)'}))
        self.assertEqual(t.evaluate(self.project)[0], 'REVIEW')
        self.run_job()
        self.assertEqual(self.fake.calls, [])

    def test_exception_or_timeout_never_leaves_running_state(self):
        result = self.run_job(Fake(self.project, outcome='raise'))
        self.assertIn('TimeoutError', result)
        state = json.loads((self.project / '.thumbnail_state.json').read_text())
        self.assertEqual(state['status'], 'FAILED')

    def test_success_exit_without_manifest_counts_as_failure(self):
        def liar(command, log_path, timeout):
            Path(log_path).write_text('')
            return 0
        self.assertIn('no production manifest', self.run_job(liar))

    def test_missing_runtime_waits_without_consuming_an_attempt(self):
        with patch.object(t, 'preflight', return_value=['ffmpeg']):
            self.assertIn('runtime missing: ffmpeg', self.run_job())
        self.assertFalse((self.project / '.thumbnail_state.json').exists())
        self.assertEqual(self.fake.calls, [])

    def test_child_gets_homebrew_path_under_minimal_launchd_environment(self):
        captured = {}
        def fake_run(command, **kwargs):
            captured.update(kwargs)
            return type('R', (), {'returncode': 0})()
        with patch.dict(os.environ, {'PATH': '/usr/bin:/bin'}), patch.object(t, 'TOOL_PATH', '/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin'), \
             patch.object(t.subprocess, 'run', side_effect=fake_run):
            t.default_runner(['x'], self.base / 'child.log', 5)
        self.assertTrue(captured['env']['PATH'].startswith('/opt/homebrew/bin'))
        self.assertEqual(oct((self.base / 'child.log').stat().st_mode & 0o777), '0o600')

    def test_real_preflight_finds_ffmpeg_via_augmented_path(self):
        self.assertTrue(all(n not in ' '.join(t.preflight()) for n in ('ffmpeg', 'ffprobe')) or not __import__('shutil').which('ffmpeg', path='/opt/homebrew/bin'))

    def test_low_disk_waits_without_state_change(self):
        with patch.object(t.shutil, 'disk_usage', return_value=type('U', (), {'free': 10})()):
            self.assertIn('insufficient free disk', self.run_job())
        self.assertFalse((self.project / '.thumbnail_state.json').exists())
        self.assertEqual(self.fake.calls, [])


if __name__ == '__main__':
    unittest.main()
