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


def within(path, root):
    return path == root or root in path.parents


def discover(raw_root, output_root):
    raw_root = Path(raw_root).resolve(strict=True)
    output_root = Path(output_root).resolve()
    if not raw_root.is_dir() or within(output_root, raw_root) or within(raw_root, output_root):
        raise ValueError("output root and RAW root must be separate")
    jobs = []
    seen = set()
    for base, dirs, files in os.walk(raw_root, followlinks=False):
        base = Path(base)
        dirs[:] = sorted(d for d in dirs if not (base / d).is_symlink())
        rel = base.relative_to(raw_root)
        if len(rel.parts) >= 3 and rel.parts[-2].upper() in ("INT", "NAR") and UNIT.fullmatch(base.name):
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
            mapped = rel.parts if len(rel.parts) == 4 else (raw_root.name, *rel.parts)
            dest = output_root.joinpath(*mapped[:-1], base.name + "_CLEAN_VAD.fcpxmld")
            if within(dest.resolve(), raw_root):
                raise ValueError("mapped output resolves inside RAW")
            if dest.exists() or dest.is_symlink():
                state = "SKIP_VERIFIED" if valid_existing(base, dest) else "BLOCKED_UNVERIFIED"
            elif stage_dir(dest).exists():
                state = "BLOCKED_PARTIAL"
            else:
                state = "READY"
            jobs.append((base, dest, state))
            dirs[:] = []
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
            identity = (st.st_size, st.st_mtime_ns, st.st_ctime_ns, st.st_ino)
            if identity != (again.st_size, again.st_mtime_ns, again.st_ctime_ns, again.st_ino):
                raise ValueError("input changed during snapshot")
            items.append((str(p.relative_to(unit)), st.st_size, st.st_mtime_ns,
                          st.st_ctime_ns, st.st_ino, sampled))
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
        return [[".", st.st_size, st.st_mtime_ns]]
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
            items.append([str(p.relative_to(dest)), st.st_size, st.st_mtime_ns])
    if not items:
        raise ValueError("empty output bundle")
    return sorted(items)


def manifest_path(dest):
    return dest.with_name(dest.name + ".manifest.json")


def stage_dir(dest):
    """Durable one-job claim, outside RAW; abandoned staging blocks a retry."""
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


def process_one(unit, dest, editor, raw_root):
    """No RAW writes. Output is staged and published only after input recheck."""
    if within(dest.resolve(), raw_root):
        return "BLOCKED_RAW_PATH"
    if dest.exists() or dest.is_symlink():
        return "SKIP_VERIFIED" if valid_existing(unit, dest) else "BLOCKED_UNVERIFIED"
    stage = stage_dir(dest)
    if stage.exists() or stage.is_symlink():
        return "BLOCKED_PARTIAL"
    before = snapshot(unit)
    now = time.time_ns()
    if not before or any(size == 0 or now - mtime < 60_000_000_000
                         for _, size, mtime, _, _, _ in before):
        return "UNSTABLE"
    dest.parent.mkdir(parents=True, exist_ok=True)
    if within(dest.resolve(), raw_root):
        return "BLOCKED_RAW_PATH"
    try:
        stage.mkdir()  # atomic one-job reservation; a failed run leaves evidence
    except FileExistsError:
        return "BLOCKED_PARTIAL"
    except OSError:
        return "FAILED"
    staged_output = stage / dest.name
    cmd = ["python3", str(editor), "--unit", str(unit), "--output",
           str(staged_output), "--engine", "vad", "--run"]
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
    ap.add_argument("--raw-root", required=True, type=Path)
    ap.add_argument("--output-root", required=True, type=Path)
    ap.add_argument("--editor", type=Path, help="Mac-local youtube_edit.py")
    ap.add_argument("--run", action="store_true", help="opt in to VAD execution")
    args = ap.parse_args(argv)
    raw_root = args.raw_root.resolve(strict=True)
    jobs = discover(raw_root, args.output_root)
    if args.run and (not args.editor or not args.editor.is_file()):
        ap.error("--run requires --editor")
    failures = 0
    for unit, dest, state in jobs:
        if state == "READY" and args.run:
            state = process_one(unit, dest, args.editor, raw_root)
        if state in ("FAILED", "INPUT_CHANGED", "UNSTABLE",
                     "BLOCKED_UNVERIFIED", "BLOCKED_PARTIAL", "BLOCKED_RAW_PATH"):
            failures += 1
        print(json.dumps({"unit": str(unit), "output": str(dest), "status": state}))
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
