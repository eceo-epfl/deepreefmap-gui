"""Onboard this installation, and forget it again.

`connect` is the whole flow behind the Connect button: decode the pasted code,
enrol against the address inside it, and write the token to whichever credential
store this machine has. It talks to the network, so it runs on a worker thread and
never on the GUI one.

Qt-free, and it never logs the pasted code: the code is a credential.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from deepreefmap_gui.packaging.releases import current_version
from deepreefmap_gui.server.state import (
    LAST_SYNC_KEY,
    default_device_name,
    platform_name,
)
from deepreefmap_gui.survey.store import SYNC_SECTIONS, SurveyStore
from deepreefmap_gui.sync import client, credentials
from deepreefmap_gui.sync.connect_code import decode_connect_code
from deepreefmap_gui.sync.engine import CURSOR_KEY, WATERMARK_PREFIX

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Connected:
    """A spent connect code, as the page reports it.

    The address is in here because the app never had one configured: until this
    ran, nothing on this machine knew which registry it was about to join.
    """

    base_url: str
    device_id: str
    device_name: str
    backend: str
    # The human who minted the code, as the registry reports them. Shown as
    # audit, never as attribution.
    enrolled_by: str = ""
    # The connect code's own caveat, ie. a plain http address.
    warning: str = ""


def connect(pasted: str, device_name: str = "") -> Connected:
    """Trade a pasted connect code for a stored device credential.

    `device_name` is sent here and nowhere else. Renaming a device is a web
    interface action, so attribution is not mutable by the device it names.
    """
    code = decode_connect_code(pasted)
    name = device_name.strip() or default_device_name()
    enrolment = client.enrol(
        code, name, platform=platform_name(), gui_version=current_version()
    )
    if not enrolment.token:
        raise client.SyncError(
            "The registry accepted the code but returned no token, so nothing was stored."
        )
    backend = credentials.save(code.base_url, enrolment.token)
    logger.info("Enrolled with %s as device %s", code.base_url, enrolment.device_id)
    return Connected(
        base_url=code.base_url,
        device_id=enrolment.device_id,
        device_name=name,
        backend=backend,
        enrolled_by=enrolment.enrolled_by,
        warning=code.warning or "",
    )


def forget(store: SurveyStore | None) -> None:
    """Drop the token and this survey's sync position.

    The position goes with it. A cursor is one registry's own sequence, so
    keeping it across a reconnection would silently skip every row the next
    registry wrote before that number, and the watermarks would hold back rows
    it has never seen. Revocation itself happens server-side, in the web
    interface: this only forgets locally.
    """
    credentials.forget()
    if store is None:
        return
    for key in (CURSOR_KEY, LAST_SYNC_KEY):
        store.set_sync_state(key, None)
    for section in SYNC_SECTIONS:
        store.set_sync_state(f"{WATERMARK_PREFIX}{section}", None)
