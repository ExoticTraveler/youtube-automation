from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools'))
import capture_narration as capture


class CaptureNarrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.assets = Path(self.tmp.name) / 'Narration Sources'
        self.content = b'# Final narration\n\nKeep  spaces, punctuation -- and final newline.\n'

    def test_exact_bytes_and_project_mapping_are_preserved(self):
        status, path = capture.save_narration('TEST_42', self.content, self.assets)
        self.assertEqual(status, 'CREATED')
        self.assertEqual(path, self.assets / 'TEST_42.md')
        self.assertEqual(path.read_bytes(), self.content)

    def test_identical_duplicate_is_safe(self):
        capture.save_narration('TEST42', self.content, self.assets)
        status, path = capture.save_narration('TEST42', self.content, self.assets)
        self.assertEqual((status, path.read_bytes()), ('ALREADY EXISTS', self.content))

    def test_different_duplicate_is_a_conflict_without_overwrite(self):
        capture.save_narration('TEST42', self.content, self.assets)
        with self.assertRaisesRegex(capture.NarrationConflictError, 'different content'):
            capture.save_narration('TEST42', b'different', self.assets)
        self.assertEqual((self.assets / 'TEST42.md').read_bytes(), self.content)

    def test_invalid_project_ids_are_rejected(self):
        for project_id in ('', '../IRAN3', 'IRAN 3', '.IRAN3', 'IRAN3.md', 'x' * 65):
            with self.subTest(project_id=project_id):
                with self.assertRaises(ValueError):
                    capture.save_narration(project_id, self.content, self.assets)

    def test_atomic_publication_leaves_no_partial_target_or_temp_file(self):
        with patch.object(capture.os, 'link', side_effect=OSError('simulated link failure')):
            with self.assertRaisesRegex(OSError, 'simulated link failure'):
                capture.save_narration('TEST42', self.content, self.assets)
        self.assertFalse((self.assets / 'TEST42.md').exists())
        self.assertEqual(list(self.assets.glob('.TEST42.*.tmp')), [])


if __name__ == '__main__':
    unittest.main()
