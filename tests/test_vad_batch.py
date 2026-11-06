import hashlib
import importlib.util
import io
import os
import tempfile
import time
from contextlib import redirect_stdout
from types import SimpleNamespace
from unittest.mock import patch
import unittest
from pathlib import Path

spec = importlib.util.spec_from_file_location("vad_batch", Path(__file__).resolve().parents[1] / "tools" / "vad_batch.py")
vad = importlib.util.module_from_spec(spec)
spec.loader.exec_module(vad)


def tree_state(path):
    """Exact names + bytes of every entry under path (for source-protection checks)."""
    out = {}
    for base, dirs, files in os.walk(path, followlinks=False):
        for name in dirs + files:
            p = Path(base) / name
            out[str(p.relative_to(path))] = (
                "link" if p.is_symlink() else
                "dir" if p.is_dir() else hashlib.sha256(p.read_bytes()).hexdigest())
    return out


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve() / "ROOT"
        self.units = []
        # 2 countries x 2 cities, overlapping UNIT IDs, both kinds
        for country, cities in (("AFG", ("Herat", "Kabul")), ("JPN", ("Tokyo", "Osaka"))):
            for city in cities:
                for unit in ("INT-1", "NAR-1"):
                    self.units.append(self._mkunit(country, city, unit))
        self.unit = self.root / "AFG" / "Herat" / "INT" / "INT-1"

    def _mkunit(self, country, city, unit, data=b"fake"):
        u = self.root / country / city / unit.split("-")[0] / unit
        u.mkdir(parents=True)
        (u / "clip.mov").write_bytes(data)
        return u

    def _age(self):
        for u in self.units:
            for f in u.iterdir():
                os.utime(f, (time.time() - 130, time.time() - 130))

    def _editor(self, mutate=None, fail=False):
        def run(cmd, check=False):
            out = Path(cmd[cmd.index("--output") + 1])
            if fail:
                out.mkdir(parents=True)  # partial
                return SimpleNamespace(returncode=1)
            out.mkdir(parents=True)
            (out / "Info.fcpxml").write_text("<fcpxml />")
            if mutate:
                mutate()
            return SimpleNamespace(returncode=0)
        return run

    def _jobs(self):
        return {j[0]: j for j in vad.discover(self.root)}


class OutputContract(Base):
    def test_sibling_output_mapping_overlapping_ids(self):
        jobs = self._jobs()
        self.assertEqual(set(jobs), set(self.units))
        dests = {j[1] for j in jobs.values()}
        self.assertEqual(len(dests), 8)
        for unit, dest, state in jobs.values():
            self.assertEqual(state, "READY")
            self.assertEqual(dest, unit.parent / f"{unit.name}_CLEAN_VAD.fcpxmld")
            self.assertNotIn(unit, dest.parents)
        self.assertIn(self.root / "JPN/Osaka/NAR/NAR-1_CLEAN_VAD.fcpxmld", dests)

    def test_country_as_root_supported(self):
        jobs = vad.discover(self.root / "AFG")
        self.assertEqual(len(jobs), 4)
        self.assertIn(self.root / "AFG/Kabul/NAR/NAR-1_CLEAN_VAD.fcpxmld", {j[1] for j in jobs})

    def test_refused_targets(self):
        u = self.unit
        bad = [
            u / "INT-1_CLEAN_VAD.fcpxmld",                     # inside UNIT
            u / "clip.mov",                                    # source file
            u,                                                 # UNIT itself
            u.parent / "INT-2_CLEAN_VAD.fcpxmld",              # wrong unit name
            u.parent / "INT-1_CLEAN_VAD.fcpxmld.mov",          # wrong suffix
            self.root / "AFG/Herat/NAR/INT-1_CLEAN_VAD.fcpxmld",  # other kind dir
            self.root / "AFG/Kabul/INT/INT-1_CLEAN_VAD.fcpxmld",  # other city
            self.root / "INT-1_CLEAN_VAD.fcpxmld",             # unrelated ROOT path
            u.parent / "x" / ".." / "INT-1_CLEAN_VAD.fcpxmld",  # traversal
            u / ".." / "INT-1_CLEAN_VAD.fcpxmld",              # traversal via unit
        ]
        for dest in bad:
            with self.subTest(dest=dest):
                with self.assertRaises(ValueError):
                    vad.validate_output_target(self.root, u, dest)
                self.assertEqual(vad.process_one(u, dest, Path("/nonexistent"), self.root),
                                 "BLOCKED_OUTPUT_PATH")
        vad.validate_output_target(self.root, u, vad.output_for(u))  # the one allowed

    def test_unit_outside_root_or_wrong_kind_refused(self):
        other = Path(self.tmp.name).resolve() / "elsewhere" / "C" / "INT" / "INT-1"
        other.mkdir(parents=True)
        with self.assertRaises(ValueError):
            vad.validate_output_target(self.root, other, vad.output_for(other))
        wrong = self.root / "AFG" / "Herat" / "NAR" / "INT-7"
        wrong.mkdir(parents=True)
        with self.assertRaises(ValueError):
            vad.validate_output_target(self.root, wrong, vad.output_for(wrong))

    def test_nested_unit_inside_unit_never_targeted(self):
        nested = self.unit / "INT" / "INT-2"
        nested.mkdir(parents=True)
        (nested / "clip.mov").write_bytes(b"x")
        self.assertNotIn(nested, self._jobs())
        with self.assertRaises(ValueError):
            vad.validate_output_target(self.root, nested, vad.output_for(nested))

    def test_generated_outputs_and_staging_not_rediscovered(self):
        dest = vad.output_for(self.unit)
        (dest / "INT" / "INT-9").mkdir(parents=True)
        (dest / "INT" / "INT-9" / "a.mov").write_bytes(b"x")
        st = vad.stage_dir(vad.output_for(self.root / "AFG/Herat/NAR/NAR-1"))
        (st / "NAR" / "NAR-9").mkdir(parents=True)
        (st / "NAR" / "NAR-9" / "a.mov").write_bytes(b"x")
        self.assertEqual(set(self._jobs()), set(self.units))


