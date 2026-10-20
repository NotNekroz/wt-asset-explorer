"""Main window: War Thunder Vehicle & Loadout Explorer + Dagor Asset Explorer in one interface."""

from __future__ import annotations

import ctypes
from pathlib import Path
import re
import sys
import traceback
# [REMOVED in cleanup] `from io import BytesIO` (wtx/gui.py line 10 of the pre-cleanup file): imported but never used in this module.

from PySide6.QtCore import (
    QEvent,
    QObject,
    QRectF,
    QSize,
    QRunnable,
    QSettings,
    QStandardPaths,
    Qt,
    QThreadPool,
    QTimer,
    QUrl,
    Signal,
)
from PySide6.QtGui import (
    QColor,
    QDesktopServices,
    QFont,
    QFontDatabase,
    QIcon,
    QImage,
    QPainter,
    QPalette,
    QPen,
    QPixmap,
    QTextCharFormat,
    QTextCursor,
)
from PySide6.QtNetwork import QNetworkAccessManager, QNetworkReply, QNetworkRequest
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSplitter,
    QStyle,
    QToolButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

import util.log as dae_log
from vehicle_core.VehicleLook import auto_find_wt_path
from wtx import ROOT
from wtx.fonts import ensure_game_font, name_symbol_report, register_symbol_font, symbols_first
from wtx.assets import AssetLibrary, AssetRef
from vehicle_core.Loadout import LoadoutData
from wtx.loadout_panel import LoadoutPanel
from wtx.settings_dialog import SettingsDialog
from wtx.theme import QSS
from wtx.vehicles import (
    VEHICLE_TYPES,
    Vehicle,
    VehicleCatalog,
    find_wt_ext,
    looks_like_wt_dir,
)

APP_NAME = "WT Asset Explorer"
CDN = "https://static.encyclopedia.warthunder.com/images/{}.png"
UA = b"Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/115.0.0.0"


def safe_name(s: str) -> str:
  """Converts a display name or ID into a Windows-safe directory name."""
  clean_name = re.sub(r'[\\/:*?"<>|]', "_", str(s)).strip(" ._")
  return clean_name or "unnamed"


# ---------------------------------------------------------------------------------------- plumbing
class Bridge(QObject):
  line = Signal(str, int)  # text, kind
  progress = Signal(int, int)


class TeeStream:

  def __init__(self, bridge: Bridge, original, kind: int):
    self._bridge, self._orig, self._kind, self._buf = bridge, original, kind, ""

  def write(self, text: str):
    if self._orig:
      try:
        self._orig.write(text)
      except Exception:
        pass
    self._buf += text
    while "\n" in self._buf:
      line, self._buf = self._buf.split("\n", 1)
      if line.strip():
        self._bridge.line.emit(line.rstrip(), self._kind)

  def flush(self):
    if self._orig:
      try:
        self._orig.flush()
      except Exception:
        pass


class _JobSignals(QObject):
  done = Signal(object)
  failed = Signal(str)


class Job(QRunnable):

  def __init__(self, fn):
    super().__init__()
    self.setAutoDelete(False)
    self.fn = fn
    self.signals = _JobSignals()

  def run(self):
    try:
      result = self.fn()
    except Exception:
      self.signals.failed.emit(traceback.format_exc())
      return
    self.signals.done.emit(result)


class Console(QPlainTextEdit):

  def __init__(self):
    super().__init__()
    self.setReadOnly(True)
    self.setMaximumBlockCount(30000)
    self.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
    font = QFont("Cascadia Mono")
    font.setStyleHint(QFont.StyleHint.Monospace)
    font.setPointSize(9)
    self.setFont(font)
    self.setStyleSheet(
        "QPlainTextEdit { background:#0c0c0c; color:#cccccc; border:1px solid"
        " #444; selection-background-color:#264f78; }"
    )
    self._fmt = {}
    for kind, color in {
        0: "#cccccc",
        1: "#f9f1a5",
        2: "#f14c4c",
        3: "#61d6d6",
    }.items():
      f = QTextCharFormat()
      f.setForeground(QColor(color))
      self._fmt[kind] = f

  def add(self, text: str, kind: int = 0):
    if kind == 0:
      if "[E]" in text[:30]:
        kind = 2
      elif "[W]" in text[:30]:
        kind = 1
    bar = self.verticalScrollBar()
    stick = bar.value() >= bar.maximum() - 4
    cur = QTextCursor(self.document())
    cur.movePosition(QTextCursor.MoveOperation.End)
    if not self.document().isEmpty():
      cur.insertBlock()
    cur.insertText(text, self._fmt.get(kind, self._fmt[0]))
    if stick:
      bar.setValue(bar.maximum())


class AssetPicker(QDialog):

  def __init__(self, parent, models: list[AssetRef], title: str, prefill: str):
    super().__init__(parent)
    self.setWindowTitle(title)
    self.resize(640, 480)
    self._models = models
    lay = QVBoxLayout(self)
    lay.addWidget(
        QLabel(
            "No 3D model was matched automatically. Search the game's model"
            " list and pick one.\nYour choice is remembered for next time."
        )
    )
    self.search = QLineEdit(prefill)
    self.search.setPlaceholderText("Search model or .grp name...")
    self.search.textChanged.connect(self._refill)
    lay.addWidget(self.search)
    self.list = QListWidget()
    self.list.itemDoubleClicked.connect(lambda _i: self.accept())
    lay.addWidget(self.list, 1)
    self.info = QLabel()
    lay.addWidget(self.info)
    box = QDialogButtonBox(
        QDialogButtonBox.StandardButton.Ok
        | QDialogButtonBox.StandardButton.Cancel
    )
    box.accepted.connect(self.accept)
    box.rejected.connect(self.reject)
    lay.addWidget(box)
    self._refill()

  def _refill(self):
    q = self.search.text().strip().lower()
    self.list.clear()
    shown = 0
    total = 0
    for r in self._models:
      if q and q not in r.name.lower() and q not in r.grp.lower():
        continue
      total += 1
      if shown < 400:
        it = QListWidgetItem(f"{r.name}    [{r.kind}]    {r.grp_file}")
        it.setData(Qt.ItemDataRole.UserRole, r)
        self.list.addItem(it)
        shown += 1
    if self.list.count():
      self.list.setCurrentRow(0)
    self.info.setText(
        f"{total} match(es)"
        + (" - showing the first 400, refine the search" if total > 400 else "")
    )

  def chosen(self) -> AssetRef | None:
    it = self.list.currentItem()
    return it.data(Qt.ItemDataRole.UserRole) if it else None


# What the status line says while the tool works: the first pattern that matches a log line sets it.
# (The Logs panel still shows every line.)
_PHASES = [(re.compile(rx, re.I), text) for rx, text in (
    (r"^(unpacking |preparing localization|using cached game data)", "Reading game files..."),
    (r"^(loading cached localizations|reading localization|reading nations)", "Reading game data..."),
    (r"^(indexing weapon files|sorting vehicles|\d+ vehicles found)", "Indexing vehicles..."),
    (r"^scanning content folders", "Scanning model packs..."),
    (r"^indexing \d+ model packs", "Indexing models and textures..."),
    (r"^asset index ready", "Asset index ready"),
    (r"^loading .*\.grp", "Loading model files..."),
    (r"^loading ", "Reading files..."),
    (r"^(model uses \d+ textures|texture pack)", "Building textures..."),
    (r"^reading \w+ '", "Reading model..."),
    (r"^building lod", "Building model..."),
    (r"^writing obj", "Writing OBJ..."),
    (r"^writing gltf", "Writing GLB..."),
    (r"(animtree|animation\(s\)|\.a2d)", "Building animations..."),
    (r"\[weapons\]", "Adding weapon models..."),
    (r"\[skin\]", "Building skin template..."),
)]


