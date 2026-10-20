"""loadout_panel.py - the loadout selector: one icon cell per weapon slot, like the game's loadout editor.

* cells: 100x100 icon buttons in the game's order (slot tier ascending = left to right; the gun is not drawn)
* clicking a cell opens the list of that slot's weapons, grouped by category, each with icon, short name and mass
* a slot with a single possible preset is dimmed and cannot be clicked (it is filled by the game's dependency rules)
* "Mass: X / Y kg" above the cells; the stock-loadout drop-down fills the cells

All rules (dependencies, bans, mass, grouping) live in vehicle_core.Loadout; this file only draws them.
"""
from __future__ import annotations

import textwrap
from typing import Callable, Optional

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtGui import QColor, QFont, QGuiApplication, QIcon, QPainter, QPixmap
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QFrame, QGroupBox, QHBoxLayout, QLabel, QListWidget, QListWidgetItem, QPushButton,
    QScrollArea, QToolButton, QVBoxLayout, QWidget,
)

from vehicle_core.Icons import IconStore
from vehicle_core.Loadout import CHAPTER_TITLES, LoadoutData, PresetOption, Slot, fmt_kg

CELL = 72                        # cell size in px
ICON = 44
_HEADER = Qt.ItemDataRole.UserRole + 1


def _dimmed(pix: QPixmap, opacity: float = 0.35) -> QPixmap:
    out = QPixmap(pix.size())
    out.fill(Qt.GlobalColor.transparent)
    p = QPainter(out)
    p.setOpacity(opacity)
    p.drawPixmap(0, 0, pix)
    p.end()
    return out


class _ChoicePopup(QFrame):
    """The grouped weapon list of one slot. Emits the preset name ('' = empty the slot)."""
    picked = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent, Qt.WindowType.Popup)
        self.setFrameShape(QFrame.Shape.StyledPanel)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(2, 2, 2, 2)
        self.list = QListWidget()
        self.list.setIconSize(QSize(32, 32))
        self.list.setUniformItemSizes(False)
        self.list.itemClicked.connect(self._clicked)
        lay.addWidget(self.list)

    def _clicked(self, item: QListWidgetItem):
        if item.data(_HEADER) or not (item.flags() & Qt.ItemFlag.ItemIsEnabled):
            return
        self.hide()
        self.picked.emit(item.data(Qt.ItemDataRole.UserRole) or "")


