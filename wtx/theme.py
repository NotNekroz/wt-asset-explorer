"""theme.py - the dark stylesheet of the main window (colours taken from the design mock-up)."""
from __future__ import annotations

BG = "#121212"
CARD = "#1a1a1a"
FIELD = "#141414"
BORDER = "#2c2c2c"
TEXT = "#e6e6e6"
MUTED = "#9a9a9a"
ACCENT = "#3aa0f0"
PINK = "#ff7ab8"
SELECT = "#1f3c5c"

QSS = f"""
QMainWindow {{ background:transparent; }}
QDialog {{ background:{BG}; }}
QWidget {{ color:{TEXT}; font-size:13px; }}
QLabel {{ background:transparent; }}
QLabel#appTitle {{ font-size:14px; }}
QFrame#root {{ background:{BG}; border:1px solid #2a2a2a; border-radius:10px; }}
QFrame#root[max="true"] {{ border-radius:0px; border:none; }}
QWidget#titlebar {{ background:transparent; }}
QPushButton#winbtn, QPushButton#winclose {{ background:transparent; border:none; border-radius:0; padding:0; color:{TEXT}; }}
QPushButton#winbtn:hover {{ background:#2a2a2a; }}
QPushButton#winclose {{ border-top-right-radius:9px; }}
QPushButton#winclose:hover {{ background:#c42b1c; color:white; }}
QSplitter::handle {{ background:{BG}; border:none; }}
QLabel#title {{ font-size:22px; font-weight:600; }}
QLabel#titleId {{ font-size:22px; color:{MUTED}; }}
QLabel#chip {{ background:#232323; border:1px solid #343434; border-radius:4px; padding:1px 8px; color:{MUTED}; font-size:12px; }}
QLabel#dot {{ color:#555; }}
QLabel#cardTitle {{ font-size:15px; font-weight:600; }}
QLabel#muted {{ color:{MUTED}; }}
QLabel#preview {{ background:qlineargradient(x1:0,y1:0,x2:0,y2:1,stop:0 #161616,stop:1 #0d0d0d); border:1px solid {BORDER}; border-radius:8px; color:{MUTED}; }}
QFrame#card, QGroupBox {{ background:{CARD}; border:1px solid {BORDER}; border-radius:8px; }}
QGroupBox {{ margin-top:0; padding-top:34px; }}
QGroupBox::title {{ subcontrol-origin:padding; subcontrol-position:top left; left:14px; top:9px; font-size:15px; font-weight:600; color:{TEXT}; }}
QLineEdit, QComboBox {{ background:{FIELD}; border:1px solid {BORDER}; border-radius:6px; padding:5px 8px; selection-background-color:{SELECT}; }}
QLineEdit:focus, QComboBox:focus {{ border-color:{ACCENT}; }}
QComboBox::drop-down {{ border:none; width:22px; }}
QComboBox QAbstractItemView {{ background:{CARD}; border:1px solid #3a3a3a; selection-background-color:{SELECT}; outline:0; }}
QTreeWidget, QListWidget {{ background:{FIELD}; border:1px solid {BORDER}; border-radius:6px; alternate-background-color:#181818; outline:0; }}
QTreeWidget::item {{ padding:3px 2px; }}
QTreeWidget::item:selected, QListWidget::item:selected {{ background:{SELECT}; color:white; }}
QHeaderView::section {{ background:#1d1d1d; color:{MUTED}; border:none; border-bottom:1px solid {BORDER}; padding:6px 8px; }}
QPushButton {{ background:#242424; border:1px solid #3a3a3a; border-radius:6px; padding:6px 14px; }}
QPushButton:hover {{ background:#2d2d2d; }}
QPushButton:disabled {{ color:#666; background:#1c1c1c; border-color:#2a2a2a; }}
QPushButton#toolbtn {{ background:#2a2a2a; border:1px solid #3d3d3d; border-radius:6px; padding:6px 14px; }}
QPushButton#toolbtn:hover {{ background:#353535; border-color:#4a4a4a; }}
QPushButton#pink {{ background:#33202b; color:{PINK}; border:1px solid #6b3652; font-weight:600; }}
QPushButton#pink:hover {{ background:#40283a; }}
QPushButton#pink:disabled {{ color:#7a5a6b; background:#241b21; border-color:#3a2a33; }}
QPushButton#blue {{ background:{ACCENT}; color:#06121f; border:none; font-weight:600; }}
QPushButton#blue:hover {{ background:#55b0f5; }}
QPushButton#blue:disabled {{ background:#24405a; color:#6d8499; }}
QPushButton#logs {{ background:transparent; border:none; padding:2px 10px; color:{MUTED}; }}
QPushButton#logs:checked {{ color:{TEXT}; background:#262626; border-radius:4px; }}
QToolButton#slot {{ background:#202020; border:1px solid #333; border-radius:6px; color:{MUTED}; font-size:10px; }}
QToolButton#slot:hover {{ border-color:{ACCENT}; }}
QToolButton#slot[empty="true"] {{ font-size:24px; color:#bbb; }}
QCheckBox {{ spacing:7px; }}
QCheckBox::indicator {{ width:14px; height:14px; border:1px solid #555; border-radius:3px; background:{FIELD}; }}
QCheckBox::indicator:checked {{ background:{ACCENT}; border-color:{ACCENT}; }}
QCheckBox::indicator:disabled {{ border-color:#333; background:#181818; }}
QStatusBar {{ background:{BG}; color:{MUTED}; }}
QStatusBar::item {{ border:none; }}
QPlainTextEdit {{ background:#0c0c0c; color:#cccccc; border:1px solid {BORDER}; border-radius:6px; selection-background-color:#264f78; }}
QProgressBar {{ background:#222; border:none; border-radius:3px; max-height:6px; }}
QProgressBar::chunk {{ background:{ACCENT}; border-radius:3px; }}
QScrollBar:vertical {{ background:transparent; width:10px; margin:0; }}
QScrollBar::handle:vertical {{ background:#3a3a3a; border-radius:5px; min-height:30px; }}
QScrollBar:horizontal {{ background:transparent; height:10px; margin:0; }}
QScrollBar::handle:horizontal {{ background:#3a3a3a; border-radius:5px; min-width:30px; }}
QScrollBar::add-line, QScrollBar::sub-line {{ width:0; height:0; }}
QToolTip {{ background:#222; color:{TEXT}; border:1px solid #444; padding:4px; }}
"""
