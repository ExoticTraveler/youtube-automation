import importlib.util
import os
import tempfile
import unittest
from pathlib import Path

spec = importlib.util.spec_from_file_location("vad_batch", Path(__file__).resolve().parents[1] / "tools" / "vad_batch.py")
vad = importlib.util.module_from_spec(spec)
spec.loader.exec_module(vad)


class Discovery(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.raw = self.root / "AFG"
        self.out = self.root / "outputs"
        self.unit = self.raw / "Herat" / "INT" / "INT-1"
        self.unit.mkdir(parents=True)
        (self.unit / "clip.mov").write_bytes(b"fake")

    def test_mapping_and_skip(self):
        jobs = vad.discover(self.raw, self.out)
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0][1], (self.out / "AFG/Herat/INT/INT-1_CLEAN_VAD.fcpxmld").resolve())
        jobs[0][1].mkdir(parents=True)
        self.assertEqual(vad.discover(self.raw, self.out)[0][2], "SKIP_EXISTING")

    def test_output_inside_raw_refused(self):
        with self.assertRaises(ValueError):
            vad.discover(self.raw, self.raw / "output")

    def test_nested_same_id_has_distinct_output(self):
        other = self.raw / "Kabul" / "INT" / "INT-1"
        other.mkdir(parents=True)
        (other / "clip.mov").write_bytes(b"fake")
        self.assertEqual(len({j[1] for j in vad.discover(self.raw, self.out)}), 2)

    def test_symlink_unit_not_followed(self):
        link = self.raw / "Kabul" / "INT" / "INT-1"
        link.parent.mkdir(parents=True)
        link.symlink_to(self.unit, target_is_directory=True)
        self.assertEqual(len(vad.discover(self.raw, self.out)), 1)


if __name__ == "__main__":
    unittest.main()
