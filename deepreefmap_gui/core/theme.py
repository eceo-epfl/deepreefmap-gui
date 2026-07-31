from __future__ import annotations

import logging
from pathlib import Path

from PySide6.QtCore import QPointF, QStandardPaths, Qt
from PySide6.QtGui import QColor, QPainter, QPalette, QPen, QPixmap
from PySide6.QtWidgets import QApplication

logger = logging.getLogger(__name__)

# Structural greys as an elevation ramp: the shell is the darkest layer, panels
# sit above it, and hovered/raised controls above those. Each step is far enough
# from the last to be visible, and all carry the same faint blue-grey cast so
# they read as one surface family rather than as unrelated greys.
WINDOW = "#16191d"  # app shell, behind everything
WINDOW_TEXT = "#dfe5ec"
BASE = "#1b1f24"  # text fields and item views, recessed into a panel
ALT_BASE = "#20252b"
BUTTON = "#2a3038"
BORDER = "#333a42"  # hairline: above the panel fills, below the top of the ramp
GROOVE = "#2a3038"
TEXT_MUTED = "#93a0ad"
DISABLED_FG = "#6f6f6f"

# Recurring dark fills and text shades that sit between the palette roles above.
CARD_BG = "#21262c"  # raised card/panel fill, one notch off WINDOW
SURFACE_HI = "#373f4a"  # hover and raised states, the top of the fill ramp
BORDER_STRONG = "#46505a"  # dividers and hovered control borders
PREVIEW_BG = "#121417"  # backdrop behind image/preview panels before they load
OVERLAY_TEXT = "#e8e8e8"  # bright label text on the dark viewer overlays
TEXT_SECONDARY = "#b9c4cf"  # readouts a shade brighter than TEXT_MUTED
TEXT_DIM = "#6f7c89"  # least prominent text, dimmer than TEXT_MUTED
SLIDER_HANDLE = "#f0f0f0"  # near-white grab handle on trim/timeline sliders

# Item-view selection. A soft PRIMARY tint under unchanged body text, rather
# than a full-bleed PRIMARY slab with near-black text: a selected row should be
# legible at a glance, not the loudest thing on the page. Line edits keep the
# strong palette Highlight, where a hard selection colour is what you want.
SELECTION_BG = "#2b4763"

# Named semantic accents. These consolidate several inconsistent spellings that
# were scattered across the GUI (e.g. success was both "#4a4" and
# QColor(74, 170, 74)); migrate call sites onto these so there's one value each.
SUCCESS = "#4aaa4a"
WARNING = "#e8a04a"
ERROR = "#ff6b5e"  # brighter than the old #c0392b/#c84 for contrast on dark
PRIMARY = "#4aa3ff"
PRIMARY_DARK = "#2a78c8"  # PRIMARY's outline/handle-border shade
LINK = "#9ecbff"
UPDATE = "#e0a030"
DANGER_BG = "#8a2222"  # filled "Confirm delete?" button; distinct from ERROR text
BLOCK = "#e05050"  # hard "blocked" red on gauges and pre-flight verdicts; distinct from ERROR text

# Compound tokens that travel together (background + text + border).
BANNER_BG, BANNER_TEXT, BANNER_BORDER = "#1f2a36", "#d8e2ec", "#2f3f50"
WARN_BG, WARN_TEXT, WARN_BORDER = "#4a3a14", "#ffd98a", "#8a6b1a"

# Shared thin bar look for run-progress and utilisation meters, so every bar in
# the app reads the same. Height in px; bar_qss colors the fill chunk.
BAR_HEIGHT = 8

# Geometry, in px. Two radii (cards, controls) and two spacings (page padding,
# gap between panes) are enough to keep every screen on the same rhythm.
RADIUS = 6
RADIUS_SM = 4
PAGE_MARGIN = 14
GUTTER = 10


