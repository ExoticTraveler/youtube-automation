from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools'))
import sub_autogen as s

FINAL = "1\n00:00:01,000 --> 00:00:02,000\nHello there.\n"


class FinalSrtCompletesSubtitlesTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        base = Path(tmp.name)
        self.project = base / 'prepared' / 'P1'
        (self.project / 'subs').mkdir(parents=True)
        (self.project / 'video.mov').write_bytes(b'x')
        for name, value in (('WATCH_DIRS', [base / 'prepared', base / 'done']),
                            ('BASE', base)):
            p = patch.object(s, name, value)
            p.start()
            self.addCleanup(p.stop)
        for name in ('log', 'preflight'):
            p = patch.object(s, name)
            p.start()
            self.addCleanup(p.stop)

    def test_valid_final_without_en_skips_demucs_and_whisper(self):
        final = self.project / 'subs' / 'final.srt'
        final.write_text(FINAL, encoding='utf-8')
        with patch.object(s, 'run_demucs') as demucs, \
             patch.object(s, 'run_whisper') as whisper, \
             patch.object(s, 'stable', return_value=True):
            self.assertEqual(s.collect_targets(), [])
            s.process_all()
        demucs.assert_not_called()
        whisper.assert_not_called()
        self.assertTrue((self.project / '.sub_done').exists())
        self.assertFalse((self.project / '.sub_failed').exists())
        self.assertEqual(final.read_text(encoding='utf-8'), FINAL)
        self.assertFalse((self.project / 'subs' / 'en.srt').exists())

    def test_done_project_with_final_and_youtube_srt_but_no_en_is_not_a_target(self):
        done = s.BASE / 'done' / 'D1'
        (done / 'subs').mkdir(parents=True)
        (done / 'video.mov').write_bytes(b'x')
        (done / 'subs' / 'final.srt').write_text(FINAL, encoding='utf-8')
        (done / 'subs' / 'youtube.srt').write_text(FINAL, encoding='utf-8')
        with patch.object(s, 'stable', return_value=True):
            names = [f.name for f, _ in s.collect_targets()]
        self.assertNotIn('D1', names)
        self.assertEqual(names, ['P1'])  # the unrelated prepared project is still queued

    def test_missing_final_still_targets_project(self):
        with patch.object(s, 'stable', return_value=True):
            self.assertEqual([f.name for f, _ in s.collect_targets()], ['P1'])

    def test_invalid_final_does_not_count_as_complete(self):
        (self.project / 'subs' / 'final.srt').write_text('garbage', encoding='utf-8')
        with patch.object(s, 'stable', return_value=True):
            self.assertEqual([f.name for f, _ in s.collect_targets()], ['P1'])


if __name__ == '__main__':
    unittest.main()
