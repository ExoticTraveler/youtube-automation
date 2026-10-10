#!/usr/bin/env python3
"""Read-only on-disk status. A saved session does not prove a worker is running."""
from pathlib import Path
from final_subtitle_autogen import final_status
BASE = Path.home() / "YT_QUEUE"
def present(path):
    return path.is_file() and path.stat().st_size > 0
def main():
    for root in (BASE / "prepared", BASE / "done"):
        for p in sorted(root.iterdir()):
            if not p.is_dir() or p.name.startswith("."): continue
            subs = p / "subs"
            upload = "DONE" if root.name == "done" else "WAITING"
            if root.name != "done":
                if present(p / "youtube_video_id.txt"): upload = "VERIFY/MOVE PENDING"
                elif (p / "upload_session.json").exists(): upload = "RESUMABLE (active or paused)"
                elif (p / ".upload_ready").exists(): upload = "READY (stability rechecked)"
                elif (p / ".upload_candidate.json").exists(): upload = "STABILIZING"
            whisper = "DONE" if present(subs / "en.srt") else "FAILED" if (p / ".sub_failed").exists() else "WAITING/PROCESSING"
            caption = "DONE" if present(subs / "youtube.srt") else "WAITING"
            hybrid = "DONE" if present(subs / "hybrid.srt") else "PENDING" if whisper == caption == "DONE" else "BLOCKED"
            final = final_status(subs)
            print(f"{root.name}/{p.name}: upload={upload}; whisper={whisper}; youtube_caption={caption}; hybrid={hybrid}; final={final}")
if __name__ == "__main__": main()
