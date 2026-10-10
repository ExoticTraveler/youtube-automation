#!/usr/bin/env python3

from tools.pipeline_state import stable, paths_lock
from pathlib import Path
import sys
import shutil
import pickle
import traceback
import fcntl
import json
import time
import ssl
import socket
import http.client
import urllib.error
import errno
from datetime import datetime

import atexit

import httplib2
import google_auth_httplib2

from tools import upload_throttle
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
from googleapiclient.errors import HttpError

from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from google.auth.exceptions import RefreshError, TransportError


# ============================================================
# PATHS
# ============================================================

BASE = Path.home() / "YT_QUEUE"

PREPARED = BASE / "prepared"
DONE = BASE / "done"
FAILED = BASE / "failed"
LOGS = BASE / "logs"
SECRETS = BASE / "secrets"

CLIENT_SECRET = SECRETS / "client_secret.json"

# Existing upload token
UPLOAD_TOKEN = SECRETS / "token.pickle"

# Separate readonly token used only to verify that
# the final video resource actually exists.
CHECK_TOKEN = SECRETS / "token_check.pickle"

LOCK_FILE = BASE / ".yt_autoupload.lock"


# ============================================================
# OAUTH
# ============================================================

UPLOAD_SCOPES = [
    "https://www.googleapis.com/auth/youtube.upload"
]

CHECK_SCOPES = [
    "https://www.googleapis.com/auth/youtube.readonly"
]


# ============================================================
# SETTINGS
# ============================================================

DEFAULT_PRIVACY = "unlisted"
DEFAULT_CATEGORY_ID = "19"
DEFAULT_LANGUAGE = "en"

# 1 MiB
# YouTube chunk size must be a multiple of 256 KiB.
UPLOAD_CHUNK_SIZE = 1024 * 1024

VIDEO_EXTS = {
    ".mp4",
    ".mov",
    ".m4v",
    ".mkv",
    ".webm",
}

THUMB_EXTS = {
    ".jpg",
    ".jpeg",
    ".png",
    ".webp",
}

KNOWN_KEYS = {
    "title",
    "title_alternatives",
    "description",
    "privacy",
    "tags",
    "categoryid",
}

RETRYABLE_HTTP = {
    408,
    429,
    500,
    502,
    503,
    504,
}

SESSION_EXPIRED_HTTP = {
    404,
    410,
}

MAX_WAIT = 300


# ============================================================
# CUSTOM EXCEPTIONS
# ============================================================

class SessionExpired(Exception):
    pass


class AuthRequired(Exception):
    pass


class TemporaryPause(Exception):
    pass


class VerificationPending(Exception):
    pass


class DiskFull(Exception):
    pass


# ============================================================
# LOGGING
# ============================================================

def log(msg, level="INFO"):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    line = (
        "["
        + ts
        + "] ["
        + level
        + "] "
        + msg
    )

    print(line, flush=True)

    try:
        LOGS.mkdir(
            parents=True,
            exist_ok=True
        )

        fp = LOGS / (
            "yt_autoupload_"
            + datetime.now().strftime("%Y%m")
            + ".log"
        )

        with fp.open(
            "a",
            encoding="utf-8"
        ) as f:
            f.write(line + "\n")

    except OSError as e:
        # If disk is full, stdout should still work.
        if e.errno != errno.ENOSPC:
            raise


# ============================================================
# HELPERS
# ============================================================

def acquire_lock():
    fd = LOCK_FILE.open("w")

    try:
        fcntl.flock(
            fd,
            fcntl.LOCK_EX | fcntl.LOCK_NB
        )

    except BlockingIOError:
        log(
            "Another instance running. Exit.",
            "WARN"
        )
        sys.exit(0)

    return fd


def verification_due(folder):
    try:
        state = json.loads((folder / ".verify_pending.json").read_text())
        return time.time() >= float(state.get("next_retry_at", 0))
    except (OSError, ValueError, TypeError, AttributeError):
        return True


def record_verification_pending(folder, exc):
    path = folder / ".verify_pending.json"
    try:
        old = json.loads(path.read_text())
        attempts = int(old.get("attempts", 0)) + 1
    except (OSError, ValueError, TypeError, AttributeError):
        attempts = 1
    delay = min(60 * 2 ** min(max(attempts - 1, 0), 6), 3600)
    video_id = load_video_id(folder) or "UNKNOWN"
    state = {"mode": "VERIFY_MOVE_PENDING", "video_id": video_id,
             "attempts": attempts, "next_retry_at": time.time() + delay,
             "last_error": str(exc)}
    log(f"VERIFY PENDING video_id={video_id} retry_in={delay}s reason={exc}", "WARN")
    try:
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(state, indent=2) + "\n")
        tmp.replace(path)
    except OSError as error:
        log(f"Cannot save verification backoff: {error}; project and ID preserved", "WARN")