def _chevron_file(direction: str, color: str, size: int = 16) -> str:
    """Path to a painted chevron, for the QSS rules that need an `image:`.

    Styling a QComboBox or QAbstractSpinBox box model hands arrow drawing to the
    stylesheet engine, which then draws nothing unless given an image — so a
    styled combo loses the one mark that says it opens. Qt stylesheets take only
    a URL, and this project ships no icon resources, so the arrows are painted
    once into the cache dir and referenced from there.
    """
    cache = Path(
        QStandardPaths.writableLocation(QStandardPaths.StandardLocation.CacheLocation)
        or "."
    )
    cache.mkdir(parents=True, exist_ok=True)
    path = cache / f"chevron-{direction}-{color.lstrip('#')}-{size}.png"
    if not path.exists():
        pixmap = QPixmap(size, size)
        pixmap.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        pen = QPen(QColor(color), 1.5)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        painter.setPen(pen)
        mid, arm = size / 2, size * 0.22
        drop = arm if direction == "down" else -arm
        painter.drawLine(QPointF(mid - arm, mid - drop / 2), QPointF(mid, mid + drop / 2))
        painter.drawLine(QPointF(mid, mid + drop / 2), QPointF(mid + arm, mid - drop / 2))
        painter.end()
        pixmap.save(str(path))
    # Qt stylesheet urls take forward slashes on every platform. Callers quote
    # the result: this lands under the user's cache directory, which on Windows
    # sits below a profile name that routinely contains a space, and an unquoted
    # url() stops parsing there -- taking every rule after it in the block with
    # it, so the combo and spin arrows all disappear.
    return path.as_posix()


def _arrow_qss() -> str:
    """Arrow sub-control rules, built after a QApplication exists so the
    chevrons can be painted."""
    down = _chevron_file("down", TEXT_MUTED)
    up = _chevron_file("up", TEXT_MUTED)
    down_off = _chevron_file("down", DISABLED_FG)
    up_off = _chevron_file("up", DISABLED_FG)
    return f"""
QComboBox::drop-down {{
    subcontrol-origin: padding;
    subcontrol-position: center right;
    border: none;
    width: 20px;
}}
QComboBox::down-arrow {{ image: url("{down}"); width: 12px; height: 12px; }}
QComboBox::down-arrow:disabled {{ image: url("{down_off}"); }}
QAbstractSpinBox::up-button, QAbstractSpinBox::down-button {{
    subcontrol-origin: border;
    border: none;
    width: 16px;
    background: transparent;
}}
QAbstractSpinBox::up-button {{ subcontrol-position: top right; }}
QAbstractSpinBox::down-button {{ subcontrol-position: bottom right; }}
QAbstractSpinBox::up-arrow {{ image: url("{up}"); width: 10px; height: 10px; }}
QAbstractSpinBox::down-arrow {{ image: url("{down}"); width: 10px; height: 10px; }}
QAbstractSpinBox::up-arrow:disabled, QAbstractSpinBox::up-arrow:off {{
    image: url("{up_off}");
}}
QAbstractSpinBox::down-arrow:disabled, QAbstractSpinBox::down-arrow:off {{
    image: url("{down_off}");
}}
"""


def bar_qss(chunk: str) -> str:
    """Flat GROOVE track with a rounded, colored fill chunk."""
    return (
        f"QProgressBar {{ background:{GROOVE}; border:none; border-radius:3px; }}"
        f" QProgressBar::chunk {{ background:{chunk}; border-radius:3px; }}"
    )

