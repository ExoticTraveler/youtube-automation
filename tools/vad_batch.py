#!/usr/bin/env python3
"""Safe, opt-in discovery wrapper for the Mac-local youtube_edit.py VAD interface."""
import argparse
import json
import os
import re
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
            if dest.exists() or dest.is_symlink():
                state = "SKIP_EXISTING"
            else:
                state = "READY"
            jobs.append((base, dest, state))
            dirs[:] = []
    return jobs


def snapshot(unit):
    items = []
    for base, dirs, files in os.walk(unit, followlinks=False):
        base = Path(base)
        if any((base / d).is_symlink() for d in dirs):
            raise ValueError("symlink in unit")
        for name in sorted(files):
            p = base / name
            if p.is_symlink():
                raise ValueError("symlink in unit")
            st = p.stat()
            items.append((str(p.relative_to(unit)), st.st_size, st.st_mtime_ns))
    return items


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--raw-root", required=True, type=Path)
    ap.add_argument("--output-root", required=True, type=Path)
    ap.add_argument("--editor", type=Path, help="Mac-local youtube_edit.py")
    ap.add_argument("--run", action="store_true", help="opt in to VAD execution")
    args = ap.parse_args(argv)
    jobs = discover(args.raw_root, args.output_root)
    if args.run and (not args.editor or not args.editor.is_file()):
        ap.error("--run requires --editor")
    failures = 0
    for unit, dest, state in jobs:
        if state == "READY" and args.run:
            before = snapshot(unit)
            if not before or any(size == 0 or time.time_ns() - mtime < 60_000_000_000
                                 for _, size, mtime in before):
                state = "UNSTABLE"
            else:
                after = snapshot(unit)
                if before != after:
                    state = "UNSTABLE"
                else:
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    cmd = ["python3", str(args.editor), "--unit", str(unit), "--output", str(dest), "--engine", "vad", "--run"]
                    result = subprocess.run(cmd, check=False)
                    state = "CREATED" if result.returncode == 0 and dest.exists() else "FAILED"
                    if snapshot(unit) != before:
                        state = "INPUT_CHANGED"
        if state in ("FAILED", "INPUT_CHANGED", "UNSTABLE"):
            failures += 1
        print(json.dumps({"unit": str(unit), "output": str(dest), "status": state}))
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())