def _icon_font_family() -> str | None:
  """Windows' symbol font for the caption buttons, or None (then plain text glyphs are used)."""
  try:
    fams = set(QFontDatabase.families())
  except Exception:
    return None
  for f in ("Segoe Fluent Icons", "Segoe MDL2 Assets"):
    if f in fams:
      return f
  return None


class TitleBar(QWidget):
  """Our own title bar: logo + title, drag to move, double-click to maximise, minimise / maximise / close."""

  def __init__(self, win: QMainWindow):
    super().__init__()
    self._win = win
    self._fam = _icon_font_family()
    self.setObjectName("titlebar")
    self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
    self.setFixedHeight(38)
    lay = QHBoxLayout(self)
    lay.setContentsMargins(12, 0, 0, 0)
    lay.setSpacing(10)
    icon_path = ROOT / "res" / "icon.ico"
    if icon_path.exists():
      logo = QLabel()
      logo.setPixmap(QIcon(str(icon_path)).pixmap(18, 18))
      lay.addWidget(logo)
    ttl = QLabel("War Thunder Vehicle & Loadout Explorer")
    ttl.setObjectName("appTitle")
    lay.addWidget(ttl)
    lay.addStretch(1)
    self.btn_min = self._button("\uE921", "\u2013", "winbtn", "Minimize")
    self.btn_max = self._button("\uE922", "\u25A1", "winbtn", "Maximize")
    self.btn_close = self._button("\uE8BB", "\u2715", "winclose", "Close")
    self.btn_min.clicked.connect(win.showMinimized)
    self.btn_max.clicked.connect(self.toggle_max)
    self.btn_close.clicked.connect(win.close)
    for b in (self.btn_min, self.btn_max, self.btn_close):
      lay.addWidget(b)

  def _button(self, glyph: str, fallback: str, name: str, tip: str) -> QPushButton:
    b = QPushButton(glyph if self._fam else fallback)
    b.setObjectName(name)
    b.setToolTip(tip)
    b.setFixedSize(46, 38)
    b.setFocusPolicy(Qt.FocusPolicy.NoFocus)
    if self._fam:
      b.setStyleSheet(f"font-family:'{self._fam}'; font-size:10px;")
    return b

  def toggle_max(self):
    if self._win.isMaximized():
      self._win.showNormal()
    else:
      self._win.showMaximized()

  def sync(self):
    """Maximise button shows 'restore' while the window is maximised."""
    if self._win.isMaximized():
      self.btn_max.setText("\uE923" if self._fam else "\u2750")
      self.btn_max.setToolTip("Restore")
    else:
      self.btn_max.setText("\uE922" if self._fam else "\u25A1")
      self.btn_max.setToolTip("Maximize")

  def mousePressEvent(self, e):
    if e.button() == Qt.MouseButton.LeftButton:
      h = self._win.windowHandle()
      if h is not None:
        h.startSystemMove()
        e.accept()
        return
    super().mousePressEvent(e)

  def mouseDoubleClickEvent(self, e):
    if e.button() == Qt.MouseButton.LeftButton:
      self.toggle_max()
      e.accept()
    else:
      super().mouseDoubleClickEvent(e)


class ShadowShell(QWidget):
  """Transparent outer widget of the frameless window; paints a soft drop shadow in its PAD-px margin
  (no QGraphicsDropShadowEffect: that would re-render the whole window content through a pixmap)."""
  PAD = 10
  RADIUS = 10                           # must match the border-radius of QFrame#root in theme.py

  def paintEvent(self, e):
    if self.window().isMaximized():
      return
    p = QPainter(self)
    p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    pad = self.PAD
    for i in range(pad):                # outermost ring is faintest, rings get darker towards the frame
      a = int(70 * ((i + 1) / pad) ** 2)
      p.setPen(QPen(QColor(0, 0, 0, a), 1))
      r = QRectF(self.rect().adjusted(i, i + 2, -i, -i + 2)).adjusted(0.5, 0.5, -0.5, -0.5)   # +2: shadow sits a bit lower
      p.drawRoundedRect(r, self.RADIUS + pad - i, self.RADIUS + pad - i)
    p.end()


class EdgeGrip(QWidget):
  """A thin strip on a window edge / corner of the frameless window: shows the resize cursor and starts a resize."""
  SIZE = ShadowShell.PAD                # the transparent margin around the frame doubles as the resize border

  def __init__(self, edges, cursor, kind: str):
    super().__init__()
    self._edges = edges
    self.setCursor(cursor)
    if kind == "h":                     # top / bottom edge
      self.setFixedHeight(self.SIZE)
    elif kind == "v":                   # left / right edge
      self.setFixedWidth(self.SIZE)
    else:                               # corner
      self.setFixedSize(self.SIZE, self.SIZE)

  def mousePressEvent(self, e):
    if e.button() == Qt.MouseButton.LeftButton:
      h = self.window().windowHandle()
      if h is not None:
        h.startSystemResize(self._edges)
        e.accept()
        return
    super().mousePressEvent(e)


class PreviewLabel(QLabel):
  """The vehicle picture: keeps the original pixmap and re-fits it whenever the label is resized."""

  MAX_UPSCALE = 2.0       # largest enlargement of the CDN image (1.0 = never beyond its own pixels: sharpest, but small)

  def __init__(self):
    super().__init__()
    self._pm: QPixmap | None = None
    self._pil = None                    # the original as a Pillow image, for the Lanczos enlargement
    self.setObjectName("preview")
    self.setAlignment(Qt.AlignmentFlag.AlignCenter)
    self.setMinimumSize(200, 140)
    self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Ignored)

  def show_text(self, text: str):
    self._pm = None
    self._pil = None
    self.setToolTip("")
    self.clear()
    self.setText(text)

  def show_pixmap(self, pm: QPixmap):
    self._pm = pm
    self._pil = None
    self.setToolTip(f"Source image: {pm.width()} x {pm.height()} px")
    self._fit()

  def resizeEvent(self, e):
    super().resizeEvent(e)
    self._fit()

  def _fit(self):
    pm = self._pm
    if pm is None or pm.isNull() or self.width() <= 4 or self.height() <= 4:
      return
    # Scale in DEVICE pixels and tell Qt the pixmap's device pixel ratio. Scaling to the logical label size (the old
    # code) made Qt stretch the result again on a display scaled above 100%, which softened it a second time.
    dpr = self.devicePixelRatioF()
    k = min(self.width() * dpr / pm.width(), self.height() * dpr / pm.height(), self.MAX_UPSCALE)
    size = QSize(max(1, round(pm.width() * k)), max(1, round(pm.height() * k)))
    out = self._enlarge(size) if k > 1.0 else None
    if out is None:
      out = pm.scaled(size, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)
    out.setDevicePixelRatio(dpr)
    self.setPixmap(out)

  def _enlarge(self, size: QSize) -> QPixmap | None:
    """Enlarge with Pillow's Lanczos filter plus a light unsharp mask: visibly crisper than Qt's bilinear stretch.
    Returns None when Pillow is missing or anything fails (the caller then uses Qt's own scaling)."""
    try:
      from PIL import Image, ImageFilter
      if self._pil is None:
        img = self._pm.toImage().convertToFormat(QImage.Format.Format_RGBA8888)
        self._pil = Image.frombytes("RGBA", (img.width(), img.height()), bytes(img.constBits()))
      im = self._pil.resize((size.width(), size.height()), Image.Resampling.LANCZOS)
      im = im.filter(ImageFilter.UnsharpMask(radius=1.2, percent=60, threshold=2))
      qi = QImage(im.tobytes(), im.width, im.height, im.width * 4, QImage.Format.Format_RGBA8888).copy()
      return QPixmap.fromImage(qi)
    except Exception:                   # noqa: BLE001
      return None


