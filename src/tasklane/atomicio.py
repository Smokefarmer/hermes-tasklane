"""Atomic file writes — the single implementation reused across the store and
the pairing/secret modules.

Every persisted file in TaskLane (job records, the client registry, project
registry) is written the same way: to a temp file in the same directory, flushed
and fsync'd, then ``os.replace``'d over the target so a concurrent reader never
sees a partial or missing file. ``mode`` is applied to the temp file *before* the
rename, so the file is never briefly world-readable at the final path — important
for the 0600 secret/credential files.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any


def atomic_write_text(path: str | os.PathLike[str], text: str, *, mode: int | None = None) -> Path:
    """Atomically write *text* to *path* (temp file + fsync + os.replace).

    If *mode* is given it is chmod'd onto the temp file before the rename, so the
    final path never appears with looser permissions than intended.
    """
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=target.parent,
        prefix=f".{target.name}.", suffix=".tmp", delete=False,
    ) as handle:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())
        temp_name = handle.name
    try:
        if mode is not None:
            os.chmod(temp_name, mode)
        os.replace(temp_name, target)
    except OSError:
        # never leave the temp file behind on a failed finalize
        if os.path.exists(temp_name):
            os.unlink(temp_name)
        raise
    return target


def atomic_write_json(path: str | os.PathLike[str], data: Any, *, mode: int | None = None,
                      indent: int = 2, sort_keys: bool = True) -> Path:
    """Atomically write *data* as JSON (trailing newline), via :func:`atomic_write_text`."""
    return atomic_write_text(path, json.dumps(data, indent=indent, sort_keys=sort_keys) + "\n", mode=mode)