class Symlinks(Base):
    def test_symlinked_kind_dir_escape_refused(self):
        outside = Path(self.tmp.name).resolve() / "outside" / "INT"
        (outside / "INT-5").mkdir(parents=True)
        (outside / "INT-5" / "clip.mov").write_bytes(b"x")
        city = self.root / "AFG" / "Mazar"
        city.mkdir()
        (city / "INT").symlink_to(outside, target_is_directory=True)
        self.assertEqual(set(self._jobs()), set(self.units))  # not followed
        fake = city / "INT" / "INT-5"
        with self.assertRaises(ValueError):
            vad.validate_output_target(self.root, fake, vad.output_for(fake))
        self.assertEqual(vad.process_one(fake, vad.output_for(fake), Path("/x"), self.root),
                         "BLOCKED_OUTPUT_PATH")

    def test_symlink_unit_not_followed(self):
        link = self.root / "AFG" / "Herat" / "INT" / "INT-2"
        link.symlink_to(self.unit, target_is_directory=True)
        self.assertNotIn(link, self._jobs())
        with self.assertRaises(ValueError):
            vad.validate_output_target(self.root, link, vad.output_for(link))

    def test_output_symlink_blocked_not_followed(self):
        target = Path(self.tmp.name).resolve() / "elsewhere"
        target.mkdir()
        dest = vad.output_for(self.unit)
        dest.symlink_to(target, target_is_directory=True)
        self.assertEqual(self._jobs()[self.unit][2], "BLOCKED_UNVERIFIED")
        self._age()
        with patch.object(vad.subprocess, "run") as run:
            self.assertEqual(vad.process_one(self.unit, dest, Path("/x"), self.root),
                             "BLOCKED_UNVERIFIED")
            run.assert_not_called()
        self.assertEqual(list(target.iterdir()), [])

    def test_symlinked_source_file_refused(self):
        (self.unit / "link.mov").symlink_to(self.unit / "clip.mov")
        with self.assertRaises(ValueError):
            vad.discover(self.root)


