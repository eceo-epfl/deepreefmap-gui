"""User-facing hint shown when the optional LoGeR mapping backend is unavailable."""

from __future__ import annotations

# Shown in the UI (mapping dropdown + Models tab) when loger/loger_star are
# unavailable. See the "LoGeR path" section of the README for the full setup.
LOGER_INSTALL_HINT = (
    "LoGeR is an optional mapping backend.\n"
    "Install the extra and the vendored submodule to enable it:\n"
    "    uv sync --extra loger\n"
    "    git submodule update --init --recursive\n"
    'Then download the checkpoints. See the "LoGeR path" section of the README.'
)