# Layered on top of Fusion + the palette for the few things the palette alone
# doesn't make consistent. Additive: existing per-widget stylesheets still
# cascade over this and win on conflicts.
#
# Note that a per-widget setStyleSheet *replaces* the matching rule here rather
# than merging with it, so an override that only wants to recolour a widget must
# still restate the padding and radius it is displacing.
#
# Checkboxes and radio buttons are deliberately absent: styling them in QSS
# means supplying indicator images, which this project has no pipeline for, so
# Fusion keeps drawing them.
GLOBAL_QSS = f"""
QGroupBox {{
    background-color: {CARD_BG};
    border: 1px solid {BORDER};
    border-radius: {RADIUS}px;
    /* Tall enough for the title to sit clear of the frame; any less and the
       top border is drawn straight through the text. */
    margin-top: 20px;
    padding-top: 8px;
}}
QGroupBox::title {{
    subcontrol-origin: margin;
    subcontrol-position: top left;
    left: 10px;
    padding: 0 4px;
    color: {TEXT_MUTED};
    font-weight: 600;
}}

QPushButton, QToolButton {{
    background-color: {BUTTON};
    color: {WINDOW_TEXT};
    border: 1px solid {BORDER};
    border-radius: {RADIUS_SM}px;
    padding: 5px 12px;
}}
QPushButton:hover, QToolButton:hover {{
    background-color: {SURFACE_HI};
    border-color: {BORDER_STRONG};
}}
QPushButton:pressed, QToolButton:pressed {{
    background-color: {BASE};
}}
QPushButton:disabled, QToolButton:disabled {{
    background-color: {CARD_BG};
    color: {DISABLED_FG};
    border-color: {BORDER};
}}
/* A latched button is a mode that stays on until it is pressed again, so it has
   to look held down rather than like every other button on the row. */
QPushButton:checked, QToolButton:checked {{
    background-color: {SURFACE_HI};
    border-color: {PRIMARY};
    color: {LINK};
    font-weight: 600;
}}

/* One filled action per screen: the step's forward move. */
QPushButton[cta="true"] {{
    background-color: {PRIMARY};
    color: {WINDOW};
    border-color: {PRIMARY_DARK};
    font-weight: 600;
}}
QPushButton[cta="true"]:hover {{
    background-color: {LINK};
    border-color: {PRIMARY};
}}
QPushButton[cta="true"]:pressed {{
    background-color: {PRIMARY_DARK};
}}
QPushButton[cta="true"]:disabled {{
    background-color: {GROOVE};
    color: {DISABLED_FG};
    border-color: {BORDER};
}}

/* Secondary actions that should not compete: Back, inline cell buttons. */
QPushButton[quiet="true"], QToolButton[quiet="true"] {{
    background-color: transparent;
    color: {TEXT_MUTED};
    border-color: transparent;
}}
QPushButton[quiet="true"]:hover, QToolButton[quiet="true"]:hover {{
    background-color: {SURFACE_HI};
    color: {WINDOW_TEXT};
    border-color: {BORDER};
}}

/* Fixed-size icon buttons have no room for the padding above. */
QPushButton[pad="none"], QToolButton[pad="none"] {{
    padding: 0;
}}

QLineEdit, QPlainTextEdit, QTextEdit, QComboBox, QSpinBox, QDoubleSpinBox,
QAbstractSpinBox {{
    background-color: {BASE};
    border: 1px solid {BORDER};
    border-radius: {RADIUS_SM}px;
    padding: 4px 8px;
    selection-background-color: {PRIMARY};
}}
QLineEdit:hover, QComboBox:hover, QSpinBox:hover, QDoubleSpinBox:hover,
QAbstractSpinBox:hover {{
    border-color: {BORDER_STRONG};
}}
QLineEdit:disabled, QComboBox:disabled, QSpinBox:disabled,
QDoubleSpinBox:disabled, QAbstractSpinBox:disabled {{
    background-color: {CARD_BG};
    color: {DISABLED_FG};
}}

QListWidget, QListView, QTreeWidget, QTreeView, QTableWidget, QTableView {{
    background-color: {BASE};
    alternate-background-color: {ALT_BASE};
    border: 1px solid {BORDER};
    border-radius: {RADIUS}px;
    gridline-color: {BORDER};
}}
QListWidget::item, QListView::item, QTreeWidget::item, QTreeView::item {{
    padding: 5px 8px;
    border-radius: {RADIUS_SM}px;
}}
QTableWidget::item, QTableView::item {{
    padding: 4px 8px;
}}
QListWidget::item:hover, QListView::item:hover, QTreeWidget::item:hover,
QTreeView::item:hover {{
    background-color: {SURFACE_HI};
}}
QListWidget::item:selected, QListView::item:selected,
QTreeWidget::item:selected, QTreeView::item:selected,
QTableWidget::item:selected, QTableView::item:selected {{
    background-color: {SELECTION_BG};
    color: {WINDOW_TEXT};
}}

QHeaderView::section {{
    background-color: {CARD_BG};
    color: {TEXT_MUTED};
    font-weight: 600;
    border: none;
    border-bottom: 1px solid {BORDER};
    padding: 6px 8px;
}}
QHeaderView::section:hover {{
    color: {WINDOW_TEXT};
}}
QTableCornerButton::section {{
    background-color: {CARD_BG};
    border: none;
    border-bottom: 1px solid {BORDER};
}}

QScrollBar:vertical {{
    background: transparent;
    width: 10px;
    margin: 0;
}}
QScrollBar:horizontal {{
    background: transparent;
    height: 10px;
    margin: 0;
}}
QScrollBar::handle:vertical, QScrollBar::handle:horizontal {{
    background: {BORDER_STRONG};
    border-radius: 5px;
    min-height: 24px;
    min-width: 24px;
}}
QScrollBar::handle:hover {{
    background: {TEXT_DIM};
}}
QScrollBar::add-line, QScrollBar::sub-line {{
    height: 0;
    width: 0;
}}
QScrollBar::add-page, QScrollBar::sub-page {{
    background: transparent;
}}

QToolTip {{
    color: {WINDOW_TEXT};
    background-color: {SURFACE_HI};
    border: 1px solid {BORDER_STRONG};
    border-radius: {RADIUS_SM}px;
    padding: 4px 8px;
}}
QTabWidget::pane {{
    background-color: {CARD_BG};
    border: 1px solid {BORDER};
    border-radius: {RADIUS}px;
    top: -1px;
}}
QTabBar::tab {{
    background: transparent;
    color: {TEXT_MUTED};
    padding: 6px 12px;
    border: 1px solid transparent;
    border-bottom: none;
    border-top-left-radius: {RADIUS_SM}px;
    border-top-right-radius: {RADIUS_SM}px;
}}
QTabBar::tab:hover {{
    color: {WINDOW_TEXT};
    background: {SURFACE_HI};
}}
QTabBar::tab:selected {{
    background: {CARD_BG};
    color: {WINDOW_TEXT};
    border-color: {BORDER};
}}
QTabBar::tab:disabled {{
    color: {DISABLED_FG};
}}
QSplitter::handle {{
    background-color: {WINDOW};
}}
QSplitter::handle:hover {{
    background-color: {BORDER};
}}
*:focus {{
    outline: none;
}}
QLineEdit:focus, QPlainTextEdit:focus, QTextEdit:focus, QSpinBox:focus,
QDoubleSpinBox:focus, QComboBox:focus, QAbstractSpinBox:focus {{
    border: 1px solid {PRIMARY};
}}
"""


