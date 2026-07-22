# Third-Party Notices

## Qt / PySide6

The application uses Qt through the PySide6 bindings, distributed under the
GNU Lesser General Public License v3 (LGPL-3.0). PySide6 is consumed as an
unmodified dynamically-linked dependency installed from PyPI; users can swap
in their own build of the library. Source code is available from the Qt
Project (https://code.qt.io/cgit/pyside/pyside-setup.git/).

## Bundled Fonts

`deepreefmap_gui/resources/fonts/` ships two families under the SIL Open Font
License 1.1, both unmodified and neither declaring a Reserved Font Name:

- Inter 4.001 (Regular, Medium, SemiBold, Bold). Copyright (c) 2016 The Inter
  Project Authors (https://github.com/rsms/inter).
- JetBrains Mono 2.304 (Regular, Bold). Copyright 2020 The JetBrains Mono
  Project Authors (https://github.com/JetBrains/JetBrainsMono).

The full license texts ship with the fonts as `Inter-LICENSE.txt` and
`JetBrainsMono-OFL.txt`. The OFL covers the fonts as a separate work and is
compatible with the Apache-2.0 license of this application.

## deepreefmap

Reconstruction is performed by the deepreefmap library
(https://github.com/EPFL-ECEO/deepreefmap), Apache-2.0. Optional third-party
components it can use (notably the LoGeR mapping backend and downloaded model
checkpoints) carry their own terms; see that repository's
`THIRD_PARTY_NOTICES.md` before redistribution.
