"""settings_dialog.py - everything configurable that is not on the main window."""
from __future__ import annotations

from pathlib import Path

from PySide6.QtWidgets import (
    QCheckBox, QDialog, QFileDialog, QHBoxLayout, QLabel, QLineEdit, QPushButton, QSpinBox, QVBoxLayout,
)

from wtx import tuning as _tuning


class SettingsDialog(QDialog):
    """Paths, reload / cache and a few export / display options. The caller reads the values after exec()."""

    def __init__(self, parent, game_dir: str, out_dir: str, all_armament: bool, loadout_icons: bool,
                 tuning: dict | None = None):
        super().__init__(parent)
        tune = _tuning.normalize(tuning)             # None = the starting values (environment variables / built-in defaults)
        self.setWindowTitle("Settings")
        self.setMinimumWidth(640)
        self.reload_mode: str | None = None          # "cache" / "clear" when a reload button closed the dialog

        lay = QVBoxLayout(self)
        lay.setContentsMargins(18, 16, 18, 16)
        lay.setSpacing(8)

        lay.addWidget(self._heading("War Thunder"))
        lay.addWidget(QLabel("Game folder (the folder that contains aces.vromfs.bin)"))
        row = QHBoxLayout()
        self.game_edit = QLineEdit(game_dir)
        self.game_edit.setPlaceholderText(r"...\steamapps\common\War Thunder")
        row.addWidget(self.game_edit, 1)
        b = QPushButton("Browse...")
        b.clicked.connect(self._browse_game)
        row.addWidget(b)
        lay.addLayout(row)
        row = QHBoxLayout()
        self.btn_reload = QPushButton("Reload game data")
        self.btn_reload.setToolTip("Save the settings and reload the vehicle list (uses the cache)")
        self.btn_reload.clicked.connect(lambda: self._finish("cache"))
        self.btn_clear = QPushButton("Clear cache and reload")
        self.btn_clear.setToolTip("Save the settings, throw the cached vehicle / asset index away and rebuild it")
        self.btn_clear.clicked.connect(lambda: self._finish("clear"))
        row.addWidget(self.btn_reload)
        row.addWidget(self.btn_clear)
        row.addStretch(1)
        lay.addLayout(row)

        lay.addSpacing(8)
        lay.addWidget(self._heading("Export"))
        lay.addWidget(QLabel("Output Folder"))
        row = QHBoxLayout()
        self.out_edit = QLineEdit(out_dir)
        self.out_edit.setPlaceholderText("Where the exported files go")
        row.addWidget(self.out_edit, 1)
        b2 = QPushButton("Browse...")
        b2.clicked.connect(self._browse_out)
        row.addWidget(b2)
        lay.addLayout(row)
        self.chk_all = QCheckBox("[ Export Armament ] exports every weapon of the vehicle (ignores the weapon dropdown)")
        self.chk_all.setChecked(bool(all_armament))
        lay.addWidget(self.chk_all)

        lay.addSpacing(8)
        lay.addWidget(self._heading("Display"))
        self.chk_icons = QCheckBox("Show weapon icons in the loadout slots (off = weapon names)")
        self.chk_icons.setChecked(bool(loadout_icons))
        lay.addWidget(self.chk_icons)

        lay.addSpacing(8)
        lay.addWidget(self._heading("Performance"))
        row = QHBoxLayout()
        row.addWidget(QLabel("Texture workers"))
        self.spin_tex = QSpinBox()
        self.spin_tex.setRange(0, _tuning.TEX_WORKERS_MAX)
        self.spin_tex.setValue(tune["tex_workers"])
        self.spin_tex.setToolTip("Threads that convert and write textures at the end of an export stage. 0 or 1 = no pool (one after the other).")
        row.addWidget(self.spin_tex)
        row.addSpacing(16)
        row.addWidget(QLabel("Pack workers"))
        self.spin_pack = QSpinBox()
        self.spin_pack.setRange(0, _tuning.PACK_WORKERS_MAX)
        self.spin_pack.setValue(tune["pack_workers"])
        self.spin_pack.setToolTip("Threads that parse texture packs which are not in the pack cache yet. 0 or 1 = one after the other.")
        row.addWidget(self.spin_pack)
        row.addSpacing(16)
        row.addWidget(QLabel("glTF map workers"))
        self.spin_map = QSpinBox()
        self.spin_map.setRange(0, _tuning.MAP_WORKERS_MAX)
        self.spin_map.setValue(tune["map_workers"])
        self.spin_map.setToolTip("Threads that build the glTF normal and metal-roughness pictures before the materials are written. "
                                 "0 or 1 = one after the other. Each thread holds a few copies of a texture in memory at a time.")
        row.addWidget(self.spin_map)
        row.addStretch(1)
        lay.addLayout(row)
        self.chk_clear_packs = QCheckBox("Start every export with an empty texture-pack cache (for fair comparisons)")
        self.chk_clear_packs.setChecked(tune["clear_pack_cache"])
        self.chk_clear_packs.setToolTip("Every export parses its texture packs again. The operating system's file cache cannot be cleared, "
                                        "so the first run after a boot is still colder than the following ones.")
        lay.addWidget(self.chk_clear_packs)
        self.chk_profile = QCheckBox("Profile the slow phases (adds a function-level report after the [timing] table; slows those phases down)")
        self.chk_profile.setChecked(tune["profile"])
        lay.addWidget(self.chk_profile)
        note = QLabel("Applied immediately, no restart needed. The [timing] report starts with a line that shows these values.")
        note.setWordWrap(True)
        lay.addWidget(note)

        lay.addSpacing(10)
        row = QHBoxLayout()
        row.addStretch(1)
        cancel = QPushButton("Cancel")
        cancel.clicked.connect(self.reject)
        save = QPushButton("Save")
        save.setObjectName("blue")
        save.setDefault(True)
        save.clicked.connect(self.accept)
        row.addWidget(cancel)
        row.addWidget(save)
        lay.addLayout(row)

    @staticmethod
    def _heading(text: str) -> QLabel:
        lb = QLabel(text)
        lb.setObjectName("cardTitle")
        return lb

    def _finish(self, mode: str) -> None:
        self.reload_mode = mode
        self.accept()

    def _browse_game(self) -> None:
        d = QFileDialog.getExistingDirectory(self, "Select the War Thunder folder",
                                             self.game_edit.text() or str(Path.home()))
        if d:
            self.game_edit.setText(d)

    def _browse_out(self) -> None:
        d = QFileDialog.getExistingDirectory(self, "Select the output folder",
                                             self.out_edit.text() or str(Path.home()))
        if d:
            self.out_edit.setText(d)

    # values ---------------------------------------------------------------------------------
    def game_dir(self) -> str:
        return self.game_edit.text().strip().strip('"')

    def out_dir(self) -> str:
        return self.out_edit.text().strip().strip('"')

    def all_armament(self) -> bool:
        return self.chk_all.isChecked()

    def loadout_icons(self) -> bool:
        return self.chk_icons.isChecked()

    def tuning(self) -> dict:
        return _tuning.normalize({
            "tex_workers": self.spin_tex.value(),
            "pack_workers": self.spin_pack.value(),
            "map_workers": self.spin_map.value(),
            "clear_pack_cache": self.chk_clear_packs.isChecked(),
            "profile": self.chk_profile.isChecked(),
        })
