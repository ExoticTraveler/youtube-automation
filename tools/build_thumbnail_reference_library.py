#!/usr/bin/env python3
"""Archive current channel thumbnails using the existing readonly YouTube token.

This tool makes only YouTube Data API read calls (channels, playlistItems,
videos) and HTTP GETs for image bytes. It has no upload/update/delete path.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.request
from datetime import datetime, timezone
from pathlib import Path


BASE = Path(__file__).resolve().parents[1] if __file__ else Path.cwd()
YT_QUEUE = Path.home() / "YT_QUEUE"
LIBRARY = YT_QUEUE / "thumbnail_reference_library"
IMAGES = LIBRARY / "images"
MANIFEST = LIBRARY / "manifest.json"


def load_service():
    sys.path.insert(0, str(YT_QUEUE))
    from yt_autoupload import get_check_service  # existing readonly helper
    return get_check_service()


def pages(request_builder):
    token = None
    while True:
        kwargs = {"maxResults": 50}
        if token:
            kwargs["pageToken"] = token
        response = request_builder(**kwargs).execute()
        for item in response.get("items", []):
            yield item
        token = response.get("nextPageToken")
        if not token:
            break


def channel_and_uploads(service):
    mine = service.channels().list(
        part="id,snippet,contentDetails,status", mine=True
    ).execute().get("items", [])
    if not mine:
        raise RuntimeError("Readonly account has no accessible channel")
    channel = mine[0]
    uploads_id = channel["contentDetails"]["relatedPlaylists"]["uploads"]
    items = list(pages(lambda **kw: service.playlistItems().list(
        part="snippet,contentDetails", playlistId=uploads_id, **kw
    )))
    video_ids = [item["contentDetails"].get("videoId") for item in items]
    video_ids = [value for value in video_ids if value]
    videos = {}
    for start in range(0, len(video_ids), 50):
        response = service.videos().list(
            part="snippet,contentDetails,status", id=",".join(video_ids[start:start + 50])
        ).execute()
        videos.update({item["id"]: item for item in response.get("items", [])})
    ordered = [videos[value] for value in video_ids if value in videos]
    return channel, ordered


def variants(thumbnails):
    # Prefer resolution, then the documented fallback order.
    order = ("maxres", "standard", "high", "medium", "default")
    present = [(name, thumbnails[name]) for name in order if name in thumbnails]
    return sorted(present, key=lambda pair: (pair[1].get("width", 0), pair[1].get("height", 0)), reverse=True)


def valid_image(path: Path) -> bool:
    if not path.is_file() or path.stat().st_size < 128:
        return False
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=codec_name,width,height", "-of", "json", str(path)],
            check=True, capture_output=True, text=True,
        )
        streams = json.loads(result.stdout).get("streams", [])
        return bool(streams and streams[0].get("width", 0) > 0 and streams[0].get("height", 0) > 0)
    except (OSError, subprocess.CalledProcessError, ValueError, json.JSONDecodeError):
        return False


def download(url: str, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{destination.stem}.", suffix=".tmp", dir=destination.parent)
    os.close(fd)
    temporary_path = Path(temporary)
    try:
        request = urllib.request.Request(url, headers={"User-Agent": "ExoticTraveler-thumbnail-reference/1.0"})
        with urllib.request.urlopen(request, timeout=60) as response, temporary_path.open("wb") as handle:
            shutil.copyfileobj(response, handle)
        if not valid_image(temporary_path):
            raise RuntimeError("downloaded bytes are not a decodable image")
        os.replace(temporary_path, destination)
    finally:
        temporary_path.unlink(missing_ok=True)


def read_manifest():
    if not MANIFEST.exists():
        return {}
    try:
        return {item["video_id"]: item for item in json.loads(MANIFEST.read_text(encoding="utf-8")).get("videos", [])}
    except (OSError, ValueError, KeyError, TypeError):
        return {}


def main() -> int:
    global LIBRARY, IMAGES, MANIFEST
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--refresh", action="store_true", help="redownload current thumbnails, replacing valid local copies atomically")
    parser.add_argument("--library", type=Path, default=LIBRARY, help="library root (default: ~/YT_QUEUE/thumbnail_reference_library)")
    args = parser.parse_args()
    LIBRARY = args.library.expanduser().resolve()
    IMAGES = LIBRARY / "images"
    MANIFEST = LIBRARY / "manifest.json"
    IMAGES.mkdir(parents=True, exist_ok=True)

    service = load_service()
    channel, videos = channel_and_uploads(service)
    previous = read_manifest()
    records = []
    counts = {"discovered": len(videos), "downloaded": 0, "preserved": 0, "failed": 0}

    for video in videos:
        snippet = video.get("snippet", {})
        content = video.get("contentDetails", {})
        video_id = video["id"]
        image_path = IMAGES / f"{video_id}.jpg"
        choices = variants(snippet.get("thumbnails", {}))
        selected = choices[0] if choices else (None, {})
        status = "failed"
        error = None
        if image_path.exists() and valid_image(image_path) and not args.refresh:
            status = "preserved"
            counts["preserved"] += 1
            selected_name = previous.get(video_id, {}).get("source_variant") or selected[0]
            selected_info = next((info for name, info in choices if name == selected_name), selected[1])
        else:
            selected_name, selected_info = None, {}
            for name, info in choices:
                try:
                    download(info["url"], image_path)
                    selected_name, selected_info = name, info
                    status = "downloaded"
                    counts["downloaded"] += 1
                    break
                except Exception as exc:  # record one video failure and continue
                    error = str(exc)
            if status != "downloaded":
                counts["failed"] += 1
        records.append({
            "video_id": video_id,
            "title": snippet.get("title", ""),
            "published_at": snippet.get("publishedAt"),
            "duration": content.get("duration"),
            "privacy_status": video.get("status", {}).get("privacyStatus"),
            "local_thumbnail_path": str(image_path),
            "source_variant": selected_name,
            "source_resolution": {key: selected_info.get(key) for key in ("width", "height") if key in selected_info},
            "source_thumbnail_url": selected_info.get("url"),
            "download_status": status,
            **({"error": error} if error else {}),
        })

    manifest = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "channel": {"channel_id": channel.get("id"), "title": channel.get("snippet", {}).get("title")},
        "read_only": True,
        "youtube_write_calls_made": False,
        "videos": records,
    }
    fd, temporary = tempfile.mkstemp(prefix=".manifest.", suffix=".tmp", dir=LIBRARY)
    os.close(fd)
    temporary_path = Path(temporary)
    try:
        temporary_path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        os.replace(temporary_path, MANIFEST)
    finally:
        temporary_path.unlink(missing_ok=True)
    print(json.dumps({**counts, "channel_id": channel.get("id"), "channel_title": channel.get("snippet", {}).get("title")}, ensure_ascii=False, indent=2))
    print(f"Manifest: {MANIFEST}")
    return 0 if counts["failed"] == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