# ---------------------------------------------------------------------------------------- main window
class MainWindow(QMainWindow):

  def __init__(self, bridge: Bridge):
    super().__init__()
    self.bridge = bridge
    self.settings = QSettings("WTAssetExplorer", "WTAssetExplorer")
    self.cache_root = Path(
        QStandardPaths.writableLocation(
            QStandardPaths.StandardLocation.AppLocalDataLocation
        )
        or (Path.home() / ".wt_asset_explorer")
    )

    self.catalog: VehicleCatalog | None = None
    self.library: AssetLibrary | None = None
    self.current: Vehicle | None = None
    self._busy = 0
    self._jobs: set[Job] = set()
    self._pool = QThreadPool(self)
    self._pool.setMaxThreadCount(1)
    self._preview_token = 0
    self._loadout: LoadoutData | None = None      # weapon slots of the selected vehicle (None = it has none)
    self._selection: dict[int, str] = {}          # slot index -> preset name; empty = bare jet
    self.game_dir = ""                            # edited in Settings
    self.out_dir = ""
    self.all_armament = False                     # Settings: Export Armament takes every weapon of the vehicle
    self._status_ctx = ""                         # vehicle / weapon being exported, shown after the phase text
    # [REMOVED in cleanup] `self._weapon_asset_cache: dict[str, str] = {}` (wtx/gui.py line 261 of the pre-cleanup file, in the main window __init__):
    #   an empty dict meant to cache weapon asset names; it was assigned here and never read or written anywhere else.

    self.setWindowTitle("War Thunder Vehicle & Loadout Explorer")
    self.setWindowFlags(
        Qt.WindowType.Window | Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowMinMaxButtonsHint
    )
    self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)     # lets the root frame round its corners
    self.resize(1200, 736)
    icon_path = ROOT / "res" / "icon.ico"
    if icon_path.exists():
        self.setWindowIcon(QIcon(str(icon_path)))

    self._build_ui()
    geo = self.settings.value("geometry")
    if geo is not None:
      self.restoreGeometry(geo)         # size, position and maximised state of the last session

    self.net = QNetworkAccessManager(self)
    self.net.finished.connect(self._on_preview_reply)

    self._search_timer = QTimer(self, singleShot=True, interval=150)
    self._search_timer.timeout.connect(self._filter_vehicles)

    bridge.line.connect(self._on_log_line)
    bridge.progress.connect(self._on_progress)

    self._startup()

  # ------------------------------------------------------------------ UI
  def _build_ui(self):
    self.setStyleSheet(QSS)
    shell = ShadowShell()
    self.setCentralWidget(shell)
    grid = QGridLayout(shell)
    grid.setContentsMargins(0, 0, 0, 0)
    grid.setSpacing(0)
    E, C = Qt.Edge, Qt.CursorShape
    self._grips = []
    for row, col, edges, cur, kind in (
        (0, 0, E.TopEdge | E.LeftEdge, C.SizeFDiagCursor, "c"),
        (0, 1, E.TopEdge, C.SizeVerCursor, "h"),
        (0, 2, E.TopEdge | E.RightEdge, C.SizeBDiagCursor, "c"),
        (1, 0, E.LeftEdge, C.SizeHorCursor, "v"),
        (1, 2, E.RightEdge, C.SizeHorCursor, "v"),
        (2, 0, E.BottomEdge | E.LeftEdge, C.SizeBDiagCursor, "c"),
        (2, 1, E.BottomEdge, C.SizeVerCursor, "h"),
        (2, 2, E.BottomEdge | E.RightEdge, C.SizeFDiagCursor, "c"),
    ):
      grip = EdgeGrip(edges, cur, kind)
      self._grips.append(grip)
      grid.addWidget(grip, row, col)
    root = QFrame()
    root.setObjectName("root")
    self._root = root
    grid.addWidget(root, 1, 1)
    grid.setRowStretch(1, 1)
    grid.setColumnStretch(1, 1)
    frame = QVBoxLayout(root)
    frame.setContentsMargins(1, 1, 1, 1)
    frame.setSpacing(0)

    # --- title bar (logo, title, window buttons)
    self.titlebar = TitleBar(self)
    frame.addWidget(self.titlebar)

    body = QWidget()
    outer = QVBoxLayout(body)
    outer.setContentsMargins(14, 4, 14, 8)
    outer.setSpacing(10)
    frame.addWidget(body, 1)

    split = QSplitter()
    split.setChildrenCollapsible(False)
    split.setHandleWidth(10)
    outer.addWidget(split, 1)

    # --- left card: search, Nation / Type filters, vehicle list
    left = QFrame()
    left.setObjectName("card")
    lv = QVBoxLayout(left)
    lv.setContentsMargins(10, 10, 10, 10)
    lv.setSpacing(8)
    self.search = QLineEdit()
    self.search.setPlaceholderText("Search vehicle...")
    self.search.setClearButtonEnabled(True)
    self.search.setToolTip("Matches the vehicle name or its ID")
    self.search.textChanged.connect(lambda _t: self._search_timer.start())
    lv.addWidget(self.search)
    flt = QHBoxLayout()
    flt.addWidget(QLabel("Nation"))
    self.combo_nation = QComboBox()
    self.combo_nation.addItem("All Nations")
    self.combo_nation.currentIndexChanged.connect(self._filter_vehicles)
    flt.addWidget(self.combo_nation, 1)
    flt.addWidget(QLabel("Type"))
    self.combo_type = QComboBox()
    self.combo_type.addItems(["All Types"] + VEHICLE_TYPES)
    self.combo_type.currentIndexChanged.connect(self._filter_vehicles)
    flt.addWidget(self.combo_type, 1)
    lv.addLayout(flt)

    self.tree = QTreeWidget()
    self.tree.setHeaderLabels(["Vehicle", "Nation"])
    self.tree.setUniformRowHeights(True)
    self.tree.setRootIsDecorated(False)
    self.tree.setAlternatingRowColors(True)
    hdr = self.tree.header()
    hdr.setStretchLastSection(False)
    hdr.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
    hdr.setSectionResizeMode(1, QHeaderView.ResizeMode.Interactive)
    self.tree.setColumnWidth(1, 90)
    self.tree.itemSelectionChanged.connect(self._on_vehicle_selected)
    lv.addWidget(self.tree, 1)
    split.addWidget(left)

    # --- right column
    right = QWidget()
    r = QVBoxLayout(right)
    r.setContentsMargins(0, 0, 0, 0)
    r.setSpacing(10)

    # vehicle title + chips (nation / type / 3D asset)
    trow = QHBoxLayout()
    trow.setSpacing(8)
    self.lbl_name = QLabel("Select a vehicle")
    self.lbl_name.setObjectName("title")
    self.lbl_id = QLabel("")
    self.lbl_id.setObjectName("titleId")
    for lb in (self.lbl_name, self.lbl_id):
      lb.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
    trow.addWidget(self.lbl_name)
    trow.addWidget(self.lbl_id)
    trow.addStretch(1)
    self.btn_settings = QPushButton("Settings  \u2699")
    self.btn_settings.setObjectName("toolbtn")
    self.btn_settings.clicked.connect(self._open_settings)
    trow.addWidget(self.btn_settings)
    self.btn_outdir = QPushButton("Output Folder")
    self.btn_outdir.setObjectName("toolbtn")
    self.btn_outdir.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_DirOpenIcon))
    self.btn_outdir.setToolTip("Open the output folder set in Settings")
    self.btn_outdir.clicked.connect(self._open_output_folder)
    trow.addWidget(self.btn_outdir)
    r.addLayout(trow)

    self.chip_row = QWidget()
    crow = QHBoxLayout(self.chip_row)
    crow.setContentsMargins(0, 0, 0, 0)
    crow.setSpacing(6)
    self.lbl_nation, self.lbl_type, self.lbl_asset = QLabel(""), QLabel(""), QLabel("")
    for i, lb in enumerate((self.lbl_nation, self.lbl_type, self.lbl_asset)):
      lb.setObjectName("chip")
      lb.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
      if i:
        dot = QLabel("\u2022")
        dot.setObjectName("dot")
        crow.addWidget(dot)
      crow.addWidget(lb)
    crow.addStretch(1)
    self.chip_row.hide()
    r.addWidget(self.chip_row)

    self.preview = PreviewLabel()
    self.preview.show_text("Select a vehicle to load preview")
    r.addWidget(self.preview, 1)

    # loadout card (the panel itself is the card; hidden for vehicles without a choice)
    self.grp_loadout = LoadoutPanel()
    self.grp_loadout.message.connect(self._say)
    r.addWidget(self.grp_loadout)

    # --- export card
    card = QFrame()
    card.setObjectName("card")
    cv = QVBoxLayout(card)
    cv.setContentsMargins(14, 10, 14, 12)
    cv.setSpacing(10)
    et = QLabel("Export Parameters")
    et.setObjectName("cardTitle")
    cv.addWidget(et)

    row1 = QHBoxLayout()
    row1.setSpacing(12)
    row1.addWidget(QLabel("Export Format:"))
    self.combo_format = QComboBox()
    self.combo_format.addItems(["OBJ", "GLB"])
    self.combo_format.setToolTip("OBJ: .obj + .mtl + textures.  GLB: one binary glTF file (animations, loadout models)")
    self.combo_format.currentIndexChanged.connect(lambda _i: self._update_buttons())
    row1.addWidget(self.combo_format)
    self.chk_tex = QCheckBox("Textures")
    self.chk_tex.setToolTip("Also export the textures (and the MTL material file for OBJ)")
    self.chk_anim = QCheckBox("Animations")
    self.chk_anim.setToolTip("GLB only: also write the vehicle's .a2d animation (gear, flaps, ...) as glTF animations")
    self.chk_wpn = QCheckBox("Weapon models")
    self.chk_wpn.setToolTip(
        "Put the 3D models of the chosen loadout (missiles, bombs, racks, tanks) on the vehicle's weapon nodes"
    )
    for c in (self.chk_tex, self.chk_anim, self.chk_wpn):
      c.setChecked(True)
      row1.addWidget(c)
    row1.addStretch(1)
    self.btn_weapon = QPushButton("[ Export Armament ]")
    self.btn_weapon.setObjectName("pink")
    self.btn_vehicle = QPushButton("[ Export Vehicle ]")
    self.btn_vehicle.setObjectName("blue")
    self.btn_skin = QPushButton("Create skin template (.blk)")
    self.btn_skin.setToolTip(
        "Write a custom-skin folder: the vehicle's _c / _n textures (plus the chosen loadout's weapons if "
        "'Weapon models' is ticked) and the .blk that maps them. Edit the textures, then copy the folder "
        "into War Thunder/UserSkins."
    )
    self.btn_preview = QPushButton("Preview custom skin...")
    self.btn_preview.setToolTip(
        "Pick a custom-skin folder (the one 'Create skin template' wrote, after you painted it) and export the vehicle "
        "as OBJ or GLB (the format selected here) with that skin's textures on it, into its own 'skin_preview_...' folder."
    )
    for bt in (self.btn_weapon, self.btn_vehicle, self.btn_skin, self.btn_preview):
      bt.setMinimumHeight(34)
      bt.setEnabled(False)
    self.btn_weapon.setMinimumWidth(150)
    self.btn_vehicle.setMinimumWidth(130)
    row1.addWidget(self.btn_preview)
    row1.addWidget(self.btn_skin)
    row1.addWidget(self.btn_vehicle)
    cv.addLayout(row1)

    row2 = QHBoxLayout()
    row2.setSpacing(12)
    row2.addWidget(QLabel("Armament:"))
    self.combo_loadout = QComboBox()
    self.combo_loadout.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
    self.combo_loadout.setMinimumContentsLength(20)
    self.combo_loadout.setToolTip("The weapon that 'Export Armament' writes")
    self.combo_loadout.currentIndexChanged.connect(self._on_weapon_changed)
    row2.addWidget(self.combo_loadout, 1)
    row2.addWidget(self.btn_weapon)
    cv.addLayout(row2)
    r.addWidget(card)

    self.btn_vehicle.clicked.connect(self._export_vehicle)
    self.btn_weapon.clicked.connect(self._extract_weapon)
    self.btn_skin.clicked.connect(self._create_skin_template)
    self.btn_preview.clicked.connect(self._preview_custom_skin)

    split.addWidget(right)
    split.setSizes([340, 860])
    split.setStretchFactor(0, 0)
    split.setStretchFactor(1, 1)

    # --- log panel (hidden; the Logs button in the status bar toggles it)
    self.console = Console()
    self.console.setFixedHeight(170)
    self.console.hide()
    outer.addWidget(self.console)

    # --- status row: last message on the left, progress + Logs toggle on the right
    srow = QHBoxLayout()
    srow.setContentsMargins(2, 0, 2, 0)
    self.status_lbl = QLabel("Status: Starting...")
    self.status_lbl.setObjectName("muted")
    self.status_lbl.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
    srow.addWidget(self.status_lbl, 1)
    self.progress = QProgressBar()
    self.progress.setMaximumWidth(220)
    self.progress.setTextVisible(False)
    self.progress.hide()
    srow.addWidget(self.progress)
    self.btn_logs = QPushButton("Logs")
    self.btn_logs.setObjectName("logs")
    self.btn_logs.setCheckable(True)
    self.btn_logs.setToolTip("Show / hide the log panel")
    self.btn_logs.toggled.connect(self._toggle_logs)
    srow.addWidget(self.btn_logs)
    outer.addLayout(srow)
  # ------------------------------------------------------------------ jobs / busy state
  def _say(self, text: str):
    self.bridge.line.emit(text, 3)

  def _run(self, fn, ok, label: str = ""):
    job = Job(fn)
    self._jobs.add(job)
    self._set_busy(+1)

    def finish():
      self._jobs.discard(job)
      self._set_busy(-1)

    def on_ok(res):
      finish()
      ok(res)

    def on_fail(tb):
      finish()
      self.bridge.line.emit(tb.rstrip(), 2)
      self.bridge.line.emit(
          f"[ERROR] {label or 'Task'} failed - see the trace above.", 2
      )
      self._finish_status(f"{label or 'Task'} failed - open Logs for details")

    job.signals.done.connect(on_ok)
    job.signals.failed.connect(on_fail)
    self._pool.start(job)

  def _set_busy(self, delta: int):
    self._busy = max(0, self._busy + delta)
    busy = self._busy > 0
    self.progress.setVisible(busy)
    if busy and self.progress.maximum() != 0 and self.progress.value() <= 0:
      self.progress.setRange(0, 0)
    if not busy:
      self.progress.setRange(0, 100)
      self.progress.setValue(0)
    self._update_buttons()

  def _on_progress(self, done: int, total: int):
    if total > 0:
      self.progress.setRange(0, total)
      self.progress.setValue(done)

  def _update_buttons(self):
    ready = (
        self.library is not None
        and self.current is not None
        and self._busy == 0
    )
    self.btn_vehicle.setEnabled(ready)
    self.btn_skin.setEnabled(ready)
    self.btn_preview.setEnabled(ready)
    self.chk_anim.setEnabled(self._is_glb())          # animations only exist in the GLB writer

    has_weapon = (self._selected_weapon() is not None) or (
        self.all_armament
        and self.combo_loadout.count() > 0
        and self.combo_loadout.itemText(0) != "No mounted weapons found"
    )
    self.btn_weapon.setEnabled(ready and has_weapon)

  def _is_glb(self) -> bool:
    return self.combo_format.currentText() == "GLB"

  def _export_vehicle(self):
    if self._is_glb():
      self._extract_vehicle_gltf()
    else:
      self._extract_vehicle()
  # ------------------------------------------------------------------ startup / loading
  def _startup(self):
    s = self.settings
    self.out_dir = str(s.value("out_dir", str(Path.home() / "Documents" / "WT Exports")))
    game = str(s.value("game_dir", "") or "")
    if not game:
      found = auto_find_wt_path()
      game = str(found) if found else ""
    self.game_dir = game
    self.all_armament = s.value("all_armament", False, type=bool)
    self.grp_loadout.set_use_icons(s.value("loadout_icons", True, type=bool))
    fmt = str(s.value("export_format", "OBJ"))
    self.combo_format.setCurrentIndex(max(0, self.combo_format.findText(fmt)))
    self.chk_tex.setChecked(s.value("opt_tex", True, type=bool))
    self.chk_anim.setChecked(s.value("opt_anim", True, type=bool))
    self.chk_wpn.setChecked(s.value("opt_wpn", True, type=bool))
    dae_log.setSink(lambda text, kind: self.bridge.line.emit(text, int(kind)))
    self._say("War Thunder Asset Explorer ready.")
    if game and looks_like_wt_dir(Path(game)):
      self._start_loading()
    else:
      self._say(
          "Open Settings and point 'Game folder' at your War Thunder installation (the folder"
          " that contains aces.vromfs.bin)."
      )
      self._finish_status("Set the game folder in Settings")
      QTimer.singleShot(0, self._open_settings)

  def _open_settings(self):
    dlg = SettingsDialog(self, self.game_dir, self.out_dir, self.all_armament, self.grp_loadout.use_icons)
    if dlg.exec() != QDialog.DialogCode.Accepted:
      return
    old_game = self.game_dir
    self.game_dir, self.out_dir = dlg.game_dir(), dlg.out_dir()
    self.all_armament = dlg.all_armament()
    self.grp_loadout.set_use_icons(dlg.loadout_icons())
    s = self.settings
    s.setValue("game_dir", self.game_dir)
    s.setValue("out_dir", self.out_dir)
    s.setValue("all_armament", self.all_armament)
    s.setValue("loadout_icons", dlg.loadout_icons())
    self._update_buttons()
    if dlg.reload_mode:
      self._start_loading(use_cache=dlg.reload_mode != "clear")
    elif self.game_dir and self.game_dir != old_game:
      self._start_loading()

  def _open_output_folder(self):
    out = self.out_dir.strip()
    if not out:
      self._say("No output folder set - choose one in Settings.")
      self._open_settings()
      return
    path = Path(out)
    try:
      path.mkdir(parents=True, exist_ok=True)
    except OSError as e:
      self._say(f"[WARNING] cannot create the output folder {path}: {e}")
      return
    QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))

  def _toggle_logs(self, on: bool):
    self.console.setVisible(on)

  def _on_log_line(self, text: str, kind: int = 0):
    self.console.add(text, kind)
    if self._busy:
      line = text.strip().strip("=").strip()
      if line and kind != 2 and "[warning]" not in line.lower():
        for rx, phase in _PHASES:
          if rx.search(line):
            self._set_status(phase, with_ctx=True)
            break

  def _set_status(self, text: str, with_ctx: bool = False):
    if with_ctx and self._status_ctx:
      text = f"{text} ({self._status_ctx})"
    self.status_lbl.setText("Status: " + text)

  def _finish_status(self, text: str):
    self._status_ctx = ""
    self._set_status(text)

  def _begin_status(self, name: str, what: str = "Exporting"):
    self._status_ctx = name
    self._set_status(f"{what} {name}...")
  def _start_loading(self, use_cache: bool = True):
    if self._busy:
      self._say("Busy - wait for the current task to finish.")
      return
    game = Path(self.game_dir.strip().strip('"'))
    if not looks_like_wt_dir(game):
      QMessageBox.warning(
          self,
          APP_NAME,
          "That doesn't look like a War Thunder folder:\naces.vromfs.bin was"
          " not found in it.",
      )
      return
    wt_ext = find_wt_ext(ROOT)
    if wt_ext is None:
      p, _ = QFileDialog.getOpenFileName(
          self, "Locate wt_ext_cli.exe", str(ROOT), "wt_ext_cli.exe (*.exe)"
      )
      if not p:
        return
      wt_ext = Path(p)
    self.settings.setValue("game_dir", str(game))

    # reset state
    self.library = self.catalog = self.current = None
    self.tree.clear()
    self._clear_details()
    say = self._say
    cache_root = self.cache_root

    def work():
      catalog = VehicleCatalog(game, wt_ext, cache_root)
      if not use_cache:
        catalog.clear_cache()
        for f in cache_root.glob("asset_index_*.json"):
          f.unlink(missing_ok=True)
      catalog.extract(say, use_cache=use_cache)
      font_path = ensure_game_font(game, wt_ext, cache_root, say)  # vehicle-name icons
      catalog.scan(say)
      library = AssetLibrary(game, cache_root)
      library.build_index(say, lambda d, t: self.bridge.progress.emit(d, t))
      return catalog, library, font_path

    def ok(res):
      self.catalog, self.library, font_path = res
      if register_symbol_font(QApplication.instance(), font_path):
        # vehicle names: icon characters must come from the game's font, not the UI font
        self.tree.setFont(symbols_first(self.tree.font()))
        self.lbl_name.setFont(symbols_first(self.lbl_name.font()))
      try:
        rep = name_symbol_report(self.catalog.vehicles)
        (self.cache_root / "name_symbols.txt").write_text("\n".join(rep), encoding="utf-8")
      except Exception:  # noqa: BLE001 - diagnostics only
        pass
      self._fill_nations()
      self._filter_vehicles()
      self._say(
          f"Ready - {len(self.catalog.vehicles)} vehicles. Select one and"
          " extract it."
      )
      self._update_buttons()

      def descs_done(_r):
        say("Material descriptions loaded.")
        self._finish_status("Ready for Export" if self.current else "Ready for Export - select a vehicle")

      self._finish_status("Loading material descriptions...")
      self._run(
          lambda: self.library.preload_base_descs(say),
          descs_done,
          "Loading material descriptions",
      )

    say(f"Loading War Thunder data from {game}")
    self._finish_status("Reading game files...")
    self._run(work, ok, "Loading")

  # ------------------------------------------------------------------ vehicle list
  def _fill_nations(self):
    self.combo_nation.blockSignals(True)
    self.combo_nation.clear()
    self.combo_nation.addItem("All Nations")
    self.combo_nation.addItems(self.catalog.nations())
    self.combo_nation.blockSignals(False)

  def _filter_vehicles(self):
    if not self.catalog or not getattr(self.catalog, "vehicles", None):
      return

    nation = self.combo_nation.currentText()
    vtype = self.combo_type.currentText()
    q = self.search.text().strip().lower()
    items = []

    for veh in self.catalog.vehicles:
      v_id = str(veh.id or "").lower()
      v_name = str(veh.name or "").lower()
      v_nation = str(veh.nation or "N/A")
      v_type = str(veh.type or "N/A")

      # Filter by Nation
      if nation != "All Nations" and v_nation.lower() != nation.lower():
        continue

      # Filter by Type
      if vtype != "All Types" and v_type.lower() != vtype.lower():
        continue

      # Filter by Search Term
      if q and not (q in v_id or q in v_id.replace("_", "-") or q in v_name):
        continue

      it = QTreeWidgetItem([
          str(veh.name or "N/A"),
          v_nation,
      ])
      it.setData(0, Qt.ItemDataRole.UserRole, veh)
      it.setToolTip(0, f"{veh.id}  ({v_type})")
      items.append(it)

    self.tree.setUpdatesEnabled(False)
    self.tree.blockSignals(True)
    self.tree.clear()
    self.tree.addTopLevelItems(items)
    self.tree.blockSignals(False)
    self.tree.setUpdatesEnabled(True)

    self.current = None
    self._clear_details()

  def _clear_details(self):
    self.lbl_name.setText("Select a vehicle")
    self.lbl_id.setText("")
    self.chip_row.hide()
    self.combo_loadout.clear()
    self._loadout, self._selection = None, {}
    self.grp_loadout.hide()
    self.preview.show_text("Select a vehicle to load preview")
    self._update_buttons()
  def _on_vehicle_selected(self):
    sel = self.tree.selectedItems()
    if not sel or not self.catalog:
      return
    veh: Vehicle = sel[0].data(0, Qt.ItemDataRole.UserRole)
    self.current = veh
    self.lbl_name.setText(veh.name)
    self.lbl_id.setText(f"({veh.id})")
    self.lbl_nation.setText(veh.nation)
    self.lbl_type.setText(veh.type)
    self.lbl_asset.setText("3D Asset: -")
    self.chip_row.show()

    if self.library:
      refs = self.library.find_vehicle_models(veh.id, veh.model_id)
      if refs:
        self.lbl_asset.setText(f"3D Asset: {Path(str(refs[0].grp_file)).name}")
      else:
        self.lbl_asset.setText("3D Asset: not matched automatically - you'll be asked to pick one")

    self.combo_loadout.blockSignals(True)
    self.combo_loadout.clear()
    weapons = self.catalog.weapons(veh)
    if weapons:
      for i, (name, w_id) in enumerate(weapons):
        shown = name if name and name != "N/A" else w_id
        self.combo_loadout.addItem(shown, (name, w_id))
        self.combo_loadout.setItemData(i, w_id, Qt.ItemDataRole.ToolTipRole)
    else:
      self.combo_loadout.addItem("No mounted weapons found")
    self.combo_loadout.blockSignals(False)

    self._build_loadout_ui(veh)
    self._load_preview(veh.id)
    self._update_buttons()
    if self._busy == 0 and self.library:
      self._finish_status("Export finished")
  def _selected_weapon(self) -> str | None:
    info = self._selected_weapon_info()
    return info[1] if info else None

  def _selected_weapon_info(self) -> tuple[str, str] | None:
    if not self.combo_loadout.count():
      return None
    data = self.combo_loadout.currentData()
    if isinstance(data, tuple) and len(data) == 2:
      return data
    elif isinstance(data, str):
      return ("N/A", data)
    return None

  def _on_weapon_changed(self):
    self._update_buttons()

  # ------------------------------------------------------------------ loadout
  @staticmethod
  def _shrinkable(combo: QComboBox):
    """Long entries must not stretch the window: the box stays narrow, the popup list shows the full text."""
    combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
    combo.setMinimumContentsLength(18)

  def _build_loadout_ui(self, veh: Vehicle):
    """Icon cells for the vehicle's weapon slots, filled from its BLK. Hidden when no slot offers a choice."""
    data = self.catalog.loadout(veh) if self.catalog else None
    self._loadout = data
    try:
      name_for = self.catalog.weapon_display_name if self.catalog else None
      self.grp_loadout.set_data(data, getattr(self.catalog, "icons", None), name_for)
    except Exception as e:                  # a loadout problem must never block the vehicle or its export
      self._loadout = None
      self.grp_loadout.set_data(None)
      self._say(f"[WARNING] loadout panel skipped: {type(e).__name__}: {e}")

  def _clear_loadout(self):
    self.grp_loadout.clear()

  def _loadout_hidden(self) -> set[str] | None:
    """Node names to leave out of a vehicle export, or None when the vehicle has no loadout data (export unchanged)."""
    if self._loadout is None or not self.grp_loadout.isVisible():
      return None
    return self._loadout.hidden_nodes(self.grp_loadout.selection())

  def _loadout_mirrored(self) -> set[str] | None:
    """Vehicle pod nodes the game draws on the other side than the model file (GLB export), or None."""
    if self._loadout is None or not self.grp_loadout.isVisible():
      return None
    try:
      return self._loadout.mirrored_nodes(self.grp_loadout.selection()) or None
    except Exception as e:
      self._say(f"[WARNING] pod mirroring skipped: {type(e).__name__}: {e}")
      return None

  def _loadout_mounts(self):
    """[(slot, preset, Mount)] of the chosen loadout for the GLB export, or None (no loadout / option off)."""
    if self._loadout is None or not self.grp_loadout.isVisible() or not self.chk_wpn.isChecked():
      return None
    try:
      return self._loadout.mounts(self.grp_loadout.selection()) or None
    except Exception as e:
      self._say(f"[WARNING] weapon models skipped: {type(e).__name__}: {e}")
      return None

  def _loadout_summary(self) -> str:
    return self.grp_loadout.summary()

  # ------------------------------------------------------------------ preview image
  def _load_preview(self, vid: str):
    self._preview_token += 1
    self.preview.show_text("Loading image...")
    cands = list(
        dict.fromkeys([vid, vid.replace("_", "-"), vid.replace("-", "_")])
    )
    self._request_preview(self._preview_token, cands)

  def _request_preview(self, token: int, cands: list[str]):
    if not cands:
      self.preview.show_text("Image not found on CDN")
      return
    req = QNetworkRequest(QUrl(CDN.format(cands[0])))
    req.setRawHeader(b"User-Agent", UA)
    reply = self.net.get(req)
    reply.setProperty("token", token)
    reply.setProperty("rest", cands[1:])

  def _on_preview_reply(self, reply: QNetworkReply):
    token, rest = reply.property("token"), reply.property("rest") or []
    data = (
        reply.readAll()
        if reply.error() == QNetworkReply.NetworkError.NoError
        else None
    )
    reply.deleteLater()
    if token != self._preview_token:
      return
    if data is not None:
      pm = QPixmap()
      if pm.loadFromData(data) and not pm.isNull():
        self.preview.show_pixmap(pm)
        return
    self._request_preview(token, list(rest))

  # ------------------------------------------------------------------ extraction
  def _out_dir(self) -> Path | None:
    out = self.out_dir.strip().strip('"')
    if not out:
      QMessageBox.information(self, APP_NAME, "Choose an output folder in Settings first.")
      return None
    return Path(out)

  def _pick_asset(self, key: str, title: str, prefill: str) -> AssetRef | None:
    dlg = AssetPicker(self, self.library.all_models(), title, prefill)
    if dlg.exec() != QDialog.DialogCode.Accepted:
      return None
    ref = dlg.chosen()
    if ref:
      self.library.remember(key, ref)
    return ref

  def _vehicle_dir(self, veh: Vehicle, out: Path) -> Path:
    display_or_id = veh.name if veh.name and veh.name != "N/A" else veh.id
    return (
        out
        / safe_name(veh.nation)
        / safe_name(veh.type)
        / safe_name(display_or_id)
    )

  def _extract_vehicle(self, skin_dir=None):
    veh, lib = self.current, self.library
    if not veh:
      self._say("No vehicle selected in the list.")
      return
    if not lib:
      self._say("Asset library not ready.")
      return
    out = self._out_dir()
    if not out:
      return

    refs = lib.find_vehicle_models(veh.id, veh.model_id)
    if refs:
      ref = refs[0]
      if len(refs) > 1:
        self._say(
            f"{len(refs)} candidate models; using '{ref.name}'. Others: "
            + ", ".join(r.name for r in refs[1:4])
        )
    else:
      self._say(
          f"No 3D model matched '{veh.model_id}' / '{veh.id}' automatically."
      )
      ref = self._pick_asset(
          f"vehicle:{veh.id}",
          f"Pick the 3D model for {veh.name}",
          veh.model_id,
      )
      if not ref:
        return
    hide = self._loadout_hidden()
    mounts = self._loadout_mounts()
    mirror = self._loadout_mirrored()
    if hide is not None:
      self._say(f"Loadout: {self._loadout_summary()}")
    dest = self._vehicle_dir(veh, out)
    if skin_dir:
      dest = dest / f"skin_preview_{safe_name(Path(skin_dir).name)}"
    self._export(ref, dest, f"vehicle {veh.name}", hide_nodes=hide,
                 mounts=mounts, mirror_nodes=mirror, skin_dir=skin_dir)

  def _extract_vehicle_gltf(self, skin_dir=None):
    veh, lib = self.current, self.library
    if not veh:
      self._say("No vehicle selected in the list.")
      return
    if not lib:
      self._say("Asset library not ready.")
      return
    out = self._out_dir()
    if not out:
      return

    refs = lib.find_vehicle_models(veh.id, veh.model_id)
    if refs:
      ref = refs[0]
      if len(refs) > 1:
        self._say(
            f"{len(refs)} candidate models; using '{ref.name}'. Others: "
            + ", ".join(r.name for r in refs[1:4])
        )
    else:
      self._say(
          f"No 3D model matched '{veh.model_id}' / '{veh.id}' automatically."
      )
      ref = self._pick_asset(
          f"vehicle:{veh.id}",
          f"Pick the 3D model for {veh.name}",
          veh.model_id,
      )
      if not ref:
        return

    # Use export_gltf instead of export_obj
    textures = self.chk_tex.isChecked()
    animations = self.chk_anim.isChecked()
    say = self._say
    dest_dir = self._vehicle_dir(veh, out)
    if skin_dir:
      dest_dir = dest_dir / f"skin_preview_{safe_name(Path(skin_dir).name)}"
    hide = self._loadout_hidden()
    mounts = self._loadout_mounts()
    mirror = self._loadout_mirrored()
    self._begin_status(veh.name)
    say(f"=== Extracting vehicle as glb: {ref.name} ({ref.grp_file}) ===")
    if hide is not None:
      say(f"Loadout: {self._loadout_summary()}")

    def work():
      return self.library.export_gltf(ref, dest_dir, say, textures=textures, animations=animations, hide_nodes=hide,
                                      mounts=mounts, mirror_nodes=mirror, skin_dir=skin_dir)

    def ok(path):
      say(f"=== Done: {path} ===")
      self._finish_status(f"{veh.name} exported")

    self._run(work, ok, "Extracting vehicle as glb")

  def _preview_custom_skin(self):
    veh = self.current
    if not veh or not self.library:
      self._say("Select a vehicle first.")
      return
    from wtx.skin_blk import read_skin_folder
    start = str(self.settings.value("skin_preview_dir", "") or "")
    out = Path(self.out_dir)
    guess = self._vehicle_dir(veh, out) / f"skin_{safe_name(veh.id)}"
    if guess.is_dir():
      start = str(guess)
    folder = QFileDialog.getExistingDirectory(self, "Choose the custom-skin folder", start or str(out))
    if not folder:
      return
    if not read_skin_folder(folder):
      self._say(f"No skin textures (.tga / .png / .dds) found in {folder}.")
      return
    self.settings.setValue("skin_preview_dir", folder)
    if not self.chk_tex.isChecked():
      self._say("Textures were off: turning them on, the skin preview needs them.")
      self.chk_tex.setChecked(True)
    self._say(f"Skin preview of {veh.name} with the textures of '{Path(folder).name}'.")
    if self._is_glb():
      self._extract_vehicle_gltf(skin_dir=folder)
    else:
      self._extract_vehicle(skin_dir=folder)

  def _create_skin_template(self):
    veh, lib = self.current, self.library
    if not veh:
      self._say("No vehicle selected in the list.")
      return
    if not lib:
      self._say("Asset library not ready.")
      return
    out = self._out_dir()
    if not out:
      return

    refs = lib.find_vehicle_models(veh.id, veh.model_id)
    if refs:
      ref = refs[0]
    else:
      self._say(f"No 3D model matched '{veh.model_id}' / '{veh.id}' automatically.")
      ref = self._pick_asset(f"vehicle:{veh.id}", f"Pick the 3D model for {veh.name}", veh.model_id)
      if not ref:
        return

    levels = []
    if self.chk_wpn.isChecked():
      for _slot, _preset, mount in (self._loadout_mounts() or []):
        levels.extend(mount.levels)
    skin_dir = self._vehicle_dir(veh, out) / f"skin_{safe_name(veh.id)}"
    say = self._say
    self._begin_status(veh.name, "Creating skin template for")
    say(f"=== Creating skin template for {veh.name} ({veh.id}) ===")
    if levels:
      say("Including the textures of the weapon models in the chosen loadout.")

    def work():
      return lib.export_skin_template(ref, skin_dir, veh.id, say, weapon_levels=levels, vehicle_name=veh.name)

    def ok(res):
      blk, count = res
      say(f"=== Done: {count} texture(s) + {blk.name} in {skin_dir} ===")
      say("Edit the .tga files (keep their names; the uv_guides_ROUGH folder shows where each part sits, INSTRUCTIONS.txt explains the .blk). 'Preview custom skin...' shows your painting on the model. "
          "Then copy the whole folder into War Thunder/UserSkins and pick it under 'User skin' in the vehicle's "
          "customization screen.")
      self._finish_status(f"{veh.name} skin template created")

    self._run(work, ok, "Creating skin template")

  def _extract_weapon(self):
    veh, lib, cat = self.current, self.library, self.catalog
    if not veh:
      self._say("No vehicle selected in the list.")
      return
    if not (lib and cat):
      self._say("Asset library or catalog not ready.")
      return
    out = self._out_dir()
    if not out:
      return

    textures = self.chk_tex.isChecked()
    gltf = self._is_glb()

    if self.all_armament:
      weapons = cat.weapons(veh)
      if not weapons:
        self._say(f"No mounted weapons found for {veh.name}.")
        return

      say = self._say
      self._begin_status(veh.name, "Exporting armament of")
      say(
          f"=== Starting batch extraction of {len(weapons)} weapon(s) for"
          f" {veh.name} ==="
      )

      def work_batch():
        success_count = 0
        for w_name, wid in weapons:
          if not wid:
            continue
          hints = cat.weapon_model_hints(wid)
          refs = lib.find_weapon_models(wid, hints)
          if refs:
            ref = refs[0]
            folder_name = safe_name(
                w_name if w_name and w_name != "N/A" else wid
            )
            dest_dir = self._vehicle_dir(veh, out) / "armament" / folder_name
            say(f"--> Extracting armament: {w_name} ({wid})")
            if gltf:
              lib.export_gltf(ref, dest_dir, say, textures=textures, animations=False)
            else:
              lib.export_obj(ref, dest_dir, say, textures=textures)
            success_count += 1
          else:
            say(
                "[Warning] No 3D model found automatically for weapon"
                f" '{w_name}' ({wid}) - skipping."
            )
        return success_count

      def ok_batch(count):
        say(
            "=== Done: Extracted"
            f" {count}/{len(weapons)} weapons for {veh.name} ==="
        )
        self._finish_status(f"{veh.name}: {count} armament model(s) exported")

      self._run(
          work_batch, ok_batch, f"Batch extracting armament for {veh.name}"
      )
      return

    w_info = self._selected_weapon_info()
    if not w_info or not w_info[1]:
      self._say("No weapon selected in the dropdown.")
      return

    w_name, wid = w_info
    hints = cat.weapon_model_hints(wid)
    refs = lib.find_weapon_models(wid, hints)
    if refs:
      ref = refs[0]
      if len(refs) > 1:
        self._say(
            f"{len(refs)} candidate models; using '{ref.name}'. Others: "
            + ", ".join(r.name for r in refs[1:4])
        )
    else:
      self._say(
          f"No 3D model matched weapon '{wid}'"
          + (f" (BLK mentions: {', '.join(hints)})" if hints else "")
          + "."
      )
      ref = self._pick_asset(
          f"weapon:{wid}",
          f"Pick the 3D model for {w_name if w_name != 'N/A' else wid}",
          hints[0] if hints else wid,
      )
      if not ref:
        return

    folder_name = safe_name(w_name if w_name and w_name != "N/A" else wid)
    self._export(
        ref,
        self._vehicle_dir(veh, out) / "armament" / folder_name,
        f"armament {w_name if w_name != 'N/A' else wid}",
        gltf=gltf,
    )

  def _export(self, ref: AssetRef, out_dir: Path, label: str, hide_nodes=None, mounts=None, mirror_nodes=None,
              gltf: bool = False, skin_dir=None):
    textures = self.chk_tex.isChecked()
    say = self._say
    name = label.split(" ", 1)[-1]                 # "vehicle <name>" / "armament <name>"
    self._begin_status(name)
    say(f"=== Extracting {label}: {ref.name} ({ref.grp_file}) ===")

    def work():
      if gltf:
        return self.library.export_gltf(ref, out_dir, say, textures=textures, animations=False)
      return self.library.export_obj(ref, out_dir, say, textures=textures, hide_nodes=hide_nodes,
                                     mounts=mounts, mirror_nodes=mirror_nodes, skin_dir=skin_dir)

    def ok(path):
      say(f"=== Done: {path} ===")
      self._finish_status(f"{name} exported")

    self._run(work, ok, f"Extracting {label}")

  def _sync_max_state(self):
    self.titlebar.sync()
    for grip in self._grips:                        # no resizing / shadow margin while maximised
      grip.setVisible(not self.isMaximized())
    self._root.setProperty("max", self.isMaximized())       # stylesheet: square corners, no border
    self._root.style().unpolish(self._root)
    self._root.style().polish(self._root)

  def changeEvent(self, e):
    super().changeEvent(e)
    if e.type() == QEvent.Type.WindowStateChange and hasattr(self, "titlebar"):
      self._sync_max_state()

  def showEvent(self, e):
    super().showEvent(e)
    if hasattr(self, "titlebar"):                   # window restored as maximised: no state change event fires
      self._sync_max_state()

  def closeEvent(self, e):
    s = self.settings
    s.setValue("geometry", self.saveGeometry())
    s.setValue("out_dir", self.out_dir)
    s.setValue("export_format", self.combo_format.currentText())
    s.setValue("opt_tex", self.chk_tex.isChecked())
    s.setValue("opt_anim", self.chk_anim.isChecked())
    s.setValue("opt_wpn", self.chk_wpn.isChecked())
    super().closeEvent(e)


