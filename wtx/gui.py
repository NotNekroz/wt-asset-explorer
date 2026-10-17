"""Main window: War Thunder Vehicle & Loadout Explorer + Dagor Asset Explorer in one interface."""

from __future__ import annotations

import ctypes
from pathlib import Path
import re
import sys
import traceback
from io import BytesIO

from PySide6.QtCore import (
    QObject,
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
    QFont,
    QIcon,
    QPalette,
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
    QSizePolicy,
    QSplitter,
    QToolButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

import util.log as dae_log
from vehicle_core.VehicleLook import auto_find_wt_path
from wtx import ROOT
from wtx.assets import AssetLibrary, AssetRef
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
    self._weapon_asset_cache: dict[str, str] = {}

    self.setWindowTitle("War Thunder Vehicle & Loadout Explorer")
    self.resize(1200, 736)
    icon_path = ROOT / "res" / "icon.ico"
    if icon_path.exists():
        self.setWindowIcon(QIcon(str(icon_path)))

    self._build_ui()

    self.net = QNetworkAccessManager(self)
    self.net.finished.connect(self._on_preview_reply)

    self._search_timer = QTimer(self, singleShot=True, interval=150)
    self._search_timer.timeout.connect(self._filter_vehicles)

    bridge.line.connect(self.console.add)
    bridge.progress.connect(self._on_progress)

    self._startup()

  # ------------------------------------------------------------------ UI
  def _build_ui(self):
    root = QWidget()
    self.setCentralWidget(root)
    v = QVBoxLayout(root)

    # --- paths
    paths = QHBoxLayout()
    paths.addWidget(QLabel("Game folder:"))
    self.game_edit = QLineEdit()
    self.game_edit.setPlaceholderText(r"...\steamapps\common\War Thunder")
    self.game_edit.returnPressed.connect(self._load_from_edit)
    paths.addWidget(self.game_edit, 3)
    b = QPushButton("Browse...")
    b.clicked.connect(self._browse_game)
    paths.addWidget(b)
    self.reload_btn = QToolButton()
    self.reload_btn.setText("Reload")
    self.reload_btn.setPopupMode(
        QToolButton.ToolButtonPopupMode.MenuButtonPopup
    )
    self.reload_btn.clicked.connect(
        lambda: self._start_loading(use_cache=True)
    )
    menu = QMenu(self.reload_btn)
    menu.addAction("Reload").triggered.connect(
        lambda: self._start_loading(use_cache=True)
    )
    menu.addAction("Clear cache and reload").triggered.connect(
        lambda: self._start_loading(use_cache=False)
    )
    self.reload_btn.setMenu(menu)
    paths.addWidget(self.reload_btn)
    paths.addSpacing(14)
    paths.addWidget(QLabel("Output:"))
    self.out_edit = QLineEdit()
    self.out_edit.setPlaceholderText("Where the OBJ files go")
    paths.addWidget(self.out_edit, 2)
    b2 = QPushButton("Browse...")
    b2.clicked.connect(self._browse_out)
    paths.addWidget(b2)
    v.addLayout(paths)

    # --- filter bar
    flt = QHBoxLayout()
    flt.addWidget(QLabel("Nation:"))
    self.combo_nation = QComboBox()
    self.combo_nation.addItem("All Nations")
    self.combo_nation.currentIndexChanged.connect(self._filter_vehicles)
    flt.addWidget(self.combo_nation)
    flt.addWidget(QLabel("Type:"))
    self.combo_type = QComboBox()
    self.combo_type.addItems(["All Types"] + VEHICLE_TYPES)
    self.combo_type.currentIndexChanged.connect(self._filter_vehicles)
    flt.addWidget(self.combo_type)
    flt.addWidget(QLabel("Search:"))
    self.search = QLineEdit()
    self.search.setPlaceholderText("Search vehicle name or ID...")
    self.search.textChanged.connect(lambda _t: self._search_timer.start())
    flt.addWidget(self.search, 1)
    v.addLayout(flt)

    split = QSplitter()
    v.addWidget(split, 1)

    # --- left: vehicle list
    self.tree = QTreeWidget()
    self.tree.setHeaderLabels(["Vehicle Name", "ID", "Nation", "Type"])
    self.tree.setUniformRowHeights(True)
    self.tree.setRootIsDecorated(False)

    hdr = self.tree.header()
    hdr.setStretchLastSection(False)
    for c in range(4):
      hdr.setSectionResizeMode(c, QHeaderView.ResizeMode.Interactive)

    self.tree.setColumnWidth(0, 220)
    self.tree.setColumnWidth(1, 160)
    self.tree.setColumnWidth(2, 80)
    self.tree.setColumnWidth(3, 80)

    self.tree.itemSelectionChanged.connect(self._on_vehicle_selected)
    split.addWidget(self.tree)

    # --- right: details, preview, extract buttons, console
    right = QWidget()
    r = QVBoxLayout(right)
    r.setContentsMargins(6, 0, 0, 0)

    form = QFormLayout()
    self.lbl_name, self.lbl_id, self.lbl_nation = (
        QLabel("N/A"),
        QLabel("N/A"),
        QLabel("N/A"),
    )
    self.lbl_type, self.lbl_model, self.lbl_asset = (
        QLabel("N/A"),
        QLabel("N/A"),
        QLabel("-"),
    )
    for lbl in (
        self.lbl_name,
        self.lbl_id,
        self.lbl_nation,
        self.lbl_type,
        self.lbl_model,
        self.lbl_asset,
    ):
      lbl.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
    form.addRow("Display Name:", self.lbl_name)
    form.addRow("Vehicle ID:", self.lbl_id)
    form.addRow("Nation:", self.lbl_nation)
    form.addRow("Type:", self.lbl_type)
    form.addRow("Model ID:", self.lbl_model)
    form.addRow("3D Asset:", self.lbl_asset)
    self.combo_loadout = QComboBox()
    self.combo_loadout.setMinimumWidth(300)
    self.combo_loadout.currentIndexChanged.connect(self._on_weapon_changed)
    form.addRow("Available Weapons:", self.combo_loadout)
    r.addLayout(form)

    self.preview = QLabel("Select a vehicle to load preview")
    self.preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
    self.preview.setStyleSheet(
        "border: 1px solid #444; background: #1a1a1a; color:#999;"
    )
    self.preview.setMinimumHeight(260)
    self.preview.setSizePolicy(
        QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding
    )
    r.addWidget(self.preview, 1)

    btns = QHBoxLayout()
    self.btn_vehicle = QPushButton("Extract vehicle as OBJ")
    self.btn_weapon = QPushButton("Extract armament as OBJ")
    for bt in (self.btn_vehicle, self.btn_weapon):
      bt.setMinimumHeight(34)
      bt.setEnabled(False)
      btns.addWidget(bt, 1)

    self.chk_all_armaments = QCheckBox("All Armament")
    self.chk_all_armaments.setChecked(False)
    self.chk_all_armaments.setToolTip(
        "Export all available armament models for this vehicle at once"
    )
    self.chk_all_armaments.toggled.connect(lambda _c: self._update_buttons())
    btns.addWidget(self.chk_all_armaments)

    self.chk_tex = QCheckBox("Textures")
    self.chk_tex.setChecked(True)
    self.chk_tex.setToolTip(
        "Also export the DDS textures and the MTL material file"
    )
    btns.addWidget(self.chk_tex)

    self.btn_vehicle.clicked.connect(self._extract_vehicle)
    self.btn_weapon.clicked.connect(self._extract_weapon)
    r.addLayout(btns)

    self.console = Console()
    self.console.setSizePolicy(
        QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding
    )
    self.console.setMinimumHeight(110)
    r.addWidget(self.console, 1)

    split.addWidget(right)
    split.setSizes([520, 660])
    split.setStretchFactor(1, 1)

    self.progress = QProgressBar()
    self.progress.setMaximumWidth(220)
    self.progress.setTextVisible(False)
    self.progress.hide()
    self.statusBar().addPermanentWidget(self.progress)

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

    has_weapon = (self._selected_weapon() is not None) or (
        self.chk_all_armaments.isChecked()
        and self.combo_loadout.count() > 0
        and self.combo_loadout.itemText(0) != "No mounted weapons found"
    )
    self.btn_weapon.setEnabled(ready and has_weapon)

  # ------------------------------------------------------------------ startup / loading
  def _startup(self):
    s = self.settings
    self.out_edit.setText(
        s.value("out_dir", str(Path.home() / "Documents" / "WT Exports"))
    )
    game = s.value("game_dir", "")
    if not game:
      found = auto_find_wt_path()
      game = str(found) if found else ""
    self.game_edit.setText(game)
    dae_log.setSink(lambda text, kind: self.bridge.line.emit(text, int(kind)))
    self._say("War Thunder Asset Explorer ready.")
    if game and looks_like_wt_dir(Path(game)):
      self._start_loading()
    else:
      self._say(
          "Point 'Game folder' at your War Thunder installation (the folder"
          " that contains aces.vromfs.bin)."
      )

  def _browse_game(self):
    start = self.game_edit.text() or str(Path.home())
    d = QFileDialog.getExistingDirectory(
        self, "Select the War Thunder folder", start
    )
    if d:
      self.game_edit.setText(d)
      self._load_from_edit()

  def _browse_out(self):
    d = QFileDialog.getExistingDirectory(
        self, "Select the output folder", self.out_edit.text() or str(Path.home())
    )
    if d:
      self.out_edit.setText(d)
      self.settings.setValue("out_dir", d)

  def _load_from_edit(self):
    self._start_loading()

  def _start_loading(self, use_cache: bool = True):
    if self._busy:
      self._say("Busy - wait for the current task to finish.")
      return
    game = Path(self.game_edit.text().strip().strip('"'))
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
      catalog.scan(say)
      library = AssetLibrary(game, cache_root)
      library.build_index(say, lambda d, t: self.bridge.progress.emit(d, t))
      return catalog, library

    def ok(res):
      self.catalog, self.library = res
      self._fill_nations()
      self._filter_vehicles()
      self._say(
          f"Ready - {len(self.catalog.vehicles)} vehicles. Select one and"
          " extract it."
      )
      self._update_buttons()
      self._run(
          lambda: self.library.preload_base_descs(say),
          lambda _r: say("Material descriptions loaded."),
          "Loading material descriptions",
      )

    say(f"Loading War Thunder data from {game}")
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
          str(veh.id or "N/A"),
          v_nation,
          v_type,
      ])
      it.setData(0, Qt.ItemDataRole.UserRole, veh)
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
    for lbl in (
        self.lbl_name,
        self.lbl_id,
        self.lbl_nation,
        self.lbl_type,
        self.lbl_model,
    ):
      lbl.setText("N/A")
    self.lbl_asset.setText("-")
    self.combo_loadout.clear()
    self.preview.clear()
    self.preview.setText("Select a vehicle to load preview")
    self._update_buttons()

  def _on_vehicle_selected(self):
    sel = self.tree.selectedItems()
    if not sel or not self.catalog:
      return
    veh: Vehicle = sel[0].data(0, Qt.ItemDataRole.UserRole)
    self.current = veh
    self.lbl_name.setText(veh.name)
    self.lbl_id.setText(veh.id)
    self.lbl_nation.setText(veh.nation)
    self.lbl_type.setText(veh.type)
    self.lbl_model.setText(veh.model_id)

    if self.library:
      refs = self.library.find_vehicle_models(veh.id, veh.model_id)
      if refs:
        self.lbl_asset.setText(f"{refs[0].name}  ({refs[0].grp_file})")
      else:
        self.lbl_asset.setText(
            "not matched automatically - you'll be asked to pick one"
        )

    self.combo_loadout.blockSignals(True)
    self.combo_loadout.clear()
    weapons = self.catalog.weapons(veh)
    if weapons:
      for name, w_id in weapons:
        self.combo_loadout.addItem(f"{name} ({w_id})", (name, w_id))
    else:
      self.combo_loadout.addItem("No mounted weapons found")
    self.combo_loadout.blockSignals(False)

    self._load_preview(veh.id)
    self._update_buttons()

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

  # ------------------------------------------------------------------ preview image
  def _load_preview(self, vid: str):
    self._preview_token += 1
    self.preview.clear()
    self.preview.setText("Loading image...")
    cands = list(
        dict.fromkeys([vid, vid.replace("_", "-"), vid.replace("-", "_")])
    )
    self._request_preview(self._preview_token, cands)

  def _request_preview(self, token: int, cands: list[str]):
    if not cands:
      self.preview.setText("Image not found on CDN")
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
        self.preview.setPixmap(
            pm.scaled(
                self.preview.size(),
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
        )
        return
    self._request_preview(token, list(rest))

  # ------------------------------------------------------------------ extraction
  def _out_dir(self) -> Path | None:
    out = self.out_edit.text().strip().strip('"')
    if not out:
      QMessageBox.information(self, APP_NAME, "Choose an output folder first.")
      return None
    self.settings.setValue("out_dir", out)
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

  def _extract_vehicle(self):
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
    self._export(ref, self._vehicle_dir(veh, out), f"vehicle {veh.name}")

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

    if self.chk_all_armaments.isChecked():
      weapons = cat.weapons(veh)
      if not weapons:
        self._say(f"No mounted weapons found for {veh.name}.")
        return

      say = self._say
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
    )

  def _export(self, ref: AssetRef, out_dir: Path, label: str):
    textures = self.chk_tex.isChecked()
    say = self._say
    say(f"=== Extracting {label}: {ref.name} ({ref.grp_file}) ===")

    def work():
      return self.library.export_obj(ref, out_dir, say, textures=textures)

    def ok(path):
      say(f"=== Done: {path} ===")

    self._run(work, ok, f"Extracting {label}")

  def closeEvent(self, e):
    self.settings.setValue("out_dir", self.out_edit.text())
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