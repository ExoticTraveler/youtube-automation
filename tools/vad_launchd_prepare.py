#!/usr/bin/env python3
"""Prepare a reviewable macOS LaunchAgent plist. Never install or start it."""
import argparse
import os
import plistlib
import sys
from pathlib import Path

LABEL = "com.exotictraveler.youtube-vad"


def render(python, watcher, volume, root, editor, state):
    """Return plist bytes without touching the Mac or external HDD."""
    path = list(dict.fromkeys([
        str(Path(python).parent),
        str(Path.home() / ".pyenv" / "shims"),
        "/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin",
        "/usr/sbin", "/sbin"
    ]))
    config = {
        "Label": LABEL,
        "ProgramArguments": [
            str(python), str(watcher),
            "--volume", str(volume), "--root", str(root),
            "--editor", str(editor), "--state-dir", str(state)
        ],
        "WorkingDirectory": str(watcher.parent),
        "EnvironmentVariables": {"PATH": ":".join(path), "PYTHONUNBUFFERED": "1"},
        "RunAtLoad": True,
        # A successful STOP exits 0 and stays stopped. A failed mount may
        # be retried after a throttle; the watcher itself always rechecks.
        "KeepAlive": {"SuccessfulExit": False},
        "ThrottleInterval": 300,
        "StandardOutPath": str(state / "launchd.stdout.log"),
        "StandardErrorPath": str(state / "launchd.stderr.log")
    }
    return plistlib.dumps(config, fmt=plistlib.FMT_XML, sort_keys=True)


def validate(volume, root, editor, watcher, state):
    paths = [Path(x).expanduser() for x in (volume, root, editor, watcher, state)]
    volume, root, editor, watcher, state = paths
    if not volume.is_dir() or not os.path.ismount(volume):
        raise ValueError("VOLUME_NOT_MOUNTED")
    if any(x.resolve() != x for x in (volume, root, editor, watcher, state)):
        raise ValueError("PATH_NOT_CANONICAL")
    if not root.is_dir() or not (root == volume or volume in root.parents):
        raise ValueError("ROOT_OUTSIDE_VOLUME")
    if not editor.is_file() or editor.is_symlink() or not watcher.is_file() or watcher.is_symlink():
        raise ValueError("EDITOR_OR_WATCHER_NOT_FOUND")
    if state == volume or volume in state.parents:
        raise ValueError("STATE_ON_HDD")
    if not Path(sys.executable).is_file():
        raise ValueError("PYTHON_NOT_FOUND")
    return volume, root, editor, watcher, state


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--volume", required=True, type=Path)
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--editor", required=True, type=Path)
    parser.add_argument("--watcher", required=True, type=Path)
    parser.add_argument("--state-dir", required=True, type=Path)
    parser.add_argument("--write", action="store_true",
                        help="stage plist under state-dir only; never install or launch it")
    args = parser.parse_args(argv)
    try:
        volume, root, editor, watcher, state = validate(
            args.volume, args.root, args.editor, args.watcher, args.state_dir)
    except ValueError as exc:
        parser.error(str(exc))
    output = state / "staged" / (LABEL + ".plist")
    blob = render(Path(sys.executable), watcher, volume, root, editor, state)
    if not args.write:
        print("PLAN_ONLY (no writes): " + str(output))
        print("PROGRAM: " + " ".join(plistlib.loads(blob)["ProgramArguments"]))
        return 0
    output.parent.mkdir(parents=True, exist_ok=True)
    if output.exists() or output.is_symlink():
        parser.error("REFUSE_OVERWRITE_STAGED_PLIST")
    with output.open("xb") as stream:
        stream.write(blob)
    print("STAGED_ONLY (not installed, not running): " + str(output))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
