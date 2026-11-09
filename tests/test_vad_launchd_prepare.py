import importlib.util
import plistlib
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

SOURCE = Path(__file__).resolve().parents[1] / "tools" / "vad_launchd_prepare.py"
spec = importlib.util.spec_from_file_location("vad_launchd_prepare", SOURCE)
prepare = importlib.util.module_from_spec(spec)
spec.loader.exec_module(prepare)


class PlistPreparation(unittest.TestCase):
    def test_render_has_no_once_and_keeps_processing_on_external_root(self):
        state = Path("/Users/test/Library/Application Support/youtube-vad")
        config = plistlib.loads(prepare.render(
            Path("/usr/bin/python3"), Path("/Users/test/code/vad_watch.py"),
            Path("/Volumes/New Volume"), Path("/Volumes/New Volume/PAK"),
            Path("/Users/test/Downloads/youtube_edit.py"), state))
        self.assertEqual(config["ProgramArguments"][2:6],
                         ["--volume", "/Volumes/New Volume",
                          "--root", "/Volumes/New Volume/PAK"])
        self.assertNotIn("--once", config["ProgramArguments"])
        self.assertEqual(config["KeepAlive"], {"SuccessfulExit": False})
        self.assertEqual(config["ThrottleInterval"], 300)
        self.assertIn("PATH", config["EnvironmentVariables"])
        self.assertEqual(config["StandardErrorPath"], str(state / "launchd.stderr.log"))

    def test_plan_is_read_only_and_write_only_stages(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp).resolve()
            volume, root, state = base / "volume", base / "state-root", base / "state"
            volume.mkdir()
            root = volume / "PAK"
            root.mkdir()
            editor, watcher = base / "youtube_edit.py", base / "vad_watch.py"
            editor.write_text("# editor")
            watcher.write_text("# watcher")
            args = ["--volume", str(volume), "--root", str(root),
                    "--editor", str(editor), "--watcher", str(watcher),
                    "--state-dir", str(state)]
            with patch.object(prepare.os.path, "ismount", return_value=True):
                self.assertEqual(prepare.main(args), 0)
                self.assertFalse(state.exists())
                self.assertEqual(prepare.main(args + ["--write"]), 0)
                output = state / "staged" / (prepare.LABEL + ".plist")
                self.assertTrue(output.is_file())
                self.assertEqual(plistlib.loads(output.read_bytes())["ProgramArguments"][5],
                                 str(root))
                with self.assertRaises(SystemExit):
                    prepare.main(args + ["--write"])
            self.assertFalse((base / "Library" / "LaunchAgents").exists())

    def test_missing_mount_rejected_without_state_writes(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp).resolve()
            volume = base / "volume"
            volume.mkdir()
            editor = base / "editor.py"
            watcher = base / "watcher.py"
            editor.write_text("# editor")
            watcher.write_text("# watcher")
            state = base / "state"
            args = ["--volume", str(volume), "--root", str(volume),
                    "--editor", str(editor), "--watcher", str(watcher),
                    "--state-dir", str(state), "--write"]
            with patch.object(prepare.os.path, "ismount", return_value=False):
                with self.assertRaises(SystemExit):
                    prepare.main(args)
            self.assertFalse(state.exists())


if __name__ == "__main__":
    unittest.main()
