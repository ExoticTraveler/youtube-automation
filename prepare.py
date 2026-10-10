#!/usr/bin/env python3

from pathlib import Path
import shutil
import subprocess
import urllib.request
import sys

BASE = Path.home() / "YT_QUEUE"
PENDING = BASE / "pending"
PREPARED = BASE / "prepared"

CHECK_URL = "https://www.google.com/generate_204"

def is_online():
    try:
        req = urllib.request.Request(CHECK_URL, method="HEAD")
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status == 204
    except:
        return False

def parse_meta(path):
    data = {}
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            if ":" in line:
                k, v = line.split(":", 1)
                data[k.strip().lower()] = v.strip()
    return data

def main():

    if not is_online():
        print("Offline")
        sys.exit(0)

    if not PENDING.exists():
        print("No pending folder")
        sys.exit(0)

    items = sorted([x for x in PENDING.iterdir() if x.is_dir()])
    if not items:
        print("No pending items")
        sys.exit(0)

    item = items[0]
    meta_file = item / "meta.txt"

    if not meta_file.exists():
        print("meta.txt missing")
        sys.exit(0)

    meta = parse_meta(meta_file)

    title = meta.get("hook", "Untitled")
    summary = meta.get("summary", "")

    description = title + "\n\n" + summary + "\n"

    # save files
    (item / "title.txt").write_text(title + "\n", encoding="utf-8")
    (item / "description.txt").write_text(description, encoding="utf-8")

    # copy to clipboard
    subprocess.run(["pbcopy"], input=title + "\n\n" + description, 
text=True)

    # open youtube upload
    subprocess.run(["open", "-g", "https://www.youtube.com/upload"])

    # move to prepared
    PREPARED.mkdir(parents=True, exist_ok=True)
    shutil.move(str(item), str(PREPARED / item.name))

    print("Done")

if __name__ == "__main__":
    main()
