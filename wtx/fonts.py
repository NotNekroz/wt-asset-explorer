"""fonts.py - War Thunder's own symbol font, so vehicle names show their in-game icons.

War Thunder's localisation puts icon characters in front of vehicle names (premium / squadron / event
markers and so on, e.g. U+2417 or the block elements U+2580..).  Ordinary fonts don't have them, so Qt
draws empty boxes.  The game renders them with ``symbols_skyquake.ttf`` (``baseFontData`` in its
``fonts.blk``).  The font is not shipped with this program: it is unpacked from the player's own
``ui/fonts.vromfs.bin`` into the cache folder and used as the per-glyph fallback of the whole app.

  * ensure_game_font()   - no Qt, safe on a worker thread: unpack + cache the .ttf
  * register_symbol_font() - GUI thread: load it into Qt and add it as fallback to the app font
"""
from __future__ import annotations

import shutil
import tempfile
from pathlib import Path
from typing import Callable

from PySide6.QtGui import QFont, QFontDatabase
from PySide6.QtWidgets import QApplication

from vehicle_core.VehicleLook import run_wt_ext

SYMBOL_FONT = "symbols_skyquake.ttf"
FONTS_VROMFS = Path("ui") / "fonts.vromfs.bin"
CACHE_DIR = "fonts"

symbol_family: str | None = None


def _stamp(vromfs: Path) -> str:
  st = vromfs.stat()
  return f"{st.st_size}:{int(st.st_mtime)}"


def ensure_game_font(wt_dir: Path, wt_ext: Path, cache_root: Path,
                     status: Callable[[str], None] = print) -> Path | None:
  """Path of the cached symbol font, unpacking it from the game when missing or out of date.

  Returns None on any failure - vehicle names then just show boxes for the icon characters.
  """
  try:
    vromfs = Path(wt_dir) / FONTS_VROMFS
    if not vromfs.is_file():
      status(f"[warning] {FONTS_VROMFS} not found - vehicle-name icons unavailable")
      return None
    dest_dir = Path(cache_root) / CACHE_DIR
    dest = dest_dir / SYMBOL_FONT
    stamp_file = dest_dir / "fonts.stamp"
    stamp = _stamp(vromfs)
    if dest.is_file() and stamp_file.is_file() and stamp_file.read_text().strip() == stamp:
      return dest

    status("Unpacking War Thunder fonts (vehicle-name icons)...")
    dest_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=dest_dir, prefix="unpack_") as tmp:
      tmp_path = Path(tmp)
      # only the ttfs folder is needed; fall back to the whole archive (19 MB) if --folder is refused
      if not run_wt_ext(Path(wt_ext), vromfs, tmp_path, folder_subpath="ttfs", fmt="Raw"):
        run_wt_ext(Path(wt_ext), vromfs, tmp_path, fmt="Raw")
      found = next((p for p in tmp_path.rglob("*") if p.is_file() and p.name.lower() == SYMBOL_FONT), None)
      if found is None:
        status(f"[warning] {SYMBOL_FONT} not found in {vromfs.name}")
        return None
      shutil.copyfile(found, dest)
    stamp_file.write_text(stamp)
    return dest
  except Exception as e:  # noqa: BLE001 - optional nicety, never block loading
    status(f"[warning] game fonts skipped: {type(e).__name__}: {e}")
    return None


def _with_fallback(font: QFont, family: str) -> QFont:
  f = QFont(font)
  chain = [x for x in f.families() if x] or [f.family()]
  if family not in chain:
    chain.append(family)
  f.setFamilies(chain)
  return f


def register_symbol_font(app: QApplication, path: Path | None) -> str | None:
  """Load the .ttf into Qt (GUI thread) and make it the glyph fallback of the application font."""
  global symbol_family
  if path is None or not Path(path).is_file():
    return None
  if symbol_family is None:
    font_id = QFontDatabase.addApplicationFont(str(path))
    families = QFontDatabase.applicationFontFamilies(font_id) if font_id >= 0 else []
    if not families:
      return None
    symbol_family = families[0]
  app.setFont(_with_fallback(app.font(), symbol_family))
  return symbol_family


def symbols_first(font: QFont) -> QFont:
  """Copy of ``font`` that tries the War Thunder symbol font BEFORE the normal font.

  Needed for text that carries icon characters (vehicle names): many characters the game uses
  (block elements like U+2584, geometric shapes like U+25CA) also exist in the normal UI font, which
  would then draw a plain white block / diamond instead of the game's icon.  The symbol font has no
  letters or digits, so ordinary text still comes from the normal font.
  """
  if not symbol_family:
    return QFont(font)
  f = QFont(font)
  chain = [x for x in f.families() if x] or [f.family()]
  f.setFamilies([symbol_family] + [x for x in chain if x != symbol_family])
  return f


def name_symbol_report(vehicles) -> list[str]:
  """One line per non-ASCII character used in vehicle names: code point, how many vehicles, examples."""
  found: dict[str, list[str]] = {}
  for v in vehicles:
    for ch in set(str(v.name or "")):
      if ord(ch) > 0x7F and not ch.isalpha():
        found.setdefault(ch, []).append(str(v.name))
  lines = []
  for ch in sorted(found, key=ord):
    ex = ", ".join(n.replace(ch, "[]") for n in found[ch][:3])
    lines.append(f"U+{ord(ch):04X}  x{len(found[ch])}  e.g. {ex}")
  return lines