def recover_verified_candidates():
    # Caller holds the uploader lock. Never overwrite an existing project.
    for folder in sorted(FAILED.iterdir()):
        if not folder.is_dir() or folder.name.startswith(".") or not video_id_path(folder).exists():
            continue
        dest = PREPARED / folder.name
        if dest.exists():
            log(f"VERIFY recovery conflict: {folder}; preserved for manual resolution", "WARN")
            continue
        with paths_lock(exclusive=True):
            shutil.move(str(folder), str(dest))
        log(f"VERIFY RETRY recovered={dest.name} video_id={load_video_id(dest)}")


def pick_ready_folder():
    PREPARED.mkdir(
        parents=True,
        exist_ok=True
    )

    candidates = []

    for folder in PREPARED.iterdir():
        if not folder.is_dir():
            continue

        if folder.name.startswith("."):
            continue

        if video_id_path(folder).exists():
            if verification_due(folder):
                candidates.append((float("inf"), folder))
            continue

        marker = folder / ".upload_ready"

        if marker.exists() and (video := find_video_file(folder)) and stable(folder, video, log):
            candidates.append(
                (
                    marker.stat().st_mtime,
                    folder
                )
            )

    if not candidates:
        log("No .upload_ready folders.")
        return None

    candidates.sort(
        key=lambda x: x[0],
        reverse=True
    )

    return candidates[0][1]


def find_video_file(folder):
    for p in sorted(folder.iterdir()):
        if (
            p.is_file()
            and p.suffix.lower() in VIDEO_EXTS
        ):
            return p

    return None


def find_thumbnail_file(folder):
    for ext in [
        ".jpg",
        ".jpeg",
        ".png",
        ".webp",
    ]:
        cand = folder / (
            "thumbnail" + ext
        )

        if cand.exists():
            return cand

    for p in sorted(folder.iterdir()):
        if (
            p.is_file()
            and p.suffix.lower() in THUMB_EXTS
            and p.stem.lower().startswith(
                "thumbnail"
            )
        ):
            return p

    return None


# ============================================================
# META
# ============================================================

def parse_meta(meta_path):
    meta = {}

    current_key = None
    buffer = []

    def flush():
        nonlocal current_key
        nonlocal buffer

        if current_key:
            meta[current_key] = (
                "\n".join(buffer).strip()
            )

        current_key = None
        buffer = []

    for raw in meta_path.read_text(
        encoding="utf-8"
    ).splitlines():

        line = raw.rstrip()

        if ":" in line:
            k, v = line.split(":", 1)

            key = k.strip().lower()

            if key in KNOWN_KEYS:
                flush()

                current_key = key
                buffer.append(
                    v.strip()
                )

                continue

        if current_key:
            buffer.append(line)

    flush()

    return meta


# ============================================================
# ERROR CLASSIFICATION
# ============================================================

def is_auth_required_error(exc):
    msg = str(exc).lower()

    return (
        "invalid_grant" in msg
        or "token has been expired or revoked" in msg
        or "invalid_client" in msg
        or "unauthorized_client" in msg
        or "insufficient authentication scopes" in msg
        or "insufficient permission" in msg
    )


def is_disk_full_error(exc):
    return (
        isinstance(exc, OSError)
        and exc.errno == errno.ENOSPC
    )


def is_temporary_network_error(exc):
    if is_disk_full_error(exc):
        return False

    if isinstance(
        exc,
        (
            TimeoutError,
            socket.timeout,
            socket.gaierror,
            ssl.SSLError,
            ConnectionResetError,
            ConnectionAbortedError,
            ConnectionRefusedError,
            BrokenPipeError,
            http.client.RemoteDisconnected,
            urllib.error.URLError,
            httplib2.HttpLib2Error,
            TransportError,
        ),
    ):
        return True

    if isinstance(exc, RefreshError):
        return not is_auth_required_error(
            exc
        )

    msg = str(exc).lower()

    keywords = (
        "timed out",
        "timeout",
        "ssl",
        "connection reset",
        "connection aborted",
        "connection refused",
        "remote end closed",
        "remote disconnected",
        "temporary failure",
        "name resolution",
        "nodename nor servname",
        "network is unreachable",
        "network unreachable",
        "broken pipe",
        "unable to find the server",
        "dns",
    )

    return any(
        k in msg
        for k in keywords
    )


def wait_seconds(retry_count):
    return min(
        30 * (
            2 ** max(
                0,
                retry_count - 1
            )
        ),
        MAX_WAIT
    )


# ============================================================
# OAUTH / SERVICES
# ============================================================