def apply_theme(app: QApplication) -> None:
    """Force a consistent dark theme regardless of the host OS appearance."""
    # Otherwise the app inherits the platform palette: on macOS in Light mode the
    # standard widgets render light while the hardcoded-dark stylesheets stay dark.
    try:
        app.setStyle("Fusion")
    except Exception:
        logger.warning("Could not set Fusion style; keeping platform style", exc_info=True)

    # Tell the platform we want dark so native chrome follows, notably the macOS
    # window titlebar and native dialogs, which Fusion + the palette don't reach.
    # Needs Qt 6.8+; harmless no-op on older builds.
    try:
        app.styleHints().setColorScheme(Qt.ColorScheme.Dark)
    except (AttributeError, TypeError):
        logger.debug("setColorScheme unavailable (needs Qt 6.8+)", exc_info=True)

    role = QPalette.ColorRole
    group = QPalette.ColorGroup
    pal = QPalette()
    pal.setColor(role.Window, QColor(WINDOW))
    pal.setColor(role.WindowText, QColor(WINDOW_TEXT))
    pal.setColor(role.Base, QColor(BASE))
    pal.setColor(role.AlternateBase, QColor(ALT_BASE))
    pal.setColor(role.ToolTipBase, QColor(GROOVE))
    pal.setColor(role.ToolTipText, QColor(WINDOW_TEXT))
    pal.setColor(role.Text, QColor(WINDOW_TEXT))
    pal.setColor(role.PlaceholderText, QColor("#8a8a8a"))
    pal.setColor(role.Button, QColor(BUTTON))
    pal.setColor(role.ButtonText, QColor(WINDOW_TEXT))
    pal.setColor(role.BrightText, QColor("#ffffff"))
    pal.setColor(role.Link, QColor(LINK))
    pal.setColor(role.Highlight, QColor(PRIMARY))
    pal.setColor(role.HighlightedText, QColor("#101010"))

    pal.setColor(group.Disabled, role.Text, QColor(DISABLED_FG))
    pal.setColor(group.Disabled, role.WindowText, QColor(DISABLED_FG))
    pal.setColor(group.Disabled, role.ButtonText, QColor(DISABLED_FG))
    pal.setColor(group.Disabled, role.Highlight, QColor(GROOVE))
    pal.setColor(group.Disabled, role.HighlightedText, QColor(DISABLED_FG))
    app.setPalette(pal)

    # Arrow rules come last: they need a live QApplication to paint their images.
    app.setStyleSheet(GLOBAL_QSS + _arrow_qss())
