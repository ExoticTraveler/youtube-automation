import importlib.util
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

spec = importlib.util.spec_from_file_location(
    "vad_watch", Path(__file__).resolve().parents[1] / "tools" / "vad_watch.py")
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
watch = importlib.util.module_from_spec(spec)
spec.loader.exec_module(watch)


class WatchTests(unittest.TestCase):
    def test_two_stable_scans_and_one_job(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "ROOT"
            units = [root / "PAK" / "Islamabad" / "INT" / "INT-1",
                     root / "PAK" / "Islamabad" / "NAR" / "NAR-1"]
            for unit in units:
                unit.mkdir(parents=True)
                clip = unit / "clip.mov"
                clip.write_bytes(b"fixture")
                os.utime(clip, (time.time() - 500, time.time() - 500))
            log = Path(tmp) / "watch.jsonl"
            with patch.object(watch, "check_volume", return_value=7), \
                    patch.object(watch.vad_batch, "process_one", return_value="CREATED") as run:
                prior, seen, result = watch.scan(root, root, Path("/editor"),
                                                  {}, {}, 120, log)
                self.assertIsNone(result)
                run.assert_not_called()
                prior, seen, result = watch.scan(
                    root, root, Path("/editor"), prior,
                    {key: value - 121 for key, value in seen.items()}, 120, log)
                self.assertEqual(result, "CREATED")
                self.assertEqual(run.call_count, 1)
                self.assertEqual(run.call_args.args[0], units[0].resolve())
                self.assertEqual(json.loads(log.read_text().splitlines()[-1])["status"], "CREATED")

    def test_blocked_existing_output_stops_before_processing_other_units(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "ROOT"
            blocked = root / "PAK" / "Islamabad" / "INT" / "INT-1"
            ready = root / "PAK" / "Islamabad" / "NAR" / "NAR-1"
            for unit in (blocked, ready):
                unit.mkdir(parents=True)
                clip = unit / "clip.mov"
                clip.write_bytes(b"fixture")
                os.utime(clip, (time.time() - 500, time.time() - 500))
            (blocked.parent / "INT-1_CLEAN_VAD.fcpxmld").mkdir()
            log = Path(tmp) / "watch.jsonl"
            with patch.object(watch, "check_volume", return_value=7), \
                    patch.object(watch.vad_batch, "process_one") as run:
                with self.assertRaisesRegex(RuntimeError, "BLOCKED_UNVERIFIED"):
                    watch.scan(root, root, Path("/editor"), {}, {}, 120, log)
                run.assert_not_called()
            self.assertEqual(json.loads(log.read_text().splitlines()[-1])["status"],
                             "BLOCKED_UNVERIFIED")

    def test_processing_failure_stops_supervisor(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            volume = base / "volume"
            volume.mkdir()
            editor = base / "editor.py"
            editor.write_text("# fixture editor")
            state = base / "state"
            with patch.object(watch, "scan", return_value=({}, {}, "FAILED")) as scan:
                result = watch.main([
                    "--volume", str(volume), "--root", str(volume),
                    "--editor", str(editor), "--state-dir", str(state)])
            self.assertEqual(result, 1)
            self.assertEqual(scan.call_count, 1)
            statuses = [json.loads(line) for line in
                        (state / "vad_watch.jsonl").read_text().splitlines()]
            self.assertEqual(statuses[-1]["status"], "BLOCKED")
            self.assertIn("PROCESS_FAILED", statuses[-1]["error"])

    def test_stop_marker_prevents_processing(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            volume = base / "volume"
            volume.mkdir()
            editor = base / "editor.py"
            editor.write_text("# fixture editor")
            state = base / "state"
            state.mkdir()
            (state / "STOP").touch()
            with patch.object(watch, "scan") as scan:
                result = watch.main([
                    "--volume", str(volume), "--root", str(volume),
                    "--editor", str(editor), "--state-dir", str(state)])
            self.assertEqual(result, 0)
            scan.assert_not_called()
            record = json.loads((state / "vad_watch.jsonl").read_text().splitlines()[-1])
            self.assertEqual(record["status"], "STOPPED")

    def test_mount_required(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(RuntimeError, "VOLUME_NOT_MOUNTED"):
                watch.check_volume(Path(tmp), Path(tmp))


if __name__ == "__main__":
    unittest.main()