def load_or_create_creds(
    token_path,
    scopes
):
    creds = None

    if token_path.exists():
        try:
            with token_path.open(
                "rb"
            ) as f:
                creds = pickle.load(f)

        except Exception:
            creds = None

    if (
        creds
        and creds.expired
        and getattr(
            creds,
            "refresh_token",
            None
        )
    ):
        creds.refresh(Request())

    if creds:
        try:
            has_scopes = creds.has_scopes(
                scopes
            )
        except Exception:
            has_scopes = True

        if not has_scopes:
            creds = None

    if (
        not creds
        or not creds.valid
    ):
        if not CLIENT_SECRET.exists():
            raise FileNotFoundError(
                "Missing client_secret.json"
            )

        flow = (
            InstalledAppFlow
            .from_client_secrets_file(
                str(CLIENT_SECRET),
                scopes
            )
        )

        creds = flow.run_local_server(
            port=0
        )

        try:
            with token_path.open(
                "wb"
            ) as f:
                pickle.dump(
                    creds,
                    f
                )

        except OSError as e:
            if is_disk_full_error(e):
                raise DiskFull(
                    "No space left while saving OAuth token."
                )
            raise

    return creds


_THROTTLE = None
_THROTTLE_STARTED = False


def get_throttle():
    # Adaptive upstream pacing (tools/upload_throttle.py).
    # None when disabled with YT_UPLOAD_THROTTLE=off.
    global _THROTTLE, _THROTTLE_STARTED

    if not _THROTTLE_STARTED:
        _THROTTLE_STARTED = True

        try:
            _THROTTLE = upload_throttle.from_env(
                log=log,
                csv_dir=LOGS
            )

        except Exception as e:
            log(
                "Adaptive throttle unavailable; "
                "uploading unpaced. reason="
                + str(e),
                "WARN",
            )

        if _THROTTLE:
            atexit.register(_THROTTLE.stop)

    return _THROTTLE


def get_upload_service():
    creds = load_or_create_creds(
        UPLOAD_TOKEN,
        UPLOAD_SCOPES
    )

    throttle = get_throttle()

    if throttle is None:
        return build(
            "youtube",
            "v3",
            credentials=creds
        )

    return build(
        "youtube",
        "v3",
        http=google_auth_httplib2.AuthorizedHttp(
            creds,
            http=throttle.make_http()
        )
    )


def get_check_service():
    # Watchers must never launch an interactive OAuth flow.
    if not CHECK_TOKEN.exists():
        raise AuthRequired("Readonly OAuth token missing: token_check.pickle")
    with CHECK_TOKEN.open("rb") as handle:
        creds = pickle.load(handle)
    if not creds.has_scopes(CHECK_SCOPES):
        raise AuthRequired("Readonly OAuth token missing youtube.readonly scope")
    if creds.expired and creds.refresh_token:
        request = Request()
        def refresh_request(*args, **kwargs):
            kwargs.setdefault("timeout", 30)
            response = request(*args, **kwargs)
            if response.status >= 400:
                try:
                    body = json.loads(response.data)
                    safe = {key: body[key] for key in ("error", "error_description", "error_uri") if key in body}
                except (ValueError, TypeError):
                    safe = {"error": "non-JSON OAuth response"}
                log("VERIFY AUTH HTTP " + str(response.status) + " response=" + json.dumps(safe), "WARN")
            return response
        creds.refresh(refresh_request)
    if not creds.valid:
        raise AuthRequired("Readonly OAuth token invalid; interactive reauthorization required")
    return build("youtube", "v3", credentials=creds, cache_discovery=False)




def get_upload_service_with_retry():
    retry_count = 0

    while True:
        try:
            return get_upload_service()

        except DiskFull:
            raise

        except Exception as e:
            if is_auth_required_error(e):
                raise AuthRequired(
                    str(e)
                )

            if not is_temporary_network_error(
                e
            ):
                raise

            retry_count += 1

            wait_sec = wait_seconds(
                retry_count
            )

            log(
                "Network/DNS error while "
                "preparing YouTube API. "
                "Retry in "
                + str(wait_sec)
                + " sec. reason="
                + str(e),
                "WARN",
            )

            time.sleep(wait_sec)


# ============================================================
# SESSION FILE
# ============================================================

def session_path(folder):
    return (
        folder
        / "upload_session.json"
    )


def video_signature(video_file):
    st = video_file.stat()

    return {
        "video_name":
            video_file.name,

        "file_size":
            st.st_size,

        "file_mtime_ns":
            st.st_mtime_ns,

        "chunk_size":
            UPLOAD_CHUNK_SIZE,
    }


def save_session(
    folder,
    video_file,
    req
):
    if not getattr(
        req,
        "resumable_uri",
        None
    ):
        return

    data = video_signature(
        video_file
    )

    data.update(
        {
            "session_uri":
                req.resumable_uri,

            "last_known_bytes":
                int(
                    getattr(
                        req,
                        "resumable_progress",
                        0
                    )
                    or 0
                ),

            "updated_at":
                datetime.now().isoformat(
                    timespec="seconds"
                ),
        }
    )

    path = session_path(folder)

    tmp = folder / (
        "upload_session.json.tmp"
    )

    try:
        tmp.write_text(
            json.dumps(
                data,
                indent=2
            ),
            encoding="utf-8"
        )

        tmp.replace(path)

    except OSError as e:
        if is_disk_full_error(e):
            # Important:
            # old upload_session.json remains intact.
            raise DiskFull(
                "No space left while saving upload session."
            )

        raise


