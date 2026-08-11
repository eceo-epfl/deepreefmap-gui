"""Record of the applications-menu entry this app created.

Its only job is to answer "did we put this here?". On Windows the installer
writes its shortcut to the same path this module would, so without a record
there is no way to tell an entry we may remove from one whose uninstaller is
counting on it.

It is never allowed to answer "does a shortcut exist" -- that is a filesystem
question, and a record that outlived the file it describes would report a
shortcut the user deleted by hand as still present.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from deepreefmap_gui.paths import shortcut_manifest_path

logger = logging.getLogger(__name__)


def read_record() -> dict[str, Any] | None:
    path = shortcut_manifest_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def write_record(location: str, target: str, backend: str) -> None:
    from deepreefmap_gui.packaging.releases import current_version

    path = shortcut_manifest_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "location": location,
                "target": target,
                "backend": backend,
                "version": current_version(),
            },
            indent=2,
        ),
        encoding="utf-8",
    )


def clear_record() -> None:
    try:
        shortcut_manifest_path().unlink(missing_ok=True)
    except OSError:
        logger.debug("Could not clear the shortcut record", exc_info=True)
