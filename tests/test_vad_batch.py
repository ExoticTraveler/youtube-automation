import importlib.util
import os
import tempfile
import time
from types import SimpleNamespace
from unittest.mock import patch
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
        self.assertEqual(vad.discover(self.raw, self.out)[0][2], "BLOCKED_UNVERIFIED")

    def test_output_inside_raw_refused(self):
        with self.assertRaises(ValueError):
            vad.discover(self.raw, self.raw / "output")

    def test_nested_same_id_has_distinct_output(self):
        other = self.raw / "Kabul" / "INT" / "INT-1"
        other.mkdir(parents=True)
        (other / "clip.mov").write_bytes(b"fake")
        self.assertEqual(len({j[1] for j in vad.discover(self.raw, self.out)}), 2)

    def _old_input(self):
        os.utime(self.unit / "clip.mov", (time.time() - 130, time.time() - 130))

    def _fake_editor(self, mutate=False):
        def run(cmd, check=False):
            out = Path(cmd[cmd.index("--output") + 1])
            out.mkdir(parents=True)
            (out / "Info.fcpxml").write_text("<fcpxml />")
            if mutate:
                (self.unit / "clip.mov").write_bytes(b"new material")
            return SimpleNamespace(returncode=0)
        return run

    def test_staging_manifest_replay_and_stale_output(self):
        self._old_input()
        dest = vad.discover(self.raw, self.out)[0][1]
        with patch.object(vad.subprocess, "run", side_effect=self._fake_editor()):
            state = vad.process_one(self.unit, dest, self.root / "editor.py", self.raw)
        self.assertEqual(state, "CREATED")
        self.assertTrue(vad.manifest_path(dest).is_file())
        self.assertEqual(vad.discover(self.raw, self.out)[0][2], "SKIP_VERIFIED")
        (dest / "Info.fcpxml").write_text("<changed />")
        self.assertEqual(vad.discover(self.raw, self.out)[0][2], "BLOCKED_UNVERIFIED")

    def test_input_changed_during_edit_never_published(self):
        self._old_input()
        dest = vad.discover(self.raw, self.out)[0][1]
        with patch.object(vad.subprocess, "run", side_effect=self._fake_editor(mutate=True)):
            state = vad.process_one(self.unit, dest, self.root / "editor.py", self.raw)
        self.assertEqual(state, "INPUT_CHANGED")
        self.assertFalse(dest.exists())
        self.assertTrue(vad.stage_dir(dest).is_dir())
        self.assertEqual(vad.discover(self.raw, self.out)[0][2], "BLOCKED_PARTIAL")

    def test_same_size_rewrites_change_sampled_signatures(self):
        self._old_input()
        source = self.unit / "clip.mov"
        old = source.stat()
        before = vad.snapshot(self.unit)
        source.write_bytes(b"FAKE")
        os.utime(source, ns=(old.st_atime_ns, old.st_mtime_ns))
        self.assertNotEqual(vad.signature(before), vad.signature(vad.snapshot(self.unit)))
        self._old_input()
        dest = vad.discover(self.raw, self.out)[0][1]
        with patch.object(vad.subprocess, "run", side_effect=self._fake_editor()):
            self.assertEqual(vad.process_one(self.unit, dest, self.root / "editor.py", self.raw),
                             "CREATED")
        output = dest / "Info.fcpxml"
        prev = output.stat()
        output.write_bytes(b"<FCpXml />")
        os.utime(output, ns=(prev.st_atime_ns, prev.st_mtime_ns))
        self.assertEqual(vad.discover(self.raw, self.out)[0][2], "BLOCKED_UNVERIFIED")

    def test_output_symlink_back_into_raw_refused(self):
        self.out.mkdir()
        (self.out / "AFG").symlink_to(self.raw, target_is_directory=True)
        with self.assertRaises(ValueError):
            vad.discover(self.raw, self.out)

    def test_existing_output_without_manifest_refused(self):
        dest = vad.discover(self.raw, self.out)[0][1]
        dest.mkdir(parents=True)
        (dest / "Info.fcpxml").write_text("<fcpxml />")
        self.assertEqual(vad.discover(self.raw, self.out)[0][2], "BLOCKED_UNVERIFIED")
        self.assertEqual(vad.process_one(self.unit, dest, self.root / "editor.py", self.raw),
                         "BLOCKED_UNVERIFIED")

    def test_partial_stage_prevents_duplicate_run(self):
        dest = vad.discover(self.raw, self.out)[0][1]
        vad.stage_dir(dest).mkdir(parents=True)
        self.assertEqual(vad.discover(self.raw, self.out)[0][2], "BLOCKED_PARTIAL")

    def test_symlink_unit_not_followed(self):
        link = self.raw / "Kabul" / "INT" / "INT-1"
        link.parent.mkdir(parents=True)
        link.symlink_to(self.unit, target_is_directory=True)
        self.assertEqual(len(vad.discover(self.raw, self.out)), 1)


if __name__ == "__main__":
    unittest.main()