def delete_session(folder):
    path = session_path(folder)

    if path.exists():
        path.unlink()


def load_session(
    folder,
    video_file
):
    path = session_path(folder)

    if not path.exists():
        return None

    try:
        data = json.loads(
            path.read_text(
                encoding="utf-8"
            )
        )

    except Exception as e:
        log(
            "Invalid upload_session.json. "
            "Starting new session. reason="
            + str(e),
            "WARN",
        )

        path.unlink(
            missing_ok=True
        )

        return None

    sig = video_signature(
        video_file
    )

    for key in (
        "video_name",
        "file_size",
        "file_mtime_ns",
        "chunk_size",
    ):
        if data.get(key) != sig[key]:
            log(
                "Video changed since session "
                "was created. "
                "Discarding old session.",
                "WARN",
            )

            path.unlink(
                missing_ok=True
            )

            return None

    if not data.get(
        "session_uri"
    ):
        path.unlink(
            missing_ok=True
        )

        return None

    return data


# ============================================================
# VIDEO ID FILE
# ============================================================

def video_id_path(folder):
    return (
        folder
        / "youtube_video_id.txt"
    )


def save_video_id(
    folder,
    video_id
):
    try:
        video_id_path(
            folder
        ).write_text(
            video_id + "\n",
            encoding="utf-8"
        )

    except OSError as e:
        if is_disk_full_error(e):
            raise DiskFull(
                "No space left while saving video ID."
            )

        raise


def load_video_id(folder):
    p = video_id_path(folder)

    if not p.exists():
        return None

    try:
        value = p.read_text(
            encoding="utf-8"
        ).strip()

        return value or None

    except Exception:
        return None


# ============================================================
# LOW-LEVEL SESSION STATUS
# ============================================================

def http_status(resp):
    return int(
        getattr(
            resp,
            "status",
            0
        )
    )


def maybe_raise_direct_http(
    resp,
    content,
    uri
):
    status = http_status(resp)

    if status in RETRYABLE_HTTP:
        raise HttpError(
            resp,
            content,
            uri=uri
        )

    if status in SESSION_EXPIRED_HTTP:
        raise SessionExpired(
            "Upload session expired HTTP "
            + str(status)
        )

    if status >= 400:
        raise HttpError(
            resp,
            content,
            uri=uri
        )


# ============================================================
# CREATE SESSION
# ============================================================

def initiate_session(
    req,
    folder,
    video_file
):
    retry_count = 0

    size = video_file.stat().st_size

    while True:
        try:
            headers = dict(
                req.headers
            )

            headers[
                "X-Upload-Content-Type"
            ] = req.resumable.mimetype()

            headers[
                "X-Upload-Content-Length"
            ] = str(size)

            headers[
                "content-length"
            ] = str(req.body_size)

            resp, content = (
                req.http.request(
                    str(req.uri),
                    method=str(
                        req.method
                    ),
                    body=req.body,
                    headers=headers,
                )
            )

            status = http_status(
                resp
            )

            if (
                status == 200
                and "location" in resp
            ):
                req.resumable_uri = (
                    resp["location"]
                )

                req.resumable_progress = 0

                save_session(
                    folder,
                    video_file,
                    req
                )

                log(
                    "New resumable upload "
                    "session created and saved."
                )

                return

            maybe_raise_direct_http(
                resp,
                content,
                str(req.uri)
            )

            raise RuntimeError(
                "Could not create upload "
                "session. HTTP "
                + str(status)
            )

        except DiskFull:
            raise

        except HttpError as e:
            status = int(
                getattr(
                    e.resp,
                    "status",
                    0
                )
            )

            if status not in RETRYABLE_HTTP:
                raise

            err = e

        except Exception as e:
            if is_auth_required_error(e):
                raise AuthRequired(
                    str(e)
                )

            if not is_temporary_network_error(
                e
            ):
                raise

            err = e

        retry_count += 1

        wait_sec = wait_seconds(
            retry_count
        )

        log(
            "Upload session creation paused "
            "by temporary error. Retry in "
            + str(wait_sec)
            + " sec. reason="
            + str(err),
            "WARN",
        )

        time.sleep(wait_sec)


# ============================================================
# QUERY EXISTING SESSION
# ============================================================