class Lifecycle(Base):
    def test_run_writes_only_allowed_paths_and_sources_untouched(self):
        self._age()
        before = tree_state(self.root)
        dest = vad.output_for(self.unit)
        with patch.object(vad.subprocess, "run", side_effect=self._editor()) as run:
            self.assertEqual(vad.process_one(self.unit, dest, Path("/e.py"), self.root), "CREATED")
        cmd = run.call_args[0][0]
        self.assertEqual(cmd[cmd.index("--unit") + 1], str(self.unit))
        self.assertEqual(Path(cmd[cmd.index("--output") + 1]).parent, vad.stage_dir(dest))
        after = tree_state(self.root)
        added = set(after) - set(before)
        kind_rel = str(self.unit.parent.relative_to(self.root))
        self.assertEqual(added, {f"{kind_rel}/INT-1_CLEAN_VAD.fcpxmld",
                                 f"{kind_rel}/INT-1_CLEAN_VAD.fcpxmld/Info.fcpxml",
                                 f"{kind_rel}/INT-1_CLEAN_VAD.fcpxmld.manifest.json"})
        self.assertEqual({k: before[k] for k in before}, {k: after[k] for k in before})
        self.assertFalse(vad.stage_dir(dest).exists())

    def test_rerun_verified_skip_and_tampered_output(self):
        self._age()
        dest = vad.output_for(self.unit)
        with patch.object(vad.subprocess, "run", side_effect=self._editor()):
            vad.process_one(self.unit, dest, Path("/e.py"), self.root)
        self.assertEqual(self._jobs()[self.unit][2], "SKIP_VERIFIED")
        with patch.object(vad.subprocess, "run") as run:
            self.assertEqual(vad.process_one(self.unit, dest, Path("/e.py"), self.root),
                             "SKIP_VERIFIED")
            run.assert_not_called()
        out = dest / "Info.fcpxml"
        prev = out.stat()
        out.write_bytes(b"<FCpXml />")  # same size, preserved mtime
        os.utime(out, ns=(prev.st_atime_ns, prev.st_mtime_ns))
        self.assertEqual(self._jobs()[self.unit][2], "BLOCKED_UNVERIFIED")

    def test_changed_source_after_success_blocks(self):
        self._age()
        dest = vad.output_for(self.unit)
        with patch.object(vad.subprocess, "run", side_effect=self._editor()):
            vad.process_one(self.unit, dest, Path("/e.py"), self.root)
        (self.unit / "clip.mov").write_bytes(b"FAKE")
        self.assertEqual(self._jobs()[self.unit][2], "BLOCKED_UNVERIFIED")

    def test_unverified_existing_output_never_overwritten(self):
        dest = vad.output_for(self.unit)
        dest.mkdir()
        (dest / "Info.fcpxml").write_text("<old />")
        self._age()
        self.assertEqual(self._jobs()[self.unit][2], "BLOCKED_UNVERIFIED")
        with patch.object(vad.subprocess, "run") as run:
            self.assertEqual(vad.process_one(self.unit, dest, Path("/e.py"), self.root),
                             "BLOCKED_UNVERIFIED")
            run.assert_not_called()
        self.assertEqual((dest / "Info.fcpxml").read_text(), "<old />")

    def test_input_changed_during_edit_never_published(self):
        self._age()
        dest = vad.output_for(self.unit)
        mutate = lambda: (self.unit / "clip.mov").write_bytes(b"new material")
        with patch.object(vad.subprocess, "run", side_effect=self._editor(mutate=mutate)):
            self.assertEqual(vad.process_one(self.unit, dest, Path("/e.py"), self.root),
                             "INPUT_CHANGED")
        self.assertFalse(dest.exists())
        self.assertTrue(vad.stage_dir(dest).is_dir())
        self.assertEqual(self._jobs()[self.unit][2], "BLOCKED_PARTIAL")

    def test_interrupted_staging_blocks_retry(self):
        self._age()
        dest = vad.output_for(self.unit)
        with patch.object(vad.subprocess, "run", side_effect=self._editor(fail=True)):
            self.assertEqual(vad.process_one(self.unit, dest, Path("/e.py"), self.root), "FAILED")
        self.assertTrue(vad.stage_dir(dest).is_dir())
        self.assertFalse(dest.exists())
        self.assertEqual(self._jobs()[self.unit][2], "BLOCKED_PARTIAL")
        with patch.object(vad.subprocess, "run") as run:
            self.assertEqual(vad.process_one(self.unit, dest, Path("/e.py"), self.root),
                             "BLOCKED_PARTIAL")
            run.assert_not_called()

    def test_fresh_input_unstable(self):
        with patch.object(vad.subprocess, "run") as run:
            self.assertEqual(vad.process_one(self.unit, vad.output_for(self.unit),
                                             Path("/e.py"), self.root), "UNSTABLE")
            run.assert_not_called()

    def test_same_size_rewrite_changes_signature(self):
        source = self.unit / "clip.mov"
        old = source.stat()
        before = vad.snapshot(self.unit)
        source.write_bytes(b"FAKE")
        os.utime(source, ns=(old.st_atime_ns, old.st_mtime_ns))
        self.assertNotEqual(vad.signature(before), vad.signature(vad.snapshot(self.unit)))


class Cli(Base):
    def test_default_dry_run_writes_nothing(self):
        self._age()
        before = tree_state(self.root)
        buf = io.StringIO()
        with patch.object(vad.subprocess, "run") as run, redirect_stdout(buf):
            self.assertEqual(vad.main(["--root", str(self.root)]), 0)
            run.assert_not_called()
        self.assertEqual(tree_state(self.root), before)
        self.assertEqual(buf.getvalue().count('"READY"'), 8)

    def test_legacy_output_root_rejected(self):
        with self.assertRaises(SystemExit), redirect_stdout(io.StringIO()), \
                patch("sys.stderr", io.StringIO()):
            vad.main(["--raw-root", str(self.root), "--output-root", "/tmp/x"])

    def test_run_requires_editor(self):
        with self.assertRaises(SystemExit), patch("sys.stderr", io.StringIO()):
            vad.main(["--root", str(self.root), "--run"])


if __name__ == "__main__":
    unittest.main()
