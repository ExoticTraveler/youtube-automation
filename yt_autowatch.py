#!/usr/bin/env python3
"""One launchd scan; the uploader retains its own global lock and retries."""
import argparse
import fcntl
import subprocess
import traceback
from pathlib import Path
from datetime import datetime
from tools.pipeline_state import find_video, stable, paths_lock
BASE = Path.home() / "YT_QUEUE"
PYTHON = str(Path.home() / ".pyenv/versions/3.10.16/bin/python3")
def log(msg):
    line = f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}"
    print(line, flush=True)
    with (BASE / "logs/yt_autowatch.log").open("a") as f: f.write(line + "\n")
def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="Inspect only; no state changes or upload")
    args = parser.parse_args()
    if args.dry_run:
        for folder in sorted((BASE / "prepared").iterdir()):
            if folder.is_dir() and not folder.name.startswith("."):
                video = find_video(folder)
                print(f"{folder.name}: video={video.name if video else 'NONE'} ready={(folder / '.upload_ready').exists()}")
        from tools.publishing import process
        for folder in sorted((BASE / "done").iterdir()):
            if folder.is_dir() and not folder.is_symlink() and not folder.name.startswith("."):
                print(f"{folder.name}: {process(folder)}")
        return
    with (BASE / ".yt_autowatch.lock").open("a") as handle:
        try: fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError: return
        # Independent metadata worker: never delay upload or change its inputs.
        # Direct metadata creation waits for a verified project in done/.
        metadata = BASE / "tools/metadata.py"
        if metadata.is_file():
            try:
                with (BASE / "logs/metadata.log").open("a") as output:
                    subprocess.Popen([PYTHON, "-B", str(metadata), "--scan", "--run"],
                                     stdin=subprocess.DEVNULL, stdout=output, stderr=output,
                                     start_new_session=True)
            except OSError as exc:
                log(f"METADATA worker unavailable: {type(exc).__name__}: {exc}")
                traceback.print_exc()
        # Thumbnail generation is intentionally NOT part of this pipeline (paid image API; manual, opt-in tool).
        # Verification recovery must not depend on a video or .upload_ready.
        ready = any(p.is_dir() and (p / "youtube_video_id.txt").exists()
                    for root in (BASE / "prepared", BASE / "failed")
                    for p in root.iterdir())
        with paths_lock():
            for folder in sorted((BASE / "prepared").iterdir()):
                if not folder.is_dir() or folder.name.startswith("."): continue
                try:
                    if (folder / "youtube_video_id.txt").exists(): continue
                    video = find_video(folder)
                    if video and stable(folder, video, log):
                        marker = folder / ".upload_ready"
                        if not marker.exists():
                            marker.write_text("auto: stable video\n")
                            log(f"UPLOAD READY {folder.name}")
                        ready = True
                except (OSError, ValueError) as exc: log(f"SCAN ERROR {folder.name}: {exc}")
        if ready:
            result = subprocess.run([PYTHON, str(BASE / "yt_autoupload.py")])
            log(f"Uploader exited code={result.returncode}")
        # Runs even when prepared/ is empty. Shares the uploader lock and never
        # uploads video bytes. Remote updates require a separate explicit opt-in.
        publishing = BASE / "tools/publishing.py"
        if publishing.is_file():
            try:
                with (BASE / "logs/publishing.log").open("a") as output:
                    subprocess.Popen([PYTHON, "-B", str(publishing), "--run"],
                                     stdin=subprocess.DEVNULL, stdout=output, stderr=output,
                                     start_new_session=True)
            except OSError as exc:
                log(f"PUBLISHING worker unavailable: {type(exc).__name__}: {exc}")
if __name__ == "__main__": main()