def sync_existing_session(
    req,
    folder,
    video_file
):
    retry_count = 0

    size = video_file.stat().st_size

    while True:
        try:
            headers = {
                "Content-Range":
                    "bytes */"
                    + str(size),

                "Content-Length":
                    "0",
            }

            resp, content = (
                req.http.request(
                    req.resumable_uri,
                    method="PUT",
                    body=b"",
                    headers=headers,
                )
            )

            status = http_status(
                resp
            )

            # Official completed-session response
            if status in (
                200,
                201,
            ):
                if isinstance(
                    content,
                    bytes
                ):
                    content = content.decode(
                        "utf-8"
                    )

                try:
                    body = (
                        json.loads(content)
                        if content
                        else {}
                    )

                except Exception:
                    body = {}

                log(
                    "Upload session reports "
                    "COMPLETE."
                )

                # DO NOT DELETE SESSION HERE.
                return body

            if status == 308:
                rng = resp.get(
                    "range"
                )

                if (
                    rng
                    and "-" in rng
                ):
                    req.resumable_progress = (
                        int(
                            rng.split(
                                "-"
                            )[-1]
                        )
                        + 1
                    )

                else:
                    req.resumable_progress = 0

                if "location" in resp:
                    req.resumable_uri = (
                        resp["location"]
                    )

                save_session(
                    folder,
                    video_file,
                    req
                )

                pct = (
                    req.resumable_progress
                    / size
                    * 100.0
                ) if size else 0

                log(
                    "Existing upload "
                    "session found."
                )

                log(
                    "YouTube has received "
                    + f"{pct:.1f}%"
                    + " ("
                    + str(
                        req.resumable_progress
                    )
                    + "/"
                    + str(size)
                    + " bytes)."
                )

                log(
                    "Resuming "
                    + folder.name
                    + " from "
                    + f"{pct:.1f}%"
                )

                return None

            maybe_raise_direct_http(
                resp,
                content,
                req.resumable_uri
            )

            raise RuntimeError(
                "Unexpected upload session "
                "status HTTP "
                + str(status)
            )

        except DiskFull:
            raise

        except SessionExpired:
            raise

        except HttpError as e:
            status = int(
                getattr(
                    e.resp,
                    "status",
                    0
                )
            )

            if (
                status
                in SESSION_EXPIRED_HTTP
            ):
                raise SessionExpired(
                    "Upload session expired "
                    "HTTP "
                    + str(status)
                )

            if (
                status
                not in RETRYABLE_HTTP
            ):
                raise

            err = e

        except Exception as e:
            if is_auth_required_error(e):
                raise AuthRequired(
                    str(e)
                )

            if not is_temporary_network_error(
                e
            ):
                raise

            err = e

        retry_count += 1

        wait_sec = wait_seconds(
            retry_count
        )

        log(
            "Cannot query upload session. "
            "Session preserved. Retry in "
            + str(wait_sec)
            + " sec. reason="
            + str(err),
            "WARN",
        )

        time.sleep(wait_sec)


# ============================================================
# VERIFY FINAL VIDEO RESOURCE
# ============================================================

def verify_video_exists(video_id, max_attempts=1):
    # One attempt per run; persistent backoff is handled by the project wrapper.
    try:
        service = get_check_service()
        response = service.videos().list(
            part="snippet,status,processingDetails", id=video_id
        ).execute()
        for video in response.get("items", []):
            if video.get("id") == video_id:
                log("VERIFY SUCCESS video_id=" + video_id)
                return video
        raise VerificationPending(
            "NOT_VISIBLE HTTP 200 response=" + json.dumps(response, ensure_ascii=False)
        )
    except VerificationPending:
        raise
    except HttpError as exc:
        body = exc.content.decode("utf-8", errors="replace") if isinstance(exc.content, bytes) else str(exc.content)
        raise VerificationPending(
            "API_ERROR HTTP " + str(getattr(exc.resp, "status", "unknown")) + " response=" + body
        ) from exc
    except RefreshError as exc:
        raise VerificationPending("AUTH_REFRESH_ERROR response=" + str(exc)) from exc
    except Exception as exc:
        raise VerificationPending(type(exc).__name__ + ": " + str(exc)) from exc




# ============================================================
# THUMBNAIL
# ============================================================