class LoadoutPanel(QGroupBox):
    changed = Signal()           # the selection changed (by the user or by a stock loadout)
    message = Signal(str)        # one-line notes for the status log

    def __init__(self, parent=None):
        super().__init__("Loadout Configuration", parent)
        self._data: Optional[LoadoutData] = None
        self._icons: Optional[IconStore] = None
        self._name_for: Optional[Callable[[str], Optional[str]]] = None
        self._sel: dict[int, str] = {}
        self._cells: dict[int, QToolButton] = {}
        self._pixcache: dict[str, Optional[QPixmap]] = {}
        self._icon_warned: set[str] = set()        # missing-icon reasons already written to the log
        self._popup = _ChoicePopup(self)
        self._popup.picked.connect(self._on_picked)
        self._popup_slot: Optional[int] = None
        self.use_icons = False                   # set from Settings (main window); off = weapon names

        lv = QVBoxLayout(self)
        lv.setContentsMargins(14, 0, 14, 12)
        lv.setSpacing(10)
        top = QHBoxLayout()
        top.setSpacing(10)
        top.addWidget(QLabel("Stock loadout:"))
        self.combo_stock = QComboBox()
        self.combo_stock.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.combo_stock.setMinimumContentsLength(14)
        self.combo_stock.currentIndexChanged.connect(self._on_stock_changed)
        top.addWidget(self.combo_stock)
        # [REMOVED] `self.mass_label = QLabel("")` + `top.addWidget(self.mass_label)` (wtx/loadout_panel.py, __init__, top row,
        #   after combo_stock): the "Mass: X / Y kg" label next to the stock-loadout combo.
        top.addStretch(1)
        btn_clear = QPushButton("Clear All Slots")
        btn_clear.clicked.connect(self.clear)
        top.addWidget(btn_clear)
        lv.addLayout(top)
        # the icon switch lives in Settings now; the checkbox stays as the state holder (hidden)
        self.chk_icons = QCheckBox("Icons", self)
        self.chk_icons.toggled.connect(self._on_icons_toggled)
        self.chk_icons.hide()

        self.area = QScrollArea()
        self.area.setWidgetResizable(True)
        self.area.setFrameShape(QFrame.Shape.NoFrame)
        self.area.setFixedHeight(CELL + 26)          # one row of cells + the horizontal scroll bar
        self.host = QWidget()
        self.row = QHBoxLayout(self.host)
        self.row.setContentsMargins(0, 0, 0, 0)
        self.row.setSpacing(2)
        self.row.addStretch(1)
        self.area.setWidget(self.host)
        lv.addWidget(self.area)
        self.hide()

    # ------------------------------------------------------------------ public
    def selection(self) -> dict[int, str]:
        return dict(self._sel)

    def data(self) -> Optional[LoadoutData]:
        return self._data

    def summary(self) -> str:
        if not self._sel:
            return "bare jet (no slot chosen)"
        return ", ".join(f"slot {s}: {p}" for s, p in sorted(self._sel.items()))

    def set_data(self, data: Optional[LoadoutData], icons: Optional[IconStore] = None,
                 name_for: Optional[Callable[[str], Optional[str]]] = None) -> None:
        """Fill the panel for a vehicle. None (or a vehicle whose slots offer no choice) hides it."""
        self._data, self._icons, self._name_for = data, icons, name_for
        self._sel = {}
        self._pixcache = {}
        self._popup.hide()
        self._clear_row()
        self.combo_stock.blockSignals(True)
        self.combo_stock.clear()
        self.combo_stock.blockSignals(False)
        if data is None or not data.has_choices:
            self._data = None
            self.setVisible(False)
            return
        self.combo_stock.blockSignals(True)
        self.combo_stock.addItem("Custom / none", None)
        for st in data.stock:
            self.combo_stock.addItem(st.label, st.name)
        self.combo_stock.blockSignals(False)
        for slot in data.cells:
            btn = QToolButton()
            btn.setObjectName("slot")
            btn.setFixedSize(CELL, CELL)
            btn.setIconSize(QSize(ICON, ICON))
            btn.setAutoRaise(False)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.clicked.connect(lambda _c=False, s=slot.index: self._open_popup(s))
            self.row.insertWidget(self.row.count() - 1, btn)
            self._cells[slot.index] = btn
        self._refresh()
        self.setVisible(True)

    def set_use_icons(self, on: bool) -> None:
        """Show the weapon icons (True) or the weapon names (False) in the slot cells."""
        self.chk_icons.setChecked(bool(on))        # fires _on_icons_toggled -> refresh

    def clear(self) -> None:
        self._set_selection({}, announce=False)
        self.combo_stock.blockSignals(True)
        self.combo_stock.setCurrentIndex(0)
        self.combo_stock.blockSignals(False)

    # ------------------------------------------------------------------ internals
    def _clear_row(self) -> None:
        for b in self._cells.values():
            self.row.removeWidget(b)
            b.deleteLater()
        self._cells = {}

    def _display(self, stem: str) -> str:
        name = None
        if self._name_for:
            try:
                name = self._name_for(stem)
            except Exception:
                name = None
        return name or stem.replace("_", " ")

    def _title(self, opt: PresetOption) -> str:
        names = []
        for e in opt.entries:
            n = self._display(e.stem)
            if n not in names:
                names.append(n)
        return ", ".join(names) or opt.label

    def _details(self, opt: PresetOption) -> str:
        """Tooltip: every weapon of the preset with ammo and mass, like the game's list entry."""
        if not opt.entries:
            return opt.label
        parts = []
        for e in opt.entries:
            ammo = f"Ammo: {e.ammo}, " if e.ammo else ""
            parts.append(f"{self._display(e.stem)} ({ammo}Mass: {fmt_kg(e.mass_kg)} kg)")
        return "\n".join(parts)

    def _pixmap(self, icon: str) -> Optional[QPixmap]:
        if icon in self._pixcache:
            return self._pixcache[icon]
        pix = None
        if self._icons is not None:
            path = self._icons.path(icon)
            if path:
                q = QPixmap(str(path))
                pix = q if not q.isNull() else None
        self._pixcache[icon] = pix
        return pix

    def _report_missing_icon(self, name: str) -> None:
        """Icons are on but one could not be shown: say why in the log (once per reason), instead of silently showing names."""
        store = self._icons
        why = "no icon store (the game data was loaded without icons)" if store is None else store.why_missing(name)
        key = why.split(":")[0] if "folder" in why else (why.split(" in ")[0] if why.startswith("no ") else why)
        if why and key not in self._icon_warned:
            self._icon_warned.add(key)
            self.message.emit(f"[warning] weapon icons not shown ({name or '?'}): {why}")

    def _icon_for(self, opt: PresetOption, dim: bool = False, size: int = ICON) -> Optional[QIcon]:
        pix = self._pixmap(opt.icon)
        if pix is None:
            return None
        pix = pix.scaled(size, size, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)
        return QIcon(_dimmed(pix) if dim else pix)

    def _refresh(self) -> None:
        d = self._data
        if d is None:
            return
        for slot in d.cells:
            btn = self._cells.get(slot.index)
            if btn is None:
                continue
            chosen = d.option(slot.index, self._sel[slot.index]) if slot.index in self._sel else None
            shown = chosen              # only what is really selected; a slot's lone preset is not "chosen" until a
            btn.setText("")             # dependency (or the user) puts it there, so it never reaches the export unseen
            btn.setIcon(QIcon())
            if shown is not None:
                ic = self._icon_for(shown) if self.use_icons else None
                if ic is None and self.use_icons:
                    self._report_missing_icon(shown.icon)
                if ic is not None:
                    btn.setIcon(ic)
                    btn.setText(textwrap.shorten(self._title(shown), 12, placeholder="..."))
                    btn.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextUnderIcon)
                else:
                    btn.setText(textwrap.fill(self._title(shown), 11, max_lines=4, placeholder="..."))
                    btn.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextOnly)
                tip = self._details(shown)
            else:
                tip = f"Slot {slot.index}: empty - click to choose"
                btn.setText("+")
                btn.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextOnly)
            btn.setToolTip(tip)
            empty = shown is None
            if btn.property("empty") != empty:             # lets the stylesheet draw the "+" bigger
                btn.setProperty("empty", empty)
                btn.style().unpolish(btn)
                btn.style().polish(btn)
            btn.setCheckable(False)
            btn.setEnabled(True)
        # [REMOVED] end of _refresh (wtx/loadout_panel.py, ~lines 257-264): filled mass_label with "Mass: <total> / <max> kg"
        #   from d.total_mass(self._sel) and d.max_mass, coloured it red (#e05050, bold) when over the limit, else grey (#9a9a9a),
        #   with the tooltip "Over the aircraft's weapon load limit". d.total_mass / d.max_mass in vehicle_core/Loadout.py are unchanged.

    def _set_selection(self, sel: dict[int, str], announce: bool = True) -> None:
        d = self._data
        if d is None:
            return
        valid = {s.index for s in d.cells}
        clean = {s: p for s, p in sel.items() if s in valid and d.option(s, p) is not None}
        old = self._sel
        self._sel = clean
        self._refresh()
        if announce:
            for s in sorted((set(old) | set(clean))):
                if old.get(s) != clean.get(s):
                    self.message.emit(f"Loadout: slot {s} -> {clean[s]}" if s in clean else f"Loadout: slot {s} cleared")
        self.changed.emit()

    def _open_popup(self, slot_index: int) -> None:
        d = self._data
        slot = next((s for s in d.cells if s.index == slot_index), None) if d else None
        if slot is None or not slot.presets:
            return                                               # a slot with one weapon still toggles it on / off
        self._popup_slot = slot_index
        lst = self._popup.list
        lst.clear()
        cur = self._sel.get(slot_index)
        empty = QListWidgetItem("- empty -")
        empty.setData(Qt.ItemDataRole.UserRole, "")
        lst.addItem(empty)
        gold = QColor("#d9b43a")
        bold = QFont(lst.font())
        bold.setBold(True)
        for chapter, items in d.groups(slot):
            head = QListWidgetItem("- " + CHAPTER_TITLES.get(chapter, "Other"))
            head.setData(_HEADER, True)
            head.setFlags(Qt.ItemFlag.NoItemFlags)
            head.setForeground(gold)
            head.setFont(bold)
            lst.addItem(head)
            for opt in items:
                it = QListWidgetItem(f"{self._title(opt)}  ({fmt_kg(opt.mass_kg)} kg)")
                it.setData(Qt.ItemDataRole.UserRole, opt.name)
                ic = self._icon_for(opt, size=32) if self.use_icons else None
                if ic is not None:
                    it.setIcon(ic)
                tip = self._details(opt)
                if d.banned(self._sel, slot_index, opt.name):
                    it.setFlags(Qt.ItemFlag.NoItemFlags)
                    tip += "\n(not available with the weapons chosen in the other slots)"
                if opt.req_mod:
                    tip += "\nNeeds: " + ", ".join(opt.req_mod)
                it.setToolTip(tip)
                if opt.name == cur:
                    f = QFont(lst.font())
                    f.setBold(True)
                    it.setFont(f)
                lst.addItem(it)
        btn = self._cells[slot_index]
        w = max(420, min(640, lst.sizeHintForColumn(0) + 60))
        h = min(560, lst.sizeHintForRow(0) * min(lst.count(), 18) + 14)
        # Keep the list on the screen the slot is on: below the slot when it fits, otherwise on whichever side has more
        # room, shortened to fit (the list scrolls), and shifted left if it would run off the right edge.
        scr = btn.screen() or QGuiApplication.primaryScreen()
        area = scr.availableGeometry()
        below = btn.mapToGlobal(btn.rect().bottomLeft())
        above = btn.mapToGlobal(btn.rect().topLeft())
        room_below = area.bottom() - below.y() - 8
        room_above = above.y() - area.top() - 8
        if h <= room_below or room_below >= room_above:
            h = max(120, min(h, room_below))
            y = below.y()
        else:
            h = max(120, min(h, room_above))
            y = above.y() - h
        w = min(w, area.width() - 16)
        x = max(area.left() + 8, min(below.x(), area.right() - w - 8))
        self._popup.resize(w, h)
        self._popup.move(x, y)
        self._popup.show()

    def _on_icons_toggled(self, on: bool) -> None:
        self.use_icons = bool(on)
        self._refresh()

    def _on_picked(self, preset: str) -> None:
        if self._data is None or self._popup_slot is None:
            return
        slot = self._popup_slot
        old = dict(self._sel)
        new = self._data.choose(old, slot, preset or None)
        for s in sorted((set(old) | set(new)) - {slot}):         # slots the game's dependency rules moved along
            if old.get(s) != new.get(s):
                self.message.emit(f"Loadout: slot {s} -> {new[s]} (required by slot {slot})" if s in new
                                  else f"Loadout: slot {s} cleared (it belonged to slot {slot})")
        self._set_selection(new, announce=False)
        if preset:
            self.message.emit(f"Loadout: slot {slot} -> {preset}")
        self.combo_stock.blockSignals(True)
        self.combo_stock.setCurrentIndex(0)
        self.combo_stock.blockSignals(False)

    def _on_stock_changed(self) -> None:
        name = self.combo_stock.currentData()
        if not name or self._data is None:
            return
        self._set_selection(self._data.stock_selection(name), announce=False)
        self.message.emit(f"Loadout: stock loadout '{self.combo_stock.currentText()}' loaded -> {self.summary()}")
