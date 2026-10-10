#!/usr/bin/env python3
"""Post-upload package readiness and resumable metadata/thumbnail updates.
Default is strictly read-only. --run records local readiness. Remote writes also
require --apply or the queue's .publishing_updates_enabled opt-in file.
"""
import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import pickle
import re
import subprocess
import sys
import tempfile
import unicodedata
import time
from datetime import datetime

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import yt_autoupload as uploader
from tools.pipeline_state import paths_lock

BASE = uploader.BASE
UPDATE_SCOPES = ['https://www.googleapis.com/auth/youtube.force-ssl']
TOKEN_NAME = 'token_update.pickle'
SIPS = '/usr/bin/sips'


def read_json(path):
    try:
        value = json.loads(path.read_text())
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def save_json(path, value):
    fd, name = tempfile.mkstemp(prefix='.publishing.', dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as handle:
            json.dump(value, handle, indent=2)
            handle.write('\n')
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def regular(path):
    if path.is_symlink() or not path.is_file():
        raise ValueError(f'WAIT regular file: {path.name}')
    return path.read_bytes()


def validate_image(path, raw):
    # Preserve the uploader's JPG/JPEG/PNG/WebP selection convention. Use the
    # macOS image decoder (/usr/bin/sips) when present; Pillow (optional, imported
    # lazily) is used only where sips is absent.
    if not 0 < len(raw) <= 50 * 1024 * 1024:
        raise ValueError('WAIT thumbnail size must be 1 byte to 50 MiB')
    if not os.path.exists(SIPS):
        return _validate_image_pillow(path)
    result = subprocess.run([SIPS, '-g', 'format', '-g', 'pixelWidth',
                             '-g', 'pixelHeight', str(path)],
                            capture_output=True, text=True, timeout=15)
    fields = dict(re.findall(r'^\s*(format|pixelWidth|pixelHeight):\s*(\S+)', result.stdout, re.M))
    if result.returncode or fields.get('format') not in ('jpeg', 'png', 'webp'):
        raise ValueError('WAIT thumbnail is not a decodable JPEG/PNG/WebP image')
    try:
        valid = int(fields['pixelWidth']) > 0 and int(fields['pixelHeight']) > 0
    except (KeyError, ValueError):
        valid = False
    if not valid:
        raise ValueError('WAIT thumbnail has invalid image dimensions')


def _validate_image_pillow(path):
    # Fallback only where macOS sips is absent (e.g. Linux); same contract and messages.
    try:
        from PIL import Image
    except ImportError:
        raise ValueError(f'WAIT thumbnail decoder unavailable ({SIPS} missing and Pillow not installed)')
    try:
        with Image.open(path) as image:
            image.load()
            fmt, width, height = (image.format or '').lower(), image.width, image.height
    except Exception:
        raise ValueError('WAIT thumbnail is not a decodable JPEG/PNG/WebP image')
    if fmt not in ('jpeg', 'png', 'webp'):
        raise ValueError('WAIT thumbnail is not a decodable JPEG/PNG/WebP image')
    if not (width > 0 and height > 0):
        raise ValueError('WAIT thumbnail has invalid image dimensions')


def validate_meta(meta):
    """Shared metadata validation (automatic readiness and manual apply). Returns the tag list."""
    title = meta.get('title', '')
    description = meta.get('description', '')
    if not title.strip() or len(title) > 100 or '\n' in title or '<' in title or '>' in title:
        raise ValueError('WAIT valid metadata title (single line, 1-100 characters)')
    if not description.strip() or len(description.encode('utf-8')) > 5000 or '<' in description or '>' in description:
        raise ValueError('WAIT valid metadata description (1-5000 UTF-8 bytes)')
    if meta.get('privacy', 'unlisted') not in ('private', 'unlisted', 'public'):
        raise ValueError('WAIT valid metadata privacy')
    if 'categoryid' in meta and not meta['categoryid'].isdigit():
        raise ValueError('WAIT numeric categoryid')
    tags = [tag.strip() for tag in meta.get('tags', '').split(',') if tag.strip()]
    if sum(len(t) + (2 if ' ' in t else 0) for t in tags) + max(0, len(tags) - 1) > 500:
        raise ValueError('WAIT metadata tags exceed 500 characters')
    return tags


def build_snippet(remote_snippet, meta, tags):
    """videos.update(part=snippet) replaces every mutable snippet property it omits, so start from the
    live resource and change only title/description (+tags/categoryId when meta.txt supplies them)."""
    snippet = {k: v for k, v in remote_snippet.items() if k in
               ('title', 'description', 'tags', 'categoryId', 'defaultLanguage', 'defaultAudioLanguage')}
    snippet.update(title=meta['title'], description=meta['description'])
    if 'tags' in meta:
        snippet['tags'] = tags
    if 'categoryid' in meta:
        snippet['categoryId'] = meta['categoryid']
    if not snippet.get('categoryId'):
        raise ValueError('Remote categoryId missing; refusing incomplete snippet update')
    return snippet


def snapshot(folder):
    if folder.is_symlink() or folder.parent != BASE / 'done':
        raise ValueError('WAIT verified project in done/')
    video_id = regular(folder / 'youtube_video_id.txt').decode().strip()
    if not re.fullmatch(r'[A-Za-z0-9_-]{11}', video_id):
        raise ValueError('WAIT valid saved YouTube video ID')
    meta_path = folder / 'meta.txt'
    raw = regular(meta_path)
    meta = uploader.parse_meta(meta_path)
    tags = validate_meta(meta)
    thumb = uploader.find_thumbnail_file(folder)
    if thumb is None:
        raise ValueError('WAIT thumbnail (existing thumbnail.* / Thumbnail… convention)')
    image = regular(thumb)
    validate_image(thumb, image)
    if regular(meta_path) != raw or regular(thumb) != image or regular(folder / 'youtube_video_id.txt').decode().strip() != video_id:
        raise ValueError('WAIT files changed during validation')
    identity = {'video_id': video_id, 'meta_sha256': hashlib.sha256(raw).hexdigest(),
                'thumbnail': thumb.name, 'thumbnail_sha256': hashlib.sha256(image).hexdigest()}
    fingerprint = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
    return dict(identity, fingerprint=fingerprint, meta=meta, tags=tags, image=image,
                newest_mtime=max(meta_path.stat().st_mtime, thumb.stat().st_mtime))


def update_service():
    # No interactive OAuth in scheduled workers; never replace upload/check tokens.
    path = BASE / 'secrets' / TOKEN_NAME
    if not path.is_file() or path.is_symlink():
        raise uploader.AuthRequired('Update OAuth required: run publishing.py --authorize interactively')
    with path.open('rb') as handle:
        creds = pickle.load(handle)
    if not creds.has_scopes(UPDATE_SCOPES):
        raise uploader.AuthRequired('Update OAuth lacks youtube.force-ssl')
    if creds.expired and creds.refresh_token:
        request = uploader.Request()
        creds.refresh(lambda *a, **kw: request(*a, **dict(kw, timeout=30)))
    if not creds.valid:
        raise uploader.AuthRequired('Update OAuth invalid; interactive authorization required')
    return uploader.build('youtube', 'v3', credentials=creds, cache_discovery=False)


def unchanged(folder, package):
    if snapshot(folder)['fingerprint'] != package['fingerprint']:
        raise ValueError('Package changed; retry with the new package')


def apply_package(folder, package, state, service=None):
    service = update_service() if service is None else service
    video_id = package['video_id']
    # Verify the exact existing resource before each update attempt. This module
    # has no videos.insert, resumable session, or video-byte upload path.
    result = service.videos().list(part='snippet', id=video_id).execute()
    remote = next((v for v in result.get('items', []) if v.get('id') == video_id), None)
    if remote is None:
        raise uploader.VerificationPending('Existing video not visible to update credentials')
    state_path = folder / '.publishing_state.json'
    if not state.get('metadata_applied'):
        unchanged(folder, package)
        snippet = build_snippet(remote['snippet'], package['meta'], package['tags'])
        response = service.videos().update(part='snippet', body={'id': video_id, 'snippet': snippet}).execute()
        if response.get('id') != video_id:
            raise ValueError('Metadata update returned unexpected video ID')
        state['metadata_applied'] = True
        save_json(state_path, state)
    if not state.get('thumbnail_applied'):
        unchanged(folder, package)
        # Upload the validated bytes, not a possibly edited live image path.
        with tempfile.TemporaryDirectory(prefix='yt-thumbnail-') as tmp:
            frozen = Path(tmp) / package['thumbnail']
            frozen.write_bytes(package['image'])
            response = service.thumbnails().set(videoId=video_id,
                media_body=uploader.MediaFileUpload(str(frozen))).execute()
        if not response.get('items'):
            raise ValueError('Thumbnail update returned no thumbnail result')
        state['thumbnail_applied'] = True
        save_json(state_path, state)
    unchanged(folder, package)
    state.update(status='APPLIED', applied_at=time.time(), next_retry_at=0, last_error=None)
    save_json(state_path, state)
    return 'ALL_READY / APPLIED'


def process(folder, run=False, apply=False, service=None, now=None):
    now = time.time() if now is None else now
    marker = folder / 'ALL_READY'
    try:
        package = snapshot(folder)
    except (OSError, ValueError, UnicodeError, subprocess.SubprocessError) as exc:
        if run:
            marker.unlink(missing_ok=True)
        return str(exc)
    fingerprint = package['fingerprint']
    state_path = folder / '.publishing_state.json'
    state = read_json(state_path)
    same = state.get('fingerprint') == fingerprint
    candidate = read_json(folder / '.publishing_candidate.json')
    stable = (candidate.get('fingerprint') == fingerprint and
              now - candidate.get('first_seen', now) >= 60 and now - package['newest_mtime'] >= 120)
    if not run:
        status = state.get('status', 'PENDING') if same else 'PENDING'
        return f'PACKAGE VALID / {status} / {"stable" if stable else "awaiting stability"} / thumbnail={package["thumbnail"]}'
    if not stable:
        marker.unlink(missing_ok=True)
        if candidate.get('fingerprint') != fingerprint:
            save_json(folder / '.publishing_candidate.json', {'fingerprint': fingerprint, 'first_seen': now})
        return 'WAIT package stability (60s between observations; file age 120s)'
    if not same:
        state = {'fingerprint': fingerprint, 'video_id': package['video_id'], 'status': 'PENDING',
                 'metadata_applied': False, 'thumbnail_applied': False}
        save_json(state_path, state)
    readiness = {k: package[k] for k in ('fingerprint', 'video_id', 'meta_sha256', 'thumbnail', 'thumbnail_sha256')}
    readiness['status'] = 'ALL_READY'
    if read_json(marker) != readiness:
        save_json(marker, readiness)
    if state.get('status') == 'APPLIED':
        return 'ALL_READY / APPLIED (unchanged; skipped)'
    if not apply:
        return 'ALL_READY / UPDATE DISABLED'
    if now < state.get('next_retry_at', 0):
        return 'ALL_READY / UPDATE RETRY PENDING'
    # Persist a lease before network work so a crash cannot cause tight retries.
    state.update(status='PENDING', next_retry_at=now + 3600)
    save_json(state_path, state)
    try:
        return apply_package(folder, package, state, service)
    except Exception as exc:
        state.update(status='PENDING', last_error=f'{type(exc).__name__}: {exc}')
        save_json(state_path, state)
        try:
            unchanged(folder, package)
        except Exception:
            marker.unlink(missing_ok=True)
        return f'ALL_READY update pending: {state["last_error"]}'


# ============================================================================
# MANUAL publish workflow:  publishing.py --project NAME --dry-run | --apply
# Incremental ASSET SYNCHRONIZER for an ALREADY-UPLOADED video. READY (an empty file named exactly `READY`) means:
# "any finalized publishing assets currently present in this project are approved to be synchronized to the existing
# YouTube video". Four independent groups: metadata (meta.txt), thumbnail, subtitle (subs/final.srt) and
# youtube_settings.txt. Each group is applied only when present, valid and new/changed versus the per-group state;
# a missing or invalid group never blocks the others. Never uploads video bytes (no videos.insert / resumable path).
# ============================================================================
READY_NAME = 'READY'
APPLIED_NAME = '.applied'
MANUAL_STATE_NAME = '.manual_apply_state.json'
STATE_VERSION = 2
MANUAL_THUMB_EXTS = ('.jpg', '.jpeg', '.png')   # thumbnails.set accepts image/jpeg and image/png
SUBTITLE_NAME = 'subs/final.srt'            # the ONLY subtitle source (never en.srt/hybrid.srt/youtube.srt/ASR)
SUBTITLE_LANGUAGE = 'en'
SETTINGS_NAME = 'youtube_settings.txt'
MAX_SUBTITLE_BYTES = 5 * 1024 * 1024
CAPTION_TOKEN_NAME = 'token_caption.pickle'   # existing captionfetch token (scope youtube.force-ssl) is reused
GROUPS = ('metadata', 'thumbnail', 'subtitle', 'settings')
LABELS = {'metadata': 'METADATA', 'thumbnail': 'THUMBNAIL', 'subtitle': 'SUBTITLE', 'settings': 'YOUTUBE_SETTINGS'}
PLAN_NAMES = {'metadata': 'metadata', 'thumbnail': 'thumbnail', 'subtitle': 'subtitles', 'settings': 'youtube_settings'}


class AssetMissing(ValueError):
    pass


class AssetAmbiguous(ValueError):
    pass


# --- youtube_settings.txt: every field classified against the official YouTube Data API v3 reference -------------
API_WRITABLE, NOT_EXPOSED, MANUAL = 'API_WRITABLE', 'NOT_EXPOSED', 'MANUAL'
IDENTITY_KEYS = ('PROJECT_ID',)     # not a YouTube setting: must equal the project folder name or the file is refused
SETTING_RULES = {
    'PLAYLIST': (API_WRITABLE, 'playlistItems.insert (separate endpoint; playlist matched by exact title, never created)'),
    'TITLE_DESCRIPTION_LANGUAGE': (API_WRITABLE, 'videos.update snippet.defaultLanguage'),
    'CATEGORY': (API_WRITABLE, 'videos.update snippet.categoryId'),
    'MADE_FOR_KIDS': (API_WRITABLE, 'videos.update status.selfDeclaredMadeForKids'),
    'AI_DISCLOSURE': (API_WRITABLE, 'videos.update status.containsSyntheticMedia'),
    'LICENSE': (API_WRITABLE, 'videos.update status.license'),
    'EMBEDDING_ALLOWED': (API_WRITABLE, 'videos.update status.embeddable'),
    'RECORDING_DATE': (API_WRITABLE, 'videos.update recordingDetails.recordingDate'),
    'VIDEO_LANGUAGE': (NOT_EXPOSED, 'Studio "video language" = snippet.defaultAudioLanguage, which videos.update does not list as '
                       'writable; snippet.defaultLanguage (title/description language) is NOT substituted'),
    'PAID_PROMOTION': (MANUAL, 'paidProductPlacementDetails.hasPaidProductPlacement: listed as a part, but not as a settable '
                       'property of videos.update (videos.insert lists it) - ambiguous, set in Studio'),
    'LIKE_COUNT_VISIBLE': (MANUAL, 'no like-count control in the API; status.publicStatsViewable (extended statistics) is a '
                           'different control and is NOT substituted'),
    'AUTO_CHAPTERS': (NOT_EXPOSED, 'no Data API field'),
    'AUTO_PLACES': (NOT_EXPOSED, 'no Data API field'),
    'AUTO_CONCEPTS': (NOT_EXPOSED, 'no Data API field'),
    'SUBSCRIBER_FEED_NOTIFY': (NOT_EXPOSED, 'notifySubscribers exists only on videos.insert; the video is already uploaded'),
    'COMMENTS_ENABLED': (NOT_EXPOSED, 'no Data API field for video comment settings'),
    'COMMENT_MODERATION': (NOT_EXPOSED, 'no Data API field (channel moderateComments is deprecated)'),
    'COMMENT_SORT': (NOT_EXPOSED, 'no Data API field'),
    'REMIX_POLICY': (NOT_EXPOSED, 'no Data API field'),
    'RECORDING_LOCATION': (NOT_EXPOSED, 'recordingDetails.location is deprecated'),
}
SETTING_TARGETS = {   # key -> (videos resource part, property)
    'TITLE_DESCRIPTION_LANGUAGE': ('snippet', 'defaultLanguage'), 'CATEGORY': ('snippet', 'categoryId'),
    'MADE_FOR_KIDS': ('status', 'selfDeclaredMadeForKids'), 'AI_DISCLOSURE': ('status', 'containsSyntheticMedia'),
    'LICENSE': ('status', 'license'), 'EMBEDDING_ALLOWED': ('status', 'embeddable'),
    'RECORDING_DATE': ('recordingDetails', 'recordingDate'),
}
SNIPPET_KEEP = ('title', 'description', 'tags', 'categoryId', 'defaultLanguage', 'defaultAudioLanguage')
STATUS_KEEP = ('privacyStatus', 'embeddable', 'license', 'publicStatsViewable', 'selfDeclaredMadeForKids', 'containsSyntheticMedia')
CATEGORY_IDS = {name.lower(): ident for name, ident in (
    ('Film & Animation', '1'), ('Autos & Vehicles', '2'), ('Music', '10'), ('Pets & Animals', '15'), ('Sports', '17'),
    ('Travel & Events', '19'), ('Gaming', '20'), ('People & Blogs', '22'), ('Comedy', '23'), ('Entertainment', '24'),
    ('News & Politics', '25'), ('Howto & Style', '26'), ('Education', '27'), ('Science & Technology', '28'),
    ('Nonprofits & Activism', '29'))}


def _bool(value):
    if value.lower() in ('true', 'false'):
        return value.lower() == 'true'
    raise ValueError('expected true or false')


def _license(value):
    mapped = {'standard': 'youtube', 'youtube': 'youtube', 'creative_commons': 'creativeCommon',
              'creativecommon': 'creativeCommon', 'creativecommons': 'creativeCommon'}.get(value.lower())
    if not mapped:
        raise ValueError('expected standard or creative_commons')
    return mapped


def _category(value):
    if value.isdigit():
        return value
    if value.lower() in CATEGORY_IDS:
        return CATEGORY_IDS[value.lower()]
    raise ValueError('unknown category name; use the numeric category id')


def _language(value):
    if not re.fullmatch(r'[A-Za-z]{2,3}(-[A-Za-z0-9]{2,8})*', value):
        raise ValueError('expected a language code such as en or en-GB')
    return value


def _date(value):
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError:
        raise ValueError('expected an ISO 8601 date or date-time') from None
    return value if 'T' in value else value + 'T00:00:00Z'


def _text(value):
    return unicodedata.normalize('NFC', value)


NORMALIZERS = {'PLAYLIST': _text, 'TITLE_DESCRIPTION_LANGUAGE': _language, 'CATEGORY': _category,
               'MADE_FOR_KIDS': _bool, 'AI_DISCLOSURE': _bool, 'LICENSE': _license, 'EMBEDDING_ALLOWED': _bool,
               'RECORDING_DATE': _date}


def parse_settings(raw, project):
    """KEY=VALUE lines ('#' comments). Returns {KEY: [values]}; raises ValueError when the file as a whole is unusable."""
    parsed = {}
    for number, line in enumerate(raw.decode('utf-8').splitlines(), 1):
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        if '=' not in line:
            raise ValueError(f'line {number} is not KEY=VALUE')
        key, value = line.split('=', 1)
        parsed.setdefault(key.strip(), []).append(value.strip())
    if parsed.get('PROJECT_ID', [project]) != [project]:
        raise ValueError(f'PROJECT_ID {parsed["PROJECT_ID"]} does not match project {project}')
    return parsed


def evaluate_settings(parsed, meta):
    """Classify every field. Status here is intrinsic (VALID/BLANK/INVALID/AMBIGUOUS/MANUAL/NOT_EXPOSED/IDENTITY);
    classify() later turns VALID into APPLY or SYNCED against the recorded state."""
    fields = []
    for key, values in parsed.items():
        rule = SETTING_RULES.get(key)
        field = {'key': key, 'value': values[0], 'norm': None, 'detail': ''}
        if key in IDENTITY_KEYS:
            field['status'], field['detail'] = 'IDENTITY', 'not a YouTube setting; verified against the folder name'
        elif rule is None:
            field['status'], field['detail'] = MANUAL, 'unknown key; not interpreted'
        elif rule[0] != API_WRITABLE:
            field['status'], field['detail'] = rule
        elif len(values) > 1:
            field['status'], field['detail'] = 'AMBIGUOUS', 'key appears more than once'
        elif not values[0]:
            field['status'], field['detail'] = 'BLANK', 'blank = unknown; never applied, never clears the video value'
        else:
            try:
                field['norm'] = NORMALIZERS[key](values[0])
                field['status'], field['detail'] = 'VALID', rule[1]
                if key == 'CATEGORY' and meta and meta.get('categoryid') not in (None, field['norm']):
                    field['status'], field['detail'] = 'AMBIGUOUS', f'meta.txt categoryid {meta["categoryid"]} disagrees'
            except ValueError as exc:
                field['status'], field['detail'] = 'INVALID', str(exc)
        fields.append(field)
    return fields


def manual_service():
    """Credentials for the manual workflow: the dedicated update token when present, otherwise the EXISTING
    caption token (same youtube.force-ssl scope covers videos.update, thumbnails.set and captions.*).
    Never starts an interactive OAuth flow."""
    if (BASE / 'secrets' / TOKEN_NAME).is_file():
        return update_service()
    path = BASE / 'secrets' / CAPTION_TOKEN_NAME
    if not path.is_file() or path.is_symlink():
        raise uploader.AuthRequired('Update OAuth required: run publishing.py --authorize interactively '
                                    f'(or provide secrets/{CAPTION_TOKEN_NAME})')
    with path.open('rb') as handle:
        creds = pickle.load(handle)
    if not creds.has_scopes(UPDATE_SCOPES):
        raise uploader.AuthRequired(f'{CAPTION_TOKEN_NAME} lacks youtube.force-ssl')
    if creds.expired and creds.refresh_token:
        request = uploader.Request()
        creds.refresh(lambda *a, **kw: request(*a, **dict(kw, timeout=30)))   # in memory only; the caption worker owns the file
    if not creds.valid:
        raise uploader.AuthRequired(f'{CAPTION_TOKEN_NAME} invalid; interactive authorization required')
    return uploader.build('youtube', 'v3', credentials=creds, cache_discovery=False)


def validate_subtitle(path):
    """final.srt must be a regular, UTF-8, parseable, non-empty SRT. Returns raw bytes. (Not touched, not rewritten.)"""
    from tools.final_subtitle_autogen import parse_srt
    raw = regular(path)
    if not 0 < len(raw) <= MAX_SUBTITLE_BYTES:
        raise ValueError('size must be 1 byte to 5 MiB')
    parse_srt(raw.decode('utf-8-sig'))
    return raw



def select_thumbnail(folder):
    """The single final thumbnail: exactly one visible image file in the project root whose name starts with
    'thumbnail' (any case). Zero or several -> error; never silently chooses."""
    found = sorted(p for p in folder.iterdir() if not p.name.startswith('.') and p.stem.lower().startswith('thumbnail')
                   and p.suffix.lower() in uploader.THUMB_EXTS)
    if not found:
        raise AssetMissing('thumbnail missing (one Thumbnail*.png/.jpg/.jpeg file in the project root)')
    if len(found) > 1:
        raise AssetAmbiguous('thumbnail ambiguous, keep exactly one: ' + ', '.join(p.name for p in found))
    thumb = found[0]
    if thumb.is_symlink() or not thumb.is_file():
        raise ValueError(f'thumbnail must be a regular file: {thumb.name}')
    if thumb.suffix.lower() not in MANUAL_THUMB_EXTS:
        raise ValueError(f'thumbnail {thumb.name}: YouTube thumbnails.set accepts only JPEG/PNG')
    return thumb


def manual_package(folder):
    """Collect every asset group WITHOUT raising. info['blocked'] holds only problems that make ANY write unsafe
    (not a project in done/, bad video ID); a missing/invalid asset only marks its own group."""
    info = {'project': folder.name, 'blocked': [], 'video_id': None, 'meta': None, 'tags': [], 'thumb': None,
            'image': None, 'subtitle': folder / SUBTITLE_NAME, 'subtitle_raw': None, 'settings_path': folder / SETTINGS_NAME,
            'settings_fields': [], 'ready': False,
            'assets': {g: {'state': 'MISSING', 'sha256': None, 'detail': ''} for g in GROUPS}}
    assets = info['assets']

    def mark(group, state, detail, sha=None):
        assets[group].update(state=state, detail=detail, sha256=sha)

    if folder.is_symlink() or folder.parent != BASE / 'done' or not folder.is_dir():
        info['blocked'].append('project is not a real directory directly under done/')
        return info
    try:
        video_id = regular(folder / 'youtube_video_id.txt').decode().strip()
        if not re.fullmatch(r'[A-Za-z0-9_-]{11}', video_id):
            raise ValueError('youtube_video_id.txt is not a valid 11-character video ID')
        info['video_id'] = video_id
    except (OSError, ValueError, UnicodeError) as exc:
        info['blocked'].append(f'video ID missing/invalid: {exc}')
    meta_path = folder / 'meta.txt'
    if not os.path.lexists(meta_path):
        mark('metadata', 'MISSING', 'meta.txt missing')
    else:
        try:
            raw = regular(meta_path)
            meta = uploader.parse_meta(meta_path)
            info['tags'] = validate_meta(meta)
            info['meta'] = meta
            mark('metadata', 'PRESENT', '', hashlib.sha256(raw).hexdigest())
        except (OSError, ValueError, UnicodeError) as exc:
            mark('metadata', 'INVALID', f'meta.txt invalid: {str(exc).removeprefix("WAIT ")}')
    try:
        thumb = select_thumbnail(folder)
        image = regular(thumb)
        validate_image(thumb, image)
        info.update(thumb=thumb, image=image)
        mark('thumbnail', 'PRESENT', '', hashlib.sha256(image).hexdigest())
    except AssetMissing as exc:
        mark('thumbnail', 'MISSING', str(exc))
    except AssetAmbiguous as exc:
        mark('thumbnail', 'AMBIGUOUS', str(exc))
    except (OSError, ValueError, UnicodeError, subprocess.SubprocessError) as exc:
        mark('thumbnail', 'INVALID', str(exc).removeprefix('WAIT '))
    if not os.path.lexists(info['subtitle']):
        mark('subtitle', 'MISSING', f'final.srt missing ({SUBTITLE_NAME})')
    else:
        try:
            raw = validate_subtitle(info['subtitle'])
            info['subtitle_raw'] = raw
            mark('subtitle', 'PRESENT', '', hashlib.sha256(raw).hexdigest())
        except (OSError, ValueError, UnicodeError, ImportError) as exc:
            mark('subtitle', 'INVALID', f'final.srt invalid ({SUBTITLE_NAME}): {str(exc).removeprefix("WAIT ")}')
    if not os.path.lexists(info['settings_path']):
        mark('settings', 'MISSING', f'{SETTINGS_NAME} missing')
    else:
        try:
            raw = regular(info['settings_path'])
            info['settings_fields'] = evaluate_settings(parse_settings(raw, folder.name), info['meta'])
            mark('settings', 'PRESENT', '', hashlib.sha256(raw).hexdigest())
        except (OSError, ValueError, UnicodeError) as exc:
            mark('settings', 'INVALID', f'{SETTINGS_NAME} invalid: {exc}')
    ready = folder / READY_NAME
    # exact directory-entry name: the filesystem may be case-insensitive, so `ready`/`Ready` must not alias READY
    info['ready'] = READY_NAME in {entry.name for entry in folder.iterdir()} and ready.is_file() and not ready.is_symlink()
    return info


# --- per-group state (.manual_apply_state.json, version 2) -------------------------------------------------------
def legacy_fingerprint(info):
    return hashlib.sha256(json.dumps([info['video_id'], info['assets']['metadata']['sha256'],
                                      info['assets']['thumbnail']['sha256'], info['assets']['subtitle']['sha256']]).encode()).hexdigest()


def load_state(folder, info):
    """Read-only. Returns the v2 state for this video; a v1 state / `.applied` marker is migrated IN MEMORY (nothing is
    written until an apply). `.applied` only ever seeds per-group hashes; it never suppresses newly added/changed assets."""
    raw, applied = read_json(folder / MANUAL_STATE_NAME), read_json(folder / APPLIED_NAME)
    video_id = info['video_id']
    if raw.get('version') == STATE_VERSION and raw.get('video_id') == video_id:
        raw.setdefault('groups', {})
        return raw
    state = {'version': STATE_VERSION, 'video_id': video_id, 'groups': {}, 'migrated_from': None}
    stamp = applied.get('applied_at')
    for source in (raw, applied):
        if source.get('video_id') == video_id:
            state['caption_id'] = state.get('caption_id') or source.get('caption_id')
            state['subtitle_insert_started'] = state.get('subtitle_insert_started') or source.get('subtitle_insert_started')
    if applied.get('video_id') == video_id and applied.get('version') == STATE_VERSION:
        state['groups'] = json.loads(json.dumps(applied.get('groups', {})))
        state['migrated_from'] = '.applied v2'
    elif applied.get('video_id') == video_id:     # v1 `.applied` = all three assets completed with these hashes
        for group, key in (('metadata', 'meta_sha256'), ('thumbnail', 'thumbnail_sha256'), ('subtitle', 'subtitle_sha256')):
            if applied.get(key):
                state['groups'][group] = {'sha256': applied[key], 'applied_at': stamp, 'migrated': True}
        state['migrated_from'] = '.applied v1'
    elif raw.get('video_id') == video_id and raw.get('fingerprint') and raw['fingerprint'] == legacy_fingerprint(info):
        for group, flag in (('metadata', 'metadata_applied'), ('thumbnail', 'thumbnail_applied'), ('subtitle', 'subtitle_applied')):
            if raw.get(flag):
                state['groups'][group] = {'sha256': info['assets'][group]['sha256'], 'migrated': True}
        state['migrated_from'] = 'state v1'
    return state


def now_iso():
    return time.strftime('%Y-%m-%dT%H:%M:%S%z')


def classify(info, state):
    """Per-group sync status: MISSING/INVALID/AMBIGUOUS, or READY (never applied) / CHANGED / ALREADY_APPLIED,
    plus NOTHING_TO_APPLY for a settings file with no applicable field. Fills info['groups'] and field statuses."""
    recorded_groups = state.get('groups', {})
    groups = {}
    for group in GROUPS:
        asset = info['assets'][group]
        if asset['state'] != 'PRESENT':
            groups[group] = {'status': asset['state'], 'detail': asset['detail']}
        elif group != 'settings':
            old = recorded_groups.get(group, {}).get('sha256')
            groups[group] = {'status': 'READY' if not old else 'ALREADY_APPLIED' if old == asset['sha256'] else 'CHANGED',
                             'detail': ''}
    recorded = recorded_groups.get('settings', {}).get('fields', {})
    if info['assets']['settings']['state'] == 'PRESENT':
        for field in info['settings_fields']:
            if field['status'] == 'VALID':
                field['status'] = 'SYNCED' if recorded.get(field['key'], {}).get('value') == field['norm'] else 'APPLY'
        pending = [f for f in info['settings_fields'] if f['status'] == 'APPLY']
        if pending:
            status = 'CHANGED' if any(f['key'] in recorded for f in pending) else 'READY'
        else:
            status = 'ALREADY_APPLIED' if any(f['status'] == 'SYNCED' for f in info['settings_fields']) else 'NOTHING_TO_APPLY'
        groups['settings'] = {'status': status, 'detail': ''}
    info['groups'] = groups
    return groups


def group_details(info, group):
    meta = info['meta'] or {}
    if group == 'metadata' and info['meta']:
        return [f"title: {meta.get('title')}", f"description: {len(meta.get('description', ''))} chars",
                f"tags: {len(info['tags'])}" if info['tags'] else 'tags: absent (remote tags left unchanged)']
    if group == 'thumbnail' and info['thumb']:
        return [f'path: {info["thumb"]}']
    if group == 'subtitle' and info['subtitle_raw'] is not None:
        return [f'path: {info["subtitle"]}', f'language: {SUBTITLE_LANGUAGE}']
    return []


def report(info, out):
    """Preflight + explicit plan. Pure output; no I/O beyond `out`."""
    out(f"PROJECT: {info['project']}")
    out(f"VIDEO_ID: {info['video_id'] or 'MISSING'}")
    out(f"READY: {'yes' if info['ready'] else 'no'}")
    out('')
    groups = info['groups']
    for group in GROUPS:
        out(f"{LABELS[group]}: {groups[group]['status']}")
        for line in group_details(info, group):
            out(f'    {line}')
        if groups[group].get('detail'):
            out(f"    {groups[group]['detail']}")
        if group == 'settings':
            for field in info['settings_fields']:
                shown = field['value'] or '(blank)'
                out(f"    {field['key']}={shown}: {field['status']}" + (f" - {field['detail']}" if field['detail'] else ''))
    pending = [g for g in GROUPS if groups[g]['status'] in ('READY', 'CHANGED')]
    out('')
    out('WOULD APPLY:' if pending else 'WOULD APPLY: (nothing)')
    for group in pending:
        if group == 'settings':
            keys = ', '.join(f['key'] for f in info['settings_fields'] if f['status'] == 'APPLY')
            out(f"- {PLAN_NAMES[group]}: {groups[group]['status'].lower()} fields {keys}")
        else:
            out(f"- {PLAN_NAMES[group]}: {groups[group]['status'].lower()}")
    skipped = [g for g in GROUPS if groups[g]['status'] in ('ALREADY_APPLIED', 'NOTHING_TO_APPLY')]
    if skipped:
        out('WOULD SKIP:')
        for group in skipped:
            out(f"- {PLAN_NAMES[group]}: " + ('already applied' if groups[group]['status'] == 'ALREADY_APPLIED' else 'no applicable fields'))
    missing = [g for g in GROUPS if groups[g]['status'] == 'MISSING']
    if missing:
        out('MISSING (not applied):')
        for group in missing:
            out(f"- {PLAN_NAMES[group]}: {info['assets'][group]['detail']}")
    broken = [g for g in GROUPS if groups[g]['status'] in ('INVALID', 'AMBIGUOUS')]
    if broken:
        out('NOT APPLIED (invalid/ambiguous; other groups are unaffected):')
        for group in broken:
            out(f"- {PLAN_NAMES[group]}: {groups[group]['status']} - {info['assets'][group]['detail']}")
    manual = [f for f in info['settings_fields'] if f['status'] in (MANUAL, NOT_EXPOSED, 'INVALID', 'AMBIGUOUS', 'BLANK')]
    if manual:
        out(f'MANUAL / UNSUPPORTED / UNUSABLE {SETTINGS_NAME} fields (left in the file, set in Studio if wanted):')
        for field in manual:
            out(f"- {field['key']}: {field['status']} - {field['detail']}")


def write_applied(folder, info, state):
    """Bookkeeping summary only. It never decides whether an asset is applied (the per-group state does)."""
    path = folder / APPLIED_NAME
    previous = read_json(path)
    first_applied = previous.get('applied_at')
    if previous and previous.get('version') != STATE_VERSION:   # keep the original v1 record
        path.rename(folder / f'{APPLIED_NAME}.previous_{time.strftime("%Y%m%dT%H%M%S")}')
    groups = state['groups']
    record = {'version': STATE_VERSION, 'video_id': info['video_id'], 'groups': groups,
              'caption_id': state.get('caption_id'), 'subtitle_language': SUBTITLE_LANGUAGE,
              'applied_at': first_applied or now_iso(), 'last_applied_at': now_iso(),
              'tool': 'publishing.py --project --apply'}
    for group, key in (('metadata', 'meta_sha256'), ('thumbnail', 'thumbnail_sha256'), ('subtitle', 'subtitle_sha256')):
        record[key] = groups.get(group, {}).get('sha256')
    record['thumbnail'] = groups.get('thumbnail', {}).get('name')
    save_json(path, record)


def apply_subtitle(service, info, state, persist, replace_english=False):
    """Upload final.srt as THE English standard caption track without ever creating a duplicate.

    - tracks of other languages, ASR (auto-generated) and forced tracks are never touched or counted
    - our track is found by the caption ID recorded in the state; replaced in place (captions.update + media)
    - a standard English track we did not create (no recorded ID) is NOT overwritten unless
      `replace_english` and it is the only one; otherwise the run stops with the track described
    - crash window: if an insert may have happened (state flag) and exactly one standard English track exists, adopt it
    - captions.delete is never used"""
    video_id = info['video_id']
    tracks = service.captions().list(part='snippet', videoId=video_id).execute().get('items', [])
    ids = {t.get('id') for t in tracks}
    standard_en = [t for t in tracks if t.get('snippet', {}).get('language') == SUBTITLE_LANGUAGE
                   and str(t.get('snippet', {}).get('trackKind', 'standard')).lower() == 'standard']
    target = state.get('caption_id') if state.get('caption_id') in ids else None
    if target is None and standard_en:
        if state.get('subtitle_insert_started') and not state.get('caption_id') and len(standard_en) == 1:
            target = standard_en[0]['id']                       # adopt the track our interrupted insert created
        elif replace_english and len(standard_en) == 1:
            target = standard_en[0]['id']
        else:
            described = '; '.join(f"id={t['id']} name={t.get('snippet', {}).get('name', '')!r} "
                                  f"updated={t.get('snippet', {}).get('lastUpdated', '?')}" for t in standard_en)
            raise ValueError(f'a standard English caption track already exists on the video ({described}); refusing to create a '
                             'duplicate or overwrite it silently. Re-run with --replace-english-caption to replace that single '
                             'track with final.srt, or remove it in Studio')
    with tempfile.TemporaryDirectory(prefix='yt-caption-') as tmp:
        frozen = Path(tmp) / 'final.srt'
        frozen.write_bytes(info['subtitle_raw'])                # the validated bytes
        media = uploader.MediaFileUpload(str(frozen), mimetype='application/octet-stream')
        if target:
            response = service.captions().update(part='snippet', body={'id': target, 'snippet': {'isDraft': False}},
                                                 media_body=media).execute()
        else:
            state['subtitle_insert_started'] = True             # persisted BEFORE the call (crash-safe adoption on retry)
            persist()
            response = service.captions().insert(part='snippet', body={'snippet': {
                'videoId': video_id, 'language': SUBTITLE_LANGUAGE, 'name': '', 'isDraft': False}}, media_body=media).execute()
    caption_id = response.get('id')
    if not caption_id:
        raise ValueError('caption request returned no caption ID')
    state['caption_id'] = caption_id
    return caption_id



def same_date(live, want):
    try:
        parse = lambda v: datetime.fromisoformat(v.replace('Z', '+00:00')).replace(tzinfo=None)
        return parse(live) == parse(want)
    except (TypeError, ValueError):
        return False


def build_settings_update(live, wanted, video_id):
    """videos.update replaces every mutable property of a requested part that it omits. So each part we touch starts
    from the LIVE resource and only the changed settings are overridden; parts with nothing to change are not sent.
    Returns (body or None, keys_needing_write, keys_already_in_sync)."""
    changes, in_sync = {}, []
    for key, want in wanted.items():
        part, prop = SETTING_TARGETS[key]
        have = live.get(part, {}).get(prop)
        if have == want or (prop == 'containsSyntheticMedia' and have is None and want is False) or \
                (prop == 'recordingDate' and have is not None and same_date(have, want)):
            in_sync.append(key)
        else:
            changes.setdefault(part, {})[prop] = want
    body = {'id': video_id}
    if 'snippet' in changes:
        snippet = {k: v for k, v in live.get('snippet', {}).items() if k in SNIPPET_KEEP}
        snippet.update(changes['snippet'])
        if not snippet.get('title') or not snippet.get('categoryId'):
            raise ValueError('live snippet lacks title/categoryId; refusing an incomplete snippet update')
        body['snippet'] = snippet
    if 'status' in changes:
        status = {k: v for k, v in live.get('status', {}).items() if k in STATUS_KEEP}
        if live.get('status', {}).get('privacyStatus') == 'private' and live['status'].get('publishAt'):
            status['publishAt'] = live['status']['publishAt']
        if not status.get('privacyStatus'):
            raise ValueError('live status lacks privacyStatus; refusing an incomplete status update')
        status.update(changes['status'])
        body['status'] = status
    if 'recordingDetails' in changes:
        body['recordingDetails'] = dict(live.get('recordingDetails', {}), **changes['recordingDetails'])
    writes = [k for k in wanted if k not in in_sync]
    return (body if len(body) > 1 else None), writes, in_sync


def resolve_playlist(service, title):
    found, token = [], None
    while True:
        response = service.playlists().list(part='snippet', mine=True, maxResults=50, pageToken=token).execute()
        found += [p['id'] for p in response.get('items', []) if _text(p.get('snippet', {}).get('title', '')) == title]
        token = response.get('nextPageToken')
        if not token:
            break
    if not found:
        raise ValueError(f'MANUAL: no playlist titled {title!r} on this channel (playlists are never created automatically)')
    if len(found) > 1:
        raise ValueError(f'AMBIGUOUS: {len(found)} playlists are titled {title!r}; refusing to guess')
    return found[0]


def apply_settings(service, info, state, persist):
    """Apply only the APPLY fields. Returns (applied_keys, {key: reason})."""
    video_id = info['video_id']
    fields = {f['key']: f for f in info['settings_fields'] if f['status'] == 'APPLY'}
    applied, failed = [], {}
    record = state['groups'].setdefault('settings', {'fields': {}})
    record.setdefault('fields', {})

    def done(key, wrote, **extra):
        record['fields'][key] = dict({'value': fields[key]['norm'], 'applied_at': now_iso(), 'wrote': wrote}, **extra)
        record['sha256'] = info['assets']['settings']['sha256']
        record['applied_at'] = now_iso()
        persist()
        applied.append(key)

    video_keys = [k for k in fields if k in SETTING_TARGETS]
    if video_keys:
        try:
            parts = 'snippet,status' + (',recordingDetails' if 'RECORDING_DATE' in video_keys else '')
            result = service.videos().list(part=parts, id=video_id).execute()
            live = next((v for v in result.get('items', []) if v.get('id') == video_id), None)
            if live is None:
                raise ValueError('existing video not visible to the update credentials')
            body, writes, in_sync = build_settings_update(live, {k: fields[k]['norm'] for k in video_keys}, video_id)
            for key in in_sync:
                done(key, False)
            if body:
                response = service.videos().update(part=','.join(k for k in body if k != 'id'), body=body).execute()
                if response.get('id') != video_id:
                    raise ValueError('settings update returned an unexpected video ID')
                for key in writes:
                    done(key, True)
        except Exception as exc:
            for key in video_keys:
                if key not in applied:
                    failed[key] = f'{type(exc).__name__}: {exc}'
    if 'PLAYLIST' in fields:
        try:
            title = fields['PLAYLIST']['norm']
            playlist_id = resolve_playlist(service, title)
            existing = service.playlistItems().list(part='id', playlistId=playlist_id, videoId=video_id,
                                                    maxResults=1).execute().get('items', [])
            if existing:
                done('PLAYLIST', False, playlist_id=playlist_id, playlist_item_id=existing[0].get('id'))
            else:
                item = service.playlistItems().insert(part='snippet', body={'snippet': {
                    'playlistId': playlist_id, 'resourceId': {'kind': 'youtube#video', 'videoId': video_id}}}).execute()
                if not item.get('id'):
                    raise ValueError('playlist insert returned no item ID')
                done('PLAYLIST', True, playlist_id=playlist_id, playlist_item_id=item['id'])
        except Exception as exc:
            failed['PLAYLIST'] = f'{type(exc).__name__}: {exc}'
    return applied, failed


def manual_run(folder, apply=False, service=None, out=print, replace_english=False):
    """Exit code: 0 ok (missing groups are normal), 1 blocked (READY absent / unsafe project / no credentials),
    2 an API step failed (successful groups stay recorded), 3 uploader busy, 4 done but an INVALID/AMBIGUOUS asset or
    an unresolved setting was skipped. Without READY nothing is ever written to YouTube."""
    info = manual_package(folder)
    state = load_state(folder, info) if info['video_id'] else {'groups': {}}
    classify(info, state)
    report(info, out)
    if info['blocked'] or not info['ready']:
        out('')
        for problem in info['blocked']:
            out(f'BLOCKED: {problem}')
        if not info['ready']:
            out(f'BLOCKED: {READY_NAME} approval marker absent (touch {folder / READY_NAME}); --apply performs zero writes without it')
        out('STOP: nothing was sent to YouTube.')
        return 1
    groups = info['groups']
    pending = [g for g in GROUPS if groups[g]['status'] in ('READY', 'CHANGED')]
    skipped_bad = [g for g in GROUPS if groups[g]['status'] in ('INVALID', 'AMBIGUOUS')]
    unusable = any(f['status'] in ('INVALID', 'AMBIGUOUS') for f in info['settings_fields'])
    clean = 0 if not (skipped_bad or unusable) else 4
    out('')
    if not apply:
        out('DRY RUN OK: no YouTube write performed. Re-run with --apply to synchronize the groups under WOULD APPLY.')
        return clean
    if not pending:
        out('NOTHING TO APPLY: every present, valid asset is already synchronized. Nothing was sent to YouTube.')
        return clean
    video_id = info['video_id']
    with (BASE / '.yt_autoupload.lock').open('a') as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            out('WAIT uploader busy; try again shortly. Nothing was sent to YouTube.')
            return 3
        try:
            service = manual_service() if service is None else service
        except uploader.AuthRequired as exc:
            out(f'STOP: {exc}. Nothing was sent to YouTube.')
            return 1
        state_path = folder / MANUAL_STATE_NAME
        backed_up = []

        def persist():
            if not backed_up and state_path.is_file() and read_json(state_path).get('version') != STATE_VERSION:
                (folder / f'{MANUAL_STATE_NAME}.v1_{time.strftime("%Y%m%dT%H%M%S")}').write_bytes(state_path.read_bytes())
            backed_up.append(True)
            save_json(state_path, state)

        try:
            result = service.videos().list(part='snippet', id=video_id).execute()
            remote = next((v for v in result.get('items', []) if v.get('id') == video_id), None)
            if remote is None:
                raise ValueError('existing video not visible to the update credentials (wrong channel/account?)')
        except Exception as exc:
            out(f'FAILED before any write: {type(exc).__name__}: {exc}')
            return 2

        def unchanged(group):
            fresh = manual_package(folder)
            if fresh['video_id'] != video_id:
                raise ValueError('video ID changed during the run')
            if group == 'settings':
                now = {f['key']: f['norm'] for f in fresh['settings_fields'] if f['status'] == 'VALID'}
                for f in info['settings_fields']:
                    if f['status'] == 'APPLY' and now.get(f['key']) != f['norm']:
                        raise ValueError(f'{f["key"]} changed during the run')
            elif fresh['assets'][group]['state'] != 'PRESENT' or fresh['assets'][group]['sha256'] != info['assets'][group]['sha256']:
                raise ValueError(f'{LABELS[group].lower()} changed during the run')

        applied_groups, failures, unresolved = [], [], []
        for group in pending:
            label = PLAN_NAMES[group]
            try:
                unchanged(group)
                sha = info['assets'][group]['sha256']
                if group == 'metadata':
                    snippet = build_snippet(remote['snippet'], info['meta'], info['tags'])
                    response = service.videos().update(part='snippet', body={'id': video_id, 'snippet': snippet}).execute()
                    if response.get('id') != video_id:
                        raise ValueError('metadata update returned an unexpected video ID')
                    state['groups']['metadata'] = {'sha256': sha, 'applied_at': now_iso()}
                elif group == 'thumbnail':
                    with tempfile.TemporaryDirectory(prefix='yt-thumbnail-') as tmp:
                        frozen = Path(tmp) / info['thumb'].name
                        frozen.write_bytes(info['image'])   # the validated bytes, not a possibly edited live file
                        response = service.thumbnails().set(videoId=video_id,
                                                            media_body=uploader.MediaFileUpload(str(frozen))).execute()
                    if not response.get('items'):
                        raise ValueError('thumbnail update returned no thumbnail result')
                    state['groups']['thumbnail'] = {'sha256': sha, 'name': info['thumb'].name, 'applied_at': now_iso()}
                elif group == 'subtitle':
                    apply_subtitle(service, info, state, persist, replace_english)
                    state['groups']['subtitle'] = {'sha256': sha, 'caption_id': state.get('caption_id'),
                                                   'language': SUBTITLE_LANGUAGE, 'applied_at': now_iso()}
                else:
                    keys, failed = apply_settings(service, info, state, persist)
                    if keys:
                        out(f'OK {label}: ' + ', '.join(keys))
                        applied_groups.append(group)
                    for key, reason in failed.items():
                        (unresolved if reason.startswith(('ValueError: MANUAL', 'ValueError: AMBIGUOUS')) else failures).append(f'{label} {key}: {reason}')
                        out(f'NOT APPLIED {label} {key}: {reason}')
                    persist()
                    continue
            except Exception as exc:
                state['last_error'] = f'{label}: {type(exc).__name__}: {exc}'
                persist()
                failures.append(f'{label}: {exc}')
                out(f'FAILED {label}: {exc}')
                continue
            state['last_error'] = None
            persist()
            applied_groups.append(group)
            out(f'OK {label}')
        if applied_groups:
            write_applied(folder, info, state)
        out('')
        if failures:
            out(f"PARTIAL FAILURE: applied {', '.join(PLAN_NAMES[g] for g in applied_groups) or 'nothing'}; FAILED {'; '.join(failures)}")
            out('Successful groups are recorded individually. Re-run the same --apply command: only the failed/missing parts are retried; the video is never re-uploaded.')
            return 2
        out(f"APPLIED to video {video_id}: {', '.join(PLAN_NAMES[g] for g in applied_groups) or 'nothing'}. {READY_NAME} left in place."
            + (f' Not applied: {"; ".join(unresolved)}.' if unresolved else ''))
        return 4 if (unresolved or skipped_bad or unusable) else 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run', action='store_true')
    parser.add_argument('--dry-run', action='store_true')
    parser.add_argument('--apply', action='store_true', help='Explicitly allow remote updates; requires --run')
    parser.add_argument('--authorize', action='store_true', help='Interactive update OAuth only; no video mutations')
    parser.add_argument('--replace-english-caption', action='store_true', help='With --project --apply: replace the content of the single existing standard English caption track instead of stopping')
    parser.add_argument('--project', metavar='NAME', help='MANUAL incremental sync of one done/ project (metadata, thumbnail, final.srt, youtube_settings.txt): requires --dry-run or --apply; any write needs the READY marker file')
    args = parser.parse_args()
    if args.project:
        if args.run or args.authorize or args.apply == args.dry_run:
            parser.error('--project NAME needs exactly one of --dry-run or --apply (and not --run/--authorize)')
        name = Path(args.project)
        folder = name if name.is_absolute() else BASE / 'done' / name.name if len(name.parts) == 1 else BASE / name
        sys.exit(manual_run(folder.absolute(), apply=args.apply, replace_english=args.replace_english_caption))
    if args.run and args.dry_run or args.apply and not args.run or args.authorize and (args.run or args.apply or args.dry_run):
        parser.error('Choose dry-run, --run [--apply], or --authorize')
    if args.authorize:
        uploader.load_or_create_creds(BASE / 'secrets' / TOKEN_NAME, UPDATE_SCOPES)
        (BASE / 'secrets' / TOKEN_NAME).chmod(0o600)
        return
    def scan():
        for folder in sorted((BASE / 'done').iterdir()):
            if folder.is_dir() and not folder.is_symlink() and not folder.name.startswith('.'):
                try:
                    print(f'{folder.name}: {process(folder, args.run, args.apply or (BASE / ".publishing_updates_enabled").is_file())}', flush=True)
                except Exception as exc:
                    print(f'{folder.name}: FAILED {type(exc).__name__}: {exc}', flush=True)
    if not args.run:
        scan()
        return
    with (BASE / '.yt_autoupload.lock').open('a') as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print('WAIT uploader busy')
            return
        with paths_lock():
            scan()


if __name__ == '__main__':
    main()