def upload_thumbnail_if_exists(
    service,
    video_id,
    folder
):
    thumb = find_thumbnail_file(
        folder
    )

    if not thumb:
        log(
            "No thumbnail.* found. "
            "Skip thumbnail."
        )

        return True

    retry_count = 0

    while retry_count < 6:
        try:
            log(
                "Uploading thumbnail: "
                + thumb.name
            )

            (
                service
                .thumbnails()
                .set(
                    videoId=video_id,
                    media_body=(
                        MediaFileUpload(
                            str(thumb)
                        )
                    )
                )
                .execute()
            )

            log(
                "Thumbnail upload OK."
            )

            return True

        except HttpError as e:
            status = int(
                getattr(
                    e.resp,
                    "status",
                    0
                )
            )

            # videoNotFound can be temporary immediately
            # after video creation.
            if (
                status == 404
                or status in RETRYABLE_HTTP
            ):
                retry_count += 1

                wait_sec = wait_seconds(
                    retry_count
                )

                log(
                    "Thumbnail temporarily "
                    "unavailable HTTP "
                    + str(status)
                    + ". Retry in "
                    + str(wait_sec)
                    + " sec.",
                    "WARN",
                )

                time.sleep(
                    wait_sec
                )

                continue

            log(
                "Thumbnail failed permanently: "
                + str(e),
                "WARN",
            )

            return False

        except Exception as e:
            if is_temporary_network_error(
                e
            ):
                retry_count += 1

                wait_sec = wait_seconds(
                    retry_count
                )

                log(
                    "Thumbnail network error. "
                    "Retry in "
                    + str(wait_sec)
                    + " sec. reason="
                    + str(e),
                    "WARN",
                )

                time.sleep(
                    wait_sec
                )

                continue

            log(
                "Thumbnail failed: "
                + str(e),
                "WARN",
            )

            return False

    log(
        "Thumbnail still failed after retries. "
        "Video itself is verified, so continue.",
        "WARN",
    )

    return False


# ============================================================
# FINALIZE
# ============================================================

def finalize_verified_video(upload_service, folder, video_id):
    verify_video_exists(video_id)
    if find_thumbnail_file(folder):
        if upload_service is None:
            upload_service = get_upload_service()
        if not upload_thumbnail_if_exists(upload_service, video_id, folder):
            log("Thumbnail was not applied; video itself verified", "WARN")
    delete_session(folder)
    return video_id




# ============================================================
# BUILD INSERT REQUEST
# ============================================================

class AdaptiveChunkUpload(MediaFileUpload):
    # next_chunk() reads chunksize() several times per call,
    # so the size may only change between calls.
    def set_chunksize(self, chunksize):
        self._chunksize = int(chunksize)


def make_upload_request(
    service,
    video_file,
    body
):
    media = AdaptiveChunkUpload(
        str(video_file),
        chunksize=UPLOAD_CHUNK_SIZE,
        resumable=True,
    )

    return (
        service
        .videos()
        .insert(
            part="snippet,status",
            body=body,
            media_body=media,
        )
    )


# ============================================================
# UPLOAD
# ============================================================

