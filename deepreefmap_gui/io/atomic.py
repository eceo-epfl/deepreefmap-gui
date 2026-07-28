"""Crash-safe replacement of small text files.

Several of the app's JSON and YAML files are read back to drive decisions rather
than merely displayed: the timing profile seeds the ETA and the pre-run memory
check, and a run manifest is what makes a run loadable at all. A plain
``write_text`` truncates the target before the new bytes land, so a kill or a
full disk part-way through leaves a file that parses as nothing and takes the
previous contents with it.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any


def atomic_write_text(path: Path, text: str, *, encoding: str = "utf-8") -> None:
    """Replace `path` with `text`, or leave it exactly as it was.

    The temporary file is created in the destination directory so ``os.replace``
    stays within one filesystem, where it is atomic on both POSIX and Windows.
    Its name is unique, so two processes writing the same target cannot pick up
    each other's half-written bytes.

    The contents are flushed to disk before the rename, so a power loss cannot
    leave a successfully renamed file holding stale or empty blocks. The
    directory entry itself is not synced -- surviving that is a stronger
    guarantee than these caches need, and it has no portable spelling.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".", suffix=".tmp")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding=encoding) as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        # Includes KeyboardInterrupt and SystemExit: an interrupted write should
        # not leave its scratch file behind either.
        tmp.unlink(missing_ok=True)
        raise


def atomic_write_json(path: Path, data: Any, *, indent: int | None = 2) -> None:
    """Serialise `data` and replace `path` with it, or leave `path` untouched.

    Serialising before the file is opened means an unserialisable value raises
    without having touched the destination.
    """
    atomic_write_text(path, json.dumps(data, indent=indent))
