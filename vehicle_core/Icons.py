"""Icons.py - the game's weapon icons (gameuiskin/<iconType>.avif) as PNG files the Qt panel can load.

The images live in ui/atlases.vromfs.bin under gameuiskin/, not in gui/aces/char. VehicleCatalog.extract() unpacks that
folder once with wt_ext_cli (--format Raw) next to the other game data; this module converts single icons to PNG on demand
(Qt has no AVIF reader) and remembers the result, so only the icons a vehicle actually shows are ever decoded.
"""
from __future__ import annotations

from pathlib import Path
from typing import Callable, Optional

Status = Callable[[str], None]

ATLAS_FILE = Path("ui") / "atlases.vromfs.bin"      # relative to the War Thunder folder
ICON_FOLDER = "gameuiskin"


def avif_supported() -> bool:
    """True when Pillow can decode AVIF (Pillow 11.3+ ships it, older ones need pillow-avif-plugin)."""
    try:
        from PIL import features
        if features.check("avif"):
            return True
    except Exception:
        return False
    try:                                              # the optional plugin registers itself on import
        import pillow_avif  # noqa: F401
        return True
    except Exception:
        return False


def _find_icon_dir(dest: Path) -> Optional[Path]:
    """The folder named gameuiskin (any depth) under dest that holds the most .avif files, or None."""
    import os
    best, best_n = None, 0
    for root, dirs, files in os.walk(dest):
        if Path(root) == Path(dest):                  # the game data / config / lang trees are huge and never hold icons
            dirs[:] = [d for d in dirs if d.lower() not in ("gamedata", "config", "lang", "icons_png")]
        if Path(root).name.lower() == ICON_FOLDER:
            n = sum(1 for f in files if f.lower().endswith(".avif"))
            if n > best_n:
                best, best_n = Path(root), n
    return best


def unpack_icons(wt_dir: Path, wt_ext: Path, dest: Path, status: Optional[Status] = None) -> bool:
    """Unpack gameuiskin into dest/gameuiskin. Tries ui/atlases.vromfs.bin first, then the other ui/*.vromfs.bin files.
    wt_ext_cli exits with success when the requested folder is not in the archive, so success is judged by whether
    .avif files really appeared (wherever the tool put them; the folder is moved to dest/gameuiskin). Never raises."""
    say = status or (lambda _m: None)
    try:
        import shutil
        from vehicle_core.VehicleLook import run_wt_ext
        atlas = Path(wt_dir) / ATLAS_FILE
        dest = Path(dest)
        candidates = [atlas] if atlas.exists() else []
        candidates += sorted(p for p in (Path(wt_dir) / "ui").glob("*.vromfs.bin") if p != atlas)
        if not candidates:
            say("[warning] no vromfs in the game's ui folder - weapon icons unavailable")
            return False
        for vromf in candidates:
            say(f"Unpacking ui/{vromf.name} (weapon icons)...")
            run_wt_ext(Path(wt_ext), vromf, dest, folder_subpath=ICON_FOLDER, fmt="Raw")
            found = _find_icon_dir(dest)
            if found is None:
                say(f"[warning] ui/{vromf.name} has no gameuiskin icons")
                continue
            target = dest / ICON_FOLDER
            if found != target:
                say(f"Weapon icons were in {found.relative_to(dest)}; moving to {ICON_FOLDER}")
                if target.exists():
                    shutil.rmtree(target, ignore_errors=True)
                shutil.move(str(found), str(target))
            return True
        say("[warning] could not unpack the weapon icons")
        return False
    except Exception as e:                            # noqa: BLE001
        say(f"[warning] weapon icons skipped: {type(e).__name__}: {e}")
        return False


class IconStore:
    """name (iconType) -> PNG path, converted lazily into png_dir."""

    def __init__(self, raw_dir: Path, png_dir: Path):
        self.raw_dir = Path(raw_dir)                  # folder that contains the .avif files
        self.png_dir = Path(png_dir)
        self._done: dict[str, Optional[Path]] = {}
        self.error: str = ""                          # why icons are missing, for one status line

    @classmethod
    def for_work_dir(cls, work_dir: Path) -> "IconStore":
        return cls(Path(work_dir) / ICON_FOLDER, Path(work_dir) / "icons_png")

    def available(self) -> bool:
        return self.raw_dir.is_dir()

    def path(self, name: str) -> Optional[Path]:
        """PNG for an icon name, or None when it is missing or cannot be decoded. Never raises."""
        if not name:
            return None
        if name in self._done:
            return self._done[name]
        out: Optional[Path] = None
        try:
            png = self.png_dir / f"{name}.png"
            if png.exists() and png.stat().st_size > 0:
                out = png
            else:
                src = self.raw_dir / f"{name}.avif"
                if src.exists():
                    if not avif_supported():
                        self.error = "Pillow has no AVIF support (pip install -U pillow, or pillow-avif-plugin)"
                    else:
                        from PIL import Image
                        self.png_dir.mkdir(parents=True, exist_ok=True)
                        with Image.open(src) as im:
                            im.convert("RGBA").save(png, "PNG")
                        out = png
        except Exception as e:                        # noqa: BLE001
            self.error = f"{type(e).__name__}: {e}"
            out = None
        self._done[name] = out
        return out

    def why_missing(self, name: str) -> str:
        """One-line reason why path(name) gave nothing (for the log); '' when the icon is fine."""
        if not name:
            return "a preset has no icon name"
        if self.path(name) is not None:
            return ""
        if not self.raw_dir.is_dir():
            return f"folder not unpacked: {self.raw_dir}"
        if not (self.raw_dir / f"{name}.avif").exists():
            return f"no {name}.avif in {self.raw_dir}"
        if not avif_supported():
            return "Pillow has no AVIF support (pip install -U pillow, or pillow-avif-plugin)"
        return self.error or "decoding failed"