def upload_one(folder):
    if video_id_path(folder).exists():
        video_id = load_video_id(folder)
        if not video_id:
            raise VerificationPending("Unreadable/empty youtube_video_id.txt; automatic upload blocked")
        log("VERIFY_ONLY video_id=" + video_id)
        return finalize_verified_video(None, folder, video_id)

    meta_file = (
        folder
        / "meta.txt"
    )

    video_file = find_video_file(
        folder
    )

    if not video_file:
        raise FileNotFoundError(
            "video file missing"
        )

    meta = parse_meta(meta_file) if meta_file.exists() else {}

    title = meta.get("title") or video_file.stem[:100] or folder.name[:100]

    description = meta.get(
        "description",
        ""
    )

    privacy = meta.get(
        "privacy",
        DEFAULT_PRIVACY if meta_file.exists() else "private"
    )

    category_id = meta.get(
        "categoryid",
        DEFAULT_CATEGORY_ID
    )

    tags = []

    if (
        "tags" in meta
        and meta["tags"].strip()
    ):
        tags = [
            t.strip()
            for t
            in meta["tags"].split(",")
            if t.strip()
        ]

    log(
        "META title="
        + title
    )

    log(
        "META description length="
        + str(
            len(description)
        )
    )

    log(
        "Uploading video: "
        + folder.name
    )

    body = {
        "snippet": {
            "title":
                title,

            "description":
                description,

            "categoryId":
                category_id,

            "defaultLanguage":
                DEFAULT_LANGUAGE,

            "defaultAudioLanguage":
                DEFAULT_LANGUAGE,
        },

        "status": {
            "privacyStatus":
                privacy,
        },
    }

    if tags:
        body[
            "snippet"
        ][
            "tags"
        ] = tags

    service = (
        get_upload_service_with_retry()
    )

    # --------------------------------------------------------
    # Session lifecycle
    # --------------------------------------------------------

    while True:
        req = make_upload_request(
            service,
            video_file,
            body
        )

        saved = load_session(
            folder,
            video_file
        )

        if saved:
            req.resumable_uri = (
                saved[
                    "session_uri"
                ]
            )

            req.resumable_progress = int(
                saved.get(
                    "last_known_bytes",
                    0
                )
                or 0
            )

            try:
                completed_response = (
                    sync_existing_session(
                        req,
                        folder,
                        video_file
                    )
                )

            except SessionExpired:
                # Before restarting from zero,
                # try saved video_id one last time.
                existing_video_id = (
                    load_video_id(
                        folder
                    )
                )

                if existing_video_id:
                    try:
                        return (
                            finalize_verified_video(
                                service,
                                folder,
                                existing_video_id
                            )
                        )

                    except VerificationPending:
                        raise

                log(
                    "Upload session expired and "
                    "no verified video exists. "
                    "Starting new session from 0%.",
                    "WARN",
                )

                delete_session(
                    folder
                )

                # Remove stale ghost ID before
                # a completely new upload.
                try:
                    video_id_path(
                        folder
                    ).unlink(
                        missing_ok=True
                    )
                except Exception:
                    pass

                continue

            if completed_response is not None:
                video_id = (
                    completed_response.get(
                        "id"
                    )
                )

                if not video_id:
                    video_id = load_video_id(
                        folder
                    )

                if not video_id:
                    raise VerificationPending(
                        "Session reports complete "
                        "but completion response "
                        "contains no video ID. "
                        "Session preserved."
                    )

                save_video_id(
                    folder,
                    video_id
                )

                return finalize_verified_video(
                    service,
                    folder,
                    video_id
                )

        else:
            initiate_session(
                req,
                folder,
                video_file
            )

        # ----------------------------------------------------
        # Transfer chunks
        # ----------------------------------------------------

        resp = None

        retry_count = 0

        last_logged_bucket = -1

        throttle = get_throttle()

        if throttle:
            # Learn the idle latency baseline before sending bytes.
            if not throttle.wait_ready():
                log(
                    "Throttle could not learn a latency "
                    "baseline; pacing at the start rate.",
                    "WARN",
                )

        while resp is None:
            try:
                if throttle:
                    req.resumable.set_chunksize(
                        throttle.chunk_bytes()
                    )

                status, resp = (
                    req.next_chunk(
                        num_retries=0
                    )
                )

                # Preserve URI/progress even after
                # final chunk response.
                save_session(
                    folder,
                    video_file,
                    req
                )

                retry_count = 0

                if status:
                    pct_float = (
                        status.progress()
                        * 100.0
                    )

                    bucket = int(
                        pct_float // 5
                    )

                    if (
                        bucket
                        != last_logged_bucket
                    ):
                        last_logged_bucket = (
                            bucket
                        )

                        log(
                            "Progress: "
                            + f"{pct_float:.1f}%"
                            + (
                                " | " + throttle.status()
                                if throttle
                                else ""
                            )
                        )

            except DiskFull:
                raise

            except HttpError as e:
                status_code = int(
                    getattr(
                        e.resp,
                        "status",
                        0
                    )
                )

                if (
                    status_code
                    in SESSION_EXPIRED_HTTP
                ):
                    try:
                        save_session(
                            folder,
                            video_file,
                            req
                        )
                    except DiskFull:
                        raise

                    log(
                        "Upload session expired "
                        "during transfer.",
                        "WARN",
                    )

                    delete_session(
                        folder
                    )

                    break

                if (
                    status_code
                    not in RETRYABLE_HTTP
                ):
                    raise

                req._in_error_state = True

                save_session(
                    folder,
                    video_file,
                    req
                )

                retry_count += 1

                wait_sec = wait_seconds(
                    retry_count
                )

                log(
                    "Temporary HTTP "
                    + str(status_code)
                    + ". Session preserved. "
                    "Retry in "
                    + str(wait_sec)
                    + " sec.",
                    "WARN",
                )

                time.sleep(
                    wait_sec
                )

            except Exception as e:
                if is_disk_full_error(e):
                    raise DiskFull(
                        str(e)
                    )

                if is_auth_required_error(
                    e
                ):
                    try:
                        save_session(
                            folder,
                            video_file,
                            req
                        )
                    except Exception:
                        pass

                    raise AuthRequired(
                        str(e)
                    )

                if not is_temporary_network_error(
                    e
                ):
                    raise

                save_session(
                    folder,
                    video_file,
                    req
                )

                req._in_error_state = True

                retry_count += 1

                wait_sec = wait_seconds(
                    retry_count
                )

                log(
                    "Upload paused due to "
                    "network/DNS error. "
                    "Session preserved. "
                    "Folder remains in prepared/"
                    + folder.name
                    + ". Retry in "
                    + str(wait_sec)
                    + " sec. reason="
                    + str(e),
                    "WARN",
                )

                time.sleep(
                    wait_sec
                )

        if resp is None:
            continue

        # ----------------------------------------------------
        # Final server response
        # ----------------------------------------------------

        video_id = resp.get(
            "id"
        )

        if not video_id:
            raise VerificationPending(
                "Upload reached final response "
                "but response contained no "
                "video ID. Session preserved."
            )

        # IMPORTANT:
        # save ID BEFORE verification.
        save_video_id(
            folder,
            video_id
        )

        log(
            "Upload bytes complete. "
            "Server returned video_id="
            + video_id
            + ". NOT marking SUCCESS yet."
        )

        # Critical difference from old version:
        #
        # NO delete_session()
        # NO done/
        #
        # until video resource exists.
        return finalize_verified_video(
            service,
            folder,
            video_id
        )


