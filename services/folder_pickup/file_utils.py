"""Filesystem helpers for folder pickup."""

from __future__ import annotations

import logging
import mimetypes
import shutil
import time
from datetime import datetime
from pathlib import Path
from typing import Set

logger = logging.getLogger(__name__)

SUPPORTED_EXTENSIONS = {".pdf", ".png", ".jpg", ".jpeg"}
RESERVED_SUBDIRS = {"processed", "failed"}
TEMP_SUFFIXES = (
    ".tmp",
    ".temp",
    ".partial",
    ".swp",
    ".crdownload",
    ".download",
    ".part",
)
TEMP_PREFIXES = (".", "~")


def ensure_pickup_dirs(inbox: Path) -> tuple[Path, Path]:
    inbox.mkdir(parents=True, exist_ok=True)
    processed = inbox / "processed"
    failed = inbox / "failed"
    processed.mkdir(parents=True, exist_ok=True)
    failed.mkdir(parents=True, exist_ok=True)
    return processed, failed


def guess_content_type(path: Path) -> str:
    guessed, _ = mimetypes.guess_type(path.name)
    if guessed:
        return guessed.lower()
    ext = path.suffix.lower()
    if ext == ".pdf":
        return "application/pdf"
    if ext in {".jpg", ".jpeg"}:
        return "image/jpeg"
    if ext == ".png":
        return "image/png"
    return "application/octet-stream"


def is_supported_extension(path: Path) -> bool:
    return path.suffix.lower() in SUPPORTED_EXTENSIONS


def is_temp_file(path: Path) -> bool:
    name = path.name
    if any(name.startswith(prefix) for prefix in TEMP_PREFIXES):
        return True
    lower = name.lower()
    return any(lower.endswith(suffix) for suffix in TEMP_SUFFIXES)


def is_incoming_file(path: Path, inbox: Path) -> bool:
    try:
        resolved = path.resolve()
        inbox_resolved = inbox.resolve()
    except OSError:
        return False

    if not resolved.is_file():
        return False
    if inbox_resolved not in resolved.parents and resolved.parent != inbox_resolved:
        return False
    if resolved.parent.name in RESERVED_SUBDIRS:
        return False
    if resolved.parent != inbox_resolved:
        return False
    if not is_supported_extension(resolved):
        return False
    if is_temp_file(resolved):
        return False
    return True


def list_incoming_files(inbox: Path) -> list[Path]:
    files: list[Path] = []
    for entry in inbox.iterdir():
        if is_incoming_file(entry, inbox):
            files.append(entry)
    return sorted(files, key=lambda p: p.stat().st_mtime)


def wait_for_file_stable(
    path: Path,
    *,
    stable_seconds: float,
    poll_interval: float = 0.5,
    max_wait_seconds: float = 120.0,
) -> bool:
    """Wait until file size/mtime stop changing (copy/upload complete)."""
    if not path.exists():
        return False

    deadline = time.monotonic() + max_wait_seconds
    last_size: Optional[int] = None
    last_mtime: Optional[float] = None
    stable_since: Optional[float] = None

    while time.monotonic() < deadline:
        try:
            stat = path.stat()
        except OSError:
            return False

        size = stat.st_size
        mtime = stat.st_mtime
        if last_size == size and last_mtime == mtime:
            if stable_since is None:
                stable_since = time.monotonic()
            elif time.monotonic() - stable_since >= stable_seconds:
                return True
        else:
            stable_since = None
            last_size = size
            last_mtime = mtime

        time.sleep(poll_interval)

    logger.warning(
        "[folder-pickup] File did not stabilize within %.1fs: %s",
        max_wait_seconds,
        path,
    )
    return False


def _unique_destination(dest_dir: Path, filename: str) -> Path:
    candidate = dest_dir / filename
    if not candidate.exists():
        return candidate
    stem = Path(filename).stem
    suffix = Path(filename).suffix
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return dest_dir / f"{stem}_{stamp}{suffix}"


def move_file(path: Path, dest_dir: Path) -> Path:
    dest_dir.mkdir(parents=True, exist_ok=True)
    destination = _unique_destination(dest_dir, path.name)
    shutil.move(str(path), str(destination))
    return destination
