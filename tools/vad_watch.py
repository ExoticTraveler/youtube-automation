#!/usr/bin/env python3
"""Local, opt-in polling supervisor for vad_batch; never installs itself."""
import argparse
import fcntl
import json
import os
import shutil
import sys
import time
from pathlib import Path

import vad_batch


def check_volume(volume, root):
    volume = Path(volume)
    root = Path(root)
    if not volume.is_dir() or not os.path.ismount(volume):
        raise RuntimeError("VOLUME_NOT_MOUNTED")
    if volume.resolve() != volume or root.resolve() != root or not root.is_dir():
        raise RuntimeError("ROOT_PATH_CHANGED")
    if root != volume and volume not in root.parents:
        raise RuntimeError("ROOT_OUTSIDE_VOLUME")
    if os.stat(volume).st_dev != os.stat(root).st_dev:
        raise RuntimeError("ROOT_DEVICE_CHANGED")
    return os.stat(volume).st_dev


def log(path, status, **extra):
    record = {"time": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "status": status, **extra}
    with Path(path).open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(record, ensure_ascii=False) + "\n")
        stream.flush()
        os.fsync(stream.fileno())


def scan(root, volume, editor, previous, observed_at, quiet_seconds, log_path):
    device = check_volume(volume, root)
    jobs = vad_batch.discover(root)
    ready = {}
    for unit, dest, status in jobs:
        if status != "READY":
            if status != "SKIP_VERIFIED":
                log(log_path, status, unit=str(unit))
            continue
        try:
            state = vad_batch.snapshot(unit)
            newest = max((item[2] for item in state), default=0)
            if not state or any(item[1] == 0 for item in state):
                continue
            ready[str(unit)] = vad_batch.signature(state)
            if (previous.get(str(unit)) != ready[str(unit)]
                    or time.monotonic() - observed_at.get(str(unit), 0) < quiet_seconds
                    or time.time_ns() - newest < quiet_seconds * 1_000_000_000):
                continue
            if check_volume(volume, root) != device:
                raise RuntimeError("ROOT_DEVICE_CHANGED")
            free = shutil.disk_usage(volume).free
            required = max(5 * 1024**3, 2 * sum(item[1] for item in state))
            if free < required:
                raise RuntimeError("VOLUME_LOW_SPACE")
            result = vad_batch.process_one(unit, dest, editor, root,
                                           Path(log_path).parent / "reports")
            log(log_path, result, unit=str(unit), output=str(dest))
            return ready, {str(unit): time.monotonic()}, result
        except (OSError, ValueError) as exc:
            log(log_path, "SCAN_FAILED", unit=str(unit), error=str(exc))
            raise
    now = time.monotonic()
    seen = {unit: observed_at.get(unit, now) if previous.get(unit) == sig else now
            for unit, sig in ready.items()}
    return ready, seen, None


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--volume", required=True, type=Path, help="exact external mount point")
    p.add_argument("--root", required=True, type=Path)
    p.add_argument("--editor", required=True, type=Path)
    p.add_argument("--state-dir", required=True, type=Path, help="local Mac directory, outside HDD")
    p.add_argument("--interval", type=int, default=120)
    p.add_argument("--quiet-seconds", type=int, default=120)
    p.add_argument("--once", action="store_true", help="one scan, never runs a job")
    args = p.parse_args(argv)
    if args.interval < 60 or args.quiet_seconds < 60:
        p.error("interval and quiet-seconds must be at least 60")
    if not args.editor.is_file() or args.editor.is_symlink():
        p.error("editor must be an existing regular file")
    if args.state_dir.resolve() == args.volume.resolve() or args.volume.resolve() in args.state_dir.resolve().parents:
        p.error("state-dir must be on the Mac, outside the external volume")
    args.state_dir.mkdir(parents=True, exist_ok=True)
    lock = (args.state_dir / "vad_watch.lock").open("a+")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print("ALREADY_RUNNING", file=sys.stderr)
        return 2
    log_path = args.state_dir / "vad_watch.jsonl"
    previous, observed = {}, {}
    while True:
        if (args.state_dir / "STOP").exists():
            log(log_path, "STOPPED")
            return 0
        try:
            if args.once:
                check_volume(args.volume, args.root)
                jobs = vad_batch.discover(args.root)
                log(log_path, "SCAN_ONLY", jobs=len(jobs))
                return 0
            previous, observed, result = scan(args.root, args.volume, args.editor,
                                              previous, observed, args.quiet_seconds, log_path)
            if result not in (None, "CREATED", "SKIP_VERIFIED"):
                raise RuntimeError(f"PROCESS_{result}")
        except (OSError, ValueError, RuntimeError) as exc:
            log(log_path, "BLOCKED", error=str(exc))
            print(f"BLOCKED: {exc}", file=sys.stderr)
            return 1
        time.sleep(args.interval)


if __name__ == "__main__":
    raise SystemExit(main())