# ============================================================
# FAILED
# ============================================================

def move_to_failed(
    folder,
    error_text
):
    try:
        (
            folder
            / "error.txt"
        ).write_text(
            error_text,
            encoding="utf-8"
        )

    except OSError as e:
        if is_disk_full_error(e):
            raise DiskFull(
                "Disk full while writing error.txt"
            )

        raise

    if video_id_path(folder).exists():
        record_verification_pending(folder, error_text)
        return
    log("FATAL project=" + folder.name + " reason=" + error_text, "ERROR")
    name = folder.name

    dest = FAILED / name

    if dest.exists():
        dest = FAILED / (
            name
            + "_"
            + datetime.now().strftime(
                "%Y%m%d_%H%M%S"
            )
        )

    with paths_lock(exclusive=True):
        shutil.move(str(folder), str(dest))

    log(
        "FAILED moved to failed/"
        + dest.name,
        "ERROR",
    )


# ============================================================
# MAIN
# ============================================================

def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--verify-only", type=Path, help="Verify this prepared/failed project; never upload bytes")
    args = parser.parse_args()

    for d in (
        PREPARED,
        DONE,
        FAILED,
        LOGS,
        SECRETS,
    ):
        d.mkdir(
            parents=True,
            exist_ok=True
        )

    lock_fd = acquire_lock()

    try:
        if args.verify_only:
            requested = args.verify_only.expanduser().resolve()
            if requested.parent not in (PREPARED.resolve(), FAILED.resolve()) or not video_id_path(requested).exists():
                raise ValueError("--verify-only requires a prepared/failed project with youtube_video_id.txt")
        recover_verified_candidates()
        folder = (requested if requested.exists() else PREPARED / requested.name) if args.verify_only else pick_ready_folder()

        if not folder:
            return

        name = folder.name

        try:
            if video_id_path(folder).exists():
                log(f"VERIFY RETRY video_id={load_video_id(folder)} project={name}")
            else:
                log("UPLOAD STARTED " + folder.name)
            video_id = upload_one(
                folder
            )

            # SUCCESS now means:
            #
            # 1. all bytes uploaded
            # 2. video ID returned
            # 3. videos.list confirms actual resource
            #
            # Session has already been removed ONLY
            # after verification.

            dest = DONE / name

            if dest.exists():
                dest = DONE / (
                    name
                    + "_"
                    + datetime.now().strftime(
                        "%Y%m%d_%H%M%S"
                    )
                )

            with paths_lock(exclusive=True):
                shutil.move(str(folder), str(dest))

            (dest / ".verify_pending.json").unlink(missing_ok=True)

            log(
                "SUCCESS VERIFIED "
                "video_id="
                + video_id
                + " moved to done/"
                + dest.name
            )

        except KeyboardInterrupt:
            log(
                "Stopped by user. "
                "Session preserved. "
                "Folder remains in prepared/"
                + name,
                "WARN",
            )

            raise

        except DiskFull as e:
            if video_id_path(folder).exists():
                record_verification_pending(folder, e)
                return
            log(
                "DISK FULL. "
                "Upload paused. "
                "Session preserved. "
                "Folder remains in prepared/"
                + name
                + ". reason="
                + str(e),
                "WARN",
            )

            return

        except VerificationPending as e:
            record_verification_pending(folder, e)
            return

        except TemporaryPause as e:
            if video_id_path(folder).exists():
                record_verification_pending(folder, e)
                return
            log(
                "Temporary pause. "
                "Session preserved. "
                "Folder remains in prepared/"
                + name
                + ". reason="
                + str(e),
                "WARN",
            )

            return

        except AuthRequired as e:
            if video_id_path(folder).exists():
                record_verification_pending(folder, e)
                return
            log(
                "AUTH REQUIRED. "
                "Session preserved. "
                "Folder remains in prepared/"
                + name
                + ". reason="
                + str(e),
                "ERROR",
            )

            return

        except Exception as e:
            if video_id_path(folder).exists():
                record_verification_pending(folder, e)
                return
            err = traceback.format_exc()

            if is_disk_full_error(e):
                log(
                    "DISK FULL. "
                    "Folder remains in prepared/"
                    + name,
                    "WARN",
                )

                return

            if is_temporary_network_error(
                e
            ):
                log(
                    "Temporary network error "
                    "escaped upload loop. "
                    "Session preserved. "
                    "Folder remains in prepared/"
                    + name,
                    "WARN",
                )

                return

            move_to_failed(
                folder,
                err
            )

    finally:
        try:
            fcntl.flock(
                lock_fd,
                fcntl.LOCK_UN
            )

        except Exception:
            pass

        lock_fd.close()


if __name__ == "__main__":
    main()
