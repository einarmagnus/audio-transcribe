"""Sidecar transcript cache.

A transcript for ``/path/to/audio.mp3`` lives next to the audio file as
``/path/to/audio.mp3.txt``.  Writes are atomic (temp file in the same
directory + ``os.replace``) so a crash mid-write can never leave a corrupt
transcript behind.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path


def sidecar_path(audio: str | os.PathLike[str]) -> Path:
    """Return the sidecar transcript path for *audio*."""
    path = Path(audio)
    return path.with_name(path.name + ".txt")


def load(audio: str | os.PathLike[str]) -> str | None:
    """Return the cached transcript for *audio*, or ``None`` on a cache miss.

    An unreadable or undecodable cache file is treated as a miss: we would
    rather transcribe again than fail because of a broken sidecar.
    """
    path = sidecar_path(audio)
    try:
        return path.read_text(encoding="utf-8")
    except (FileNotFoundError, NotADirectoryError):
        return None
    except (OSError, UnicodeDecodeError):
        return None


def save(audio: str | os.PathLike[str], text: str) -> Path:
    """Atomically write *text* to the sidecar for *audio*.

    Returns the sidecar path.  Raises ``OSError`` if the file cannot be
    written (e.g. read-only directory).
    """
    path = sidecar_path(audio)
    directory = path.parent
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=directory
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp_name, 0o644)
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise
    return path