# ---------------------------------------------------------------------------------------- entry point
def _dark_fallback(app: QApplication):
  app.setStyle("Fusion")
  p = QPalette()
  p.setColor(QPalette.ColorRole.Window, QColor(32, 32, 32))
  p.setColor(QPalette.ColorRole.WindowText, QColor(235, 235, 235))
  p.setColor(QPalette.ColorRole.Base, QColor(40, 40, 40))
  p.setColor(QPalette.ColorRole.AlternateBase, QColor(45, 45, 45))
  p.setColor(QPalette.ColorRole.Text, QColor(235, 235, 235))
  p.setColor(QPalette.ColorRole.Button, QColor(52, 52, 52))
  p.setColor(QPalette.ColorRole.ButtonText, QColor(235, 235, 235))
  p.setColor(QPalette.ColorRole.Highlight, QColor(58, 150, 221))
  p.setColor(QPalette.ColorRole.HighlightedText, QColor(255, 255, 255))
  p.setColor(QPalette.ColorRole.PlaceholderText, QColor(140, 140, 140))
  app.setPalette(p)


def run() -> int:
  try:
    ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
        "WTAssetExplorer.WTAssetExplorer.1"
    )
  except Exception:
    pass

  app = QApplication(sys.argv)
  app.setApplicationName(APP_NAME)

  icon_path = ROOT / "res" / "icon.ico"
  if icon_path.exists():
    app.setWindowIcon(QIcon(str(icon_path)))

  try:
    app.styleHints().setColorScheme(Qt.ColorScheme.Dark)
    if app.style().objectName().lower() != "windows11":
      _dark_fallback(app)
  except Exception:
    _dark_fallback(app)

  bridge = Bridge()
  sys.stdout = TeeStream(bridge, sys.__stdout__, 0)
  sys.stderr = TeeStream(bridge, sys.__stderr__, 2)
  win = MainWindow(bridge)
  win.show()
  return app.exec()