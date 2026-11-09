#!/usr/bin/env python3
"""Safe, opt-in discovery wrapper for the Mac-local youtube_edit.py VAD interface."""
import argparse
import hashlib
import json
import os
import re
import tempfile
import subprocess
import time
from pathlib import Path

UNIT = re.compile(r"(INT|NAR)-[0-9]+\Z", re.I)
MEDIA = {".mp4", ".mov", ".mxf", ".wav", ".mp3", ".m4a", ".aac"}


KINDS = ("INT", "NAR")
SUFFIX = "_CLEAN_VAD.fcpxmld"


def within(path, root):
    return path == root or root in path.parents


def output_for(unit):
    """The one allowed output location: sibling of UNIT inside its INT/NAR directory."""
    return unit.parent / (unit.name + SUFFIX)


def validate_output_target(root, unit, dest):
    """Explicit allowlist. Raises ValueError unless dest is exactly
    ROOT/[COUNTRY/]CITY/{INT,NAR}/{UNIT}_CLEAN_VAD.fcpxmld for this canonical UNIT.
    Staging and manifest names are derived from dest, so they sit in the same
    directory and are covered by this check. Never inside a UNIT tree."""
    root = Path(root)
    if root.resolve() != root:
        raise ValueError("root is not canonical")
    if unit.is_symlink() or unit.resolve() != unit or not within(unit, root):
        raise ValueError("unit is not a canonical path inside root")
    rel = unit.relative_to(root)
    kind = unit.parent
    if len(rel.parts) not in (3, 4) or kind.name.upper() not in KINDS \
            or not UNIT.fullmatch(unit.name) \
            or unit.name.upper().split("-")[0] != kind.name.upper():
        raise ValueError("unit is not under a matching INT/NAR directory")
    if any(UNIT.fullmatch(part) for part in rel.parts[:-1]):
        raise ValueError("unit nested inside another unit")
    if dest != output_for(unit):  # exact match: rejects traversal, other dirs, other names
        raise ValueError("output is not the allowed sibling of its unit")
    if dest.parent.resolve() != dest.parent or dest.parent.is_symlink():
        raise ValueError("output directory is symlinked or non-canonical")
    if dest == unit or unit in dest.parents:
        raise ValueError("output inside unit tree")


def discover(root):
    root = Path(root).resolve(strict=True)
    if not root.is_dir():
        raise ValueError("root must be a directory")
    jobs = []
    seen = set()
    for base, dirs, files in os.walk(root, followlinks=False):
        base = Path(base)
        # never descend into symlinks, hidden staging dirs or generated bundles
        dirs[:] = sorted(d for d in dirs if not (base / d).is_symlink()
                         and not d.startswith(".") and not d.endswith(".fcpxmld"))
        rel = base.relative_to(root)
        if len(rel.parts) >= 3 and rel.parts[-2].upper() in KINDS and UNIT.fullmatch(base.name):
            dirs[:] = []  # a unit is a leaf; nothing inside it is ever a target
            kind = rel.parts[-2].upper()
            if base.name.upper().split("-")[0] != kind:
                raise ValueError("unit kind mismatch")
            identity = tuple(p.casefold() for p in rel.parts)
            if identity in seen:
                raise ValueError("duplicate unit identity")
            seen.add(identity)
            if len(rel.parts) not in (3, 4):
                raise ValueError("expected COUNTRY/CITY/KIND/UNIT or CITY/KIND/UNIT")
            if not files or not any(Path(f).suffix.lower() in MEDIA for f in files):
                continue
            if any((base / f).is_symlink() for f in files):
                raise ValueError("symlink in unit")
            dest = output_for(base)
            validate_output_target(root, base, dest)
            if dest.exists() or dest.is_symlink():
                state = "SKIP_VERIFIED" if valid_existing(base, dest) else "BLOCKED_UNVERIFIED"
            elif stage_dir(dest).exists() or stage_dir(dest).is_symlink():
                state = "BLOCKED_PARTIAL"
            else:
                state = "READY"
            jobs.append((base, dest, state))
    return jobs


def _sample_digest(path, size):
    """Bounded first/last 64 KiB read; never upload or hash entire large video."""
    h = hashlib.sha256()
    with path.open("rb") as stream:
        h.update(stream.read(65536))
        if size > 65536:
            stream.seek(max(0, size - 65536))
            h.update(stream.read(65536))
    return h.hexdigest()


def snapshot(unit):
    """Metadata plus bounded content samples of each local source file."""
    items = []
    for base, dirs, files in os.walk(unit, followlinks=False):
        base = Path(base)
        if any((base / d).is_symlink() for d in dirs):
            raise ValueError("symlink in unit")
        for name in sorted(files):
            p = base / name
            if p.is_symlink() or not p.is_file():
                raise ValueError("non-regular input")
            st = p.stat()
            sampled = _sample_digest(p, st.st_size)
            again = p.stat()
            identity = (st.st_size, st.st_mtime_ns, st.st_ino)
            if identity != (again.st_size, again.st_mtime_ns, again.st_ino):
                raise ValueError("input changed during snapshot")
            items.append((str(p.relative_to(unit)), st.st_size, st.st_mtime_ns,
                          st.st_ino, sampled))
    return items


def signature(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"))
                          .encode("utf-8")).hexdigest()


def output_snapshot(dest):
    """Reject empty/dangling/symlink outputs, including inside .fcpxmld bundles."""
    if dest.is_symlink() or not dest.exists():
        raise ValueError("missing or symlinked output")
    if dest.is_file():
        st = dest.stat()
        if not st.st_size:
            raise ValueError("empty output file")
        return [[".", st.st_size, st.st_mtime_ns, st.st_ctime_ns,
                 _sample_digest(dest, st.st_size)]]
    if not dest.is_dir():
        raise ValueError("unsupported output type")
    items = []
    for base, dirs, files in os.walk(dest, followlinks=False):
        base = Path(base)
        if any((base / d).is_symlink() for d in dirs):
            raise ValueError("symlinked output directory")
        for name in sorted(files):
            p = base / name
            if p.is_symlink() or not p.is_file():
                raise ValueError("non-regular output member")
            st = p.stat()
            if not st.st_size:
                raise ValueError("empty output member")
            items.append([str(p.relative_to(dest)), st.st_size, st.st_mtime_ns,
                          st.st_ctime_ns, _sample_digest(p, st.st_size)])
    if not items:
        raise ValueError("empty output bundle")
    return sorted(items)


def manifest_path(dest):
    return dest.with_name(dest.name + ".manifest.json")


def stage_dir(dest):
    """Durable one-job claim next to the output (hidden, never inside a UNIT); abandoned staging blocks a retry."""
    return dest.with_name("." + dest.name + ".staging")


def valid_existing(unit, dest):
    """An old unmanifested output is NOT assumed valid and is never overwritten."""
    manifest = manifest_path(dest)
    if manifest.is_symlink() or not manifest.is_file():
        return False
    try:
        record = json.loads(manifest.read_text(encoding="utf-8"))
        if not isinstance(record, dict) or set(record) != {
            "schema", "unit", "source_signature", "output_signature", "engine"}:
            return False
        return (
            record["schema"] == 1 and record["unit"] == str(unit.resolve())
            and record["engine"] == "vad"
            and record["source_signature"] == signature(snapshot(unit))
            and record["output_signature"] == signature(output_snapshot(dest))
        )
    except (OSError, ValueError, TypeError):
        return False


def write_manifest(dest, unit, input_state):
    rec = {"schema": 1, "unit": str(unit.resolve()), "engine": "vad",
           "source_signature": signature(input_state),
           "output_signature": signature(output_snapshot(dest))}
    manifest = manifest_path(dest)
    if manifest.exists() or manifest.is_symlink():
        raise ValueError("manifest already exists")
    fd, tmp = tempfile.mkstemp(prefix="." + manifest.name + "-", dir=dest.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(rec, handle, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, manifest)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def process_one(unit, dest, editor, root, work_root=None):
    """Writes only the allowlisted sibling output, its staging dir and manifest.
    Output is staged and published only after an input recheck."""
    try:
        validate_output_target(root, unit, dest)
    except ValueError:
        return "BLOCKED_OUTPUT_PATH"
    if dest.exists() or dest.is_symlink():
        return "SKIP_VERIFIED" if valid_existing(unit, dest) else "BLOCKED_UNVERIFIED"
    stage = stage_dir(dest)
    if stage.exists() or stage.is_symlink():
        return "BLOCKED_PARTIAL"
    before = snapshot(unit)
    now = time.time_ns()
    if not before or any(size == 0 or now - mtime < 60_000_000_000
                         for _, size, mtime, _, _ in before):
        return "UNSTABLE"
    try:
        validate_output_target(root, unit, dest)  # re-check immediately before writing
    except ValueError:
        return "BLOCKED_OUTPUT_PATH"
    try:
        stage.mkdir()  # atomic one-job reservation; a failed run leaves evidence
    except FileExistsError:
        return "BLOCKED_PARTIAL"
    except OSError:
        return "FAILED"
    staged_output = stage / dest.name
    cmd = ["python3", str(editor), "--unit", str(unit), "--output",
           str(staged_output), "--engine", "vad", "--run"]
    if work_root is not None:
        cmd += ["--work-root", str(work_root)]
    try:
        result = subprocess.run(cmd, check=False)
        if result.returncode != 0:
            return "FAILED"
        if snapshot(unit) != before:
            return "INPUT_CHANGED"
        output_snapshot(staged_output)  # refuse empty and symlink bundles
        if dest.exists() or dest.is_symlink() or manifest_path(dest).exists():
            return "BLOCKED_UNVERIFIED"
        os.replace(staged_output, dest)  # atomic publication on same filesystem
        write_manifest(dest, unit, before)
        stage.rmdir()
        return "CREATED"
    except (OSError, ValueError):
        # Keep partial/staging and any unmanifested output for manual inspection.
        return "FAILED"


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--root", "--raw-root", dest="root", required=True, type=Path,
                    help="ROOT containing [COUNTRY/]CITY/{INT,NAR}/UNIT; outputs are "
                         "written beside each UNIT, inside its INT/NAR directory")
    ap.add_argument("--editor", type=Path, help="Mac-local youtube_edit.py")
    ap.add_argument("--work-root", type=Path, help="local Mac report directory, outside source tree")
    ap.add_argument("--run", action="store_true", help="opt in to VAD execution")
    args = ap.parse_args(argv)
    root = args.root.resolve(strict=True)
    jobs = discover(root)
    if args.run and (not args.editor or not args.editor.is_file()):
        ap.error("--run requires --editor")
    failures = 0
    for unit, dest, state in jobs:
        if state == "READY" and args.run:
            state = process_one(unit, dest, args.editor, root, args.work_root)
        if state in ("FAILED", "INPUT_CHANGED", "UNSTABLE",
                     "BLOCKED_UNVERIFIED", "BLOCKED_PARTIAL", "BLOCKED_OUTPUT_PATH"):
            failures += 1
        print(json.dumps({"unit": str(unit), "output": str(dest), "status": state}))
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())

