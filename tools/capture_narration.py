#!/usr/bin/env python3
"""Save one finalized narration verbatim as a create-only Obsidian asset.

Usage: pbpaste | python3 capture_narration.py IRAN3
"""
import argparse
import os
from pathlib import Path
import re
import sys
import tempfile


NARRATION_ASSET_DIR = (Path.home() / 'Documents/Obsidian Vault/Knowledge Factory/YouTube'
                       / 'Narration Sources')
PROJECT_ID = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$')


class NarrationConflictError(FileExistsError):
    pass


def asset_path(project_id, asset_dir=NARRATION_ASSET_DIR):
    if not isinstance(project_id, str) or not PROJECT_ID.fullmatch(project_id):
        raise ValueError('Project ID must be 1-64 ASCII letters, digits, _ or -, starting with a letter or digit')
    return Path(asset_dir) / f'{project_id}.md'


def validate_narration(content):
    if not isinstance(content, bytes):
        raise TypeError('Narration content must be bytes')
    try:
        text = content.decode('utf-8-sig')
    except UnicodeDecodeError as exc:
        raise ValueError('Narration must be UTF-8 text') from exc
    if not text.strip():
        raise ValueError('Narration must not be empty')


def existing_result(path, content):
    if path.is_symlink():
        raise NarrationConflictError(f'Narration asset must be a regular file: {path}')
    existing = path.read_bytes()
    if existing == content:
        return 'ALREADY EXISTS', path
    raise NarrationConflictError(f'Narration asset already exists with different content: {path}')


def save_narration(project_id, content, asset_dir=NARRATION_ASSET_DIR):
    """Atomically create a verbatim asset; never replace an existing target."""
    path = asset_path(project_id, asset_dir)
    validate_narration(content)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() or path.is_symlink():
        return existing_result(path, content)

    fd, temporary = tempfile.mkstemp(prefix=f'.{project_id}.', suffix='.tmp', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            # link(2) publishes only if the exact destination does not exist.
            # Unlike replace(), it cannot overwrite a narration asset.
            os.link(temporary, path)
        except FileExistsError:
            return existing_result(path, content)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
    return 'CREATED', path


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('project_id', help='Exact YouTube project ID, e.g. IRAN3')
    args = parser.parse_args(argv)
    try:
        status, path = save_narration(args.project_id, sys.stdin.buffer.read())
    except (OSError, TypeError, ValueError) as exc:
        print(f'NARRATION CAPTURE FAILED: {exc}', file=sys.stderr)
        return 1
    print(f'NARRATION {status}: {path}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
