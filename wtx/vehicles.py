"""Vehicle catalog.

This is the logic of NotNekroz/War-Thunder-Vehicle-Explorer (main.py / VehicleLook.py /
LoadoutSearch.py) with the GUI removed, so it can be driven by the combined window:

  * unpack the BLK/CSV data out of the game's vromfs with wt_ext_cli.exe
  * work out display names, nations, vehicle types and model ids
  * resolve each vehicle's weapons

The unpacked data is kept in a cache folder keyed by the vromfs file sizes/mtimes, so only the
first launch (or the first launch after a game update) has to run wt_ext_cli.
"""
from __future__ import annotations

import hashlib
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from vehicle_core.LoadoutSearch import (
    build_file_index,
    get_vehicle_weapons,
    load_all_localizations,
    resolve_file_from_index,
)
from vehicle_core.VehicleLook import (
    NATION_NAMES,
    extract_file_details,
    load_nations_from_shop,
    run_wt_ext,
)

Status = Callable[[str], None]

VEHICLE_TYPES = ["Planes", "Tanks", "Helicopters", "Ships"]

IGNORE_KEYWORDS = {"metaparts", "dummy", "nt", "event", "dm_parts", "killstreak"}


def is_ignored(vid: str) -> bool:
    """Checks for explicit dummy and meta part entries."""
    if not vid:
        return True
    lower = vid.lower()
    if re.search(r"(?:^|[_.-])nt(?:[_.-]|$)", lower):
        return True
    for kw in IGNORE_KEYWORDS:
        if kw != "nt" and kw in lower:
            return True
    return False


def is_weapon_or_preset_blk(fpath: Path) -> bool:
    """Uses LoadoutSearch file path and BLK structure logic to identify non-vehicle weapon files."""
    rel = fpath.as_posix().lower()

    # 1. Path-based check for weapon preset folders
    if any(k in rel for k in ("weaponry", "weapon_presets", "weapons", "presets", "payloads", "/fx/", "/templates/", "/config/")):
        return True

    # 2. Structural content inspection
    try:
        with open(fpath, "r", encoding="utf-8", errors="ignore") as f:
            content = f.read(4096).lower()
            # Preset files contain root-level weapon declarations
            if "weapon{" in content or "weapon {" in content or "weapon_preset" in content:
                # Ensure it's not a full vehicle BLK that merely defines weapon slots
                if not any(k in content for k in ("flightmodel", 'type:t="tank"', 'type:t="ship"', 'type:t="helicopter"', "ishelicopter:b=yes")):
                    return True
    except Exception:
        pass

    return False


@dataclass
class Vehicle:
    id: str
    name: str
    model_id: str
    nation: str
    type: str
    path: Path


def classify_vehicle_type(file_path: Path) -> str:
    """Classifies a vehicle file into Planes, Tanks, Helicopters, or Ships."""
    path_str = str(file_path).replace("\\", "/").lower()
    file_stem = file_path.stem.lower()

    if "/tankmodels/" in path_str or "/tanks/" in path_str or "tank_" in file_stem:
        return "Tanks"
    if "/ships/" in path_str or "/boats/" in path_str or "/navy/" in path_str:
        return "Ships"
    if "/helicopters/" in path_str or file_stem.startswith(
        (
            "ah_", "mi_", "ka_", "uh_", "bo_", "tiger_", "z_", "ab_", "a_129", "eurocopter",
            "hkp", "lynx", "g_lynx", "sa_341", "sa_342", "wasp", "scout", "rooivalk",
        )
    ):
        return "Helicopters"

    try:
        with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
            content = f.read(2048).lower()
            if 'type:t="helicopter"' in content or "ishelicopter:b=yes" in content:
                return "Helicopters"
            if 'type:t="tank"' in content or 'vehicletype:t="tank"' in content:
                return "Tanks"
            if 'type:t="ship"' in content or 'type:t="boat"' in content:
                return "Ships"
    except Exception:
        pass

    return "Planes"


# Keys such as model:t="..." / bombModel:t="..." inside a weapon BLK
_MODEL_KEY_RE = re.compile(r'^\s*"?([A-Za-z0-9_]*model[A-Za-z0-9_]*)"?\s*:\s*t\s*=\s*"([^"]+)"', re.I | re.M)

_VEHICLE_DIRS = ("flightmodels/", "tankmodels/", "tanks/", "helicopters/", "ships/")


def find_wt_ext(root: Path) -> Path | None:
    for cand in (root / "wt_ext_cli.exe", Path.cwd() / "wt_ext_cli.exe"):
        if cand.is_file():
            return cand
    return None


def looks_like_wt_dir(p: Path) -> bool:
    return p.is_dir() and (p / "aces.vromfs.bin").exists()


class VehicleCatalog:
    def __init__(self, wt_dir: Path, wt_ext: Path, cache_root: Path):
        self.wt_dir = Path(wt_dir)
        self.wt_ext = Path(wt_ext)
        self.cache_root = Path(cache_root)
        self.work_dir: Path | None = None
        self.vehicles: list[Vehicle] = []
        self.names_map: dict[str, str] = {}
        self.nations_map: dict[str, str] = {}
        self.file_index: dict[str, Path] = {}

    # ------------------------------------------------------------------ extraction
    def _signature(self) -> str:
        parts = []
        for name in ("aces.vromfs.bin", "char.vromfs.bin", "lang.vromfs.bin"):
            p = self.wt_dir / name
            if p.exists():
                st = p.stat()
                parts.append(f"{name}:{st.st_size}:{int(st.st_mtime)}")
        lang_dir = self.wt_dir / "lang"
        if lang_dir.exists():
            parts.append(f"lang_dir:{sum(1 for _ in lang_dir.glob('*.csv'))}")
        return hashlib.sha1("|".join(parts).encode()).hexdigest()[:12]

    def extract(self, status: Status, use_cache: bool = True) -> None:
        """Unpack gamedata / config / lang."""
        self.cache_root.mkdir(parents=True, exist_ok=True)
        self.work_dir = self.cache_root / f"gamedata_{self._signature()}"
        marker = self.work_dir / ".complete"

        if use_cache and marker.exists():
            status(f"Using cached game data ({self.work_dir.name})")
            return

        for old in self.cache_root.glob("gamedata_*"):
            shutil.rmtree(old, ignore_errors=True)
        self.work_dir.mkdir(parents=True)

        status("Unpacking aces.vromfs.bin (gamedata)...")
        if not run_wt_ext(self.wt_ext, self.wt_dir / "aces.vromfs.bin", self.work_dir,
                          folder_subpath="gamedata", fmt="BlkText"):
            shutil.rmtree(self.work_dir, ignore_errors=True)
            raise RuntimeError(
                "wt_ext_cli.exe failed to unpack aces.vromfs.bin."
            )

        status("Unpacking char.vromfs.bin (config)...")
        if not run_wt_ext(self.wt_ext, self.wt_dir / "char.vromfs.bin", self.work_dir,
                          folder_subpath="config", fmt="BlkText"):
            status("[warning] could not unpack char.vromfs.bin")

        status("Preparing localization...")
        lang_dest = self.work_dir / "lang"
        if (self.wt_dir / "lang").exists():
            shutil.copytree(self.wt_dir / "lang", lang_dest, dirs_exist_ok=True)
        else:
            run_wt_ext(self.wt_ext, self.wt_dir / "lang.vromfs.bin", lang_dest, fmt="Raw")

        marker.write_text("ok")

    def clear_cache(self) -> None:
        for old in self.cache_root.glob("gamedata_*"):
            shutil.rmtree(old, ignore_errors=True)

    # ------------------------------------------------------------------ scanning
    def scan(self, status: Status) -> list[Vehicle]:
        """Build the vehicle list from the unpacked data."""
        assert self.work_dir is not None, "extract() first"

        status("Reading localization...")
        self.names_map = load_all_localizations(self.work_dir / "lang")
        status("Reading nations...")
        self.nations_map = load_nations_from_shop(self.work_dir)
        status("Indexing weapon files...")
        self.file_index = build_file_index(self.work_dir)

        status("Sorting vehicles by nation and type...")
        vehicles: list[Vehicle] = []
        seen: set[str] = set()

        for fpath in self.work_dir.rglob("*"):
            if not fpath.is_file() or fpath.suffix.lower() not in (".blk", ".blkx"):
                continue

            rel = str(fpath).replace("\\", "/").lower()
            if not any(x in rel for x in _VEHICLE_DIRS):
                continue

            if is_weapon_or_preset_blk(fpath):
                continue

            vid = fpath.stem.lower()
            if vid in seen or vid.endswith("_default") or is_ignored(vid):
                continue
            seen.add(vid)

            name = (
                self.names_map.get(vid)
                or self.names_map.get(vid.replace("_", "-"))
                or self.names_map.get(f"{vid}_shop")
                or vid
            )
            model_id, file_nation = extract_file_details(fpath)
            nation = (
                self.nations_map.get(vid)
                or self.nations_map.get(vid.replace("_", "-"))
                or file_nation
            )
            vehicles.append(Vehicle(vid, name, model_id, nation, classify_vehicle_type(fpath), fpath))

        vehicles.sort(key=lambda v: v.name)
        self.vehicles = vehicles
        status(f"{len(vehicles)} vehicles found")
        return vehicles

    def nations(self) -> list[str]:
        found = {v.nation for v in self.vehicles if v.nation and v.nation != "N/A"}
        ordered = [n for n in NATION_NAMES.values() if n in found]
        return ordered + sorted(found - set(ordered)) + (["N/A"] if any(v.nation == "N/A" for v in self.vehicles) else [])

    # ------------------------------------------------------------------ weapons
    def weapons(self, vehicle: Vehicle) -> list[tuple[str, str]]:
        return get_vehicle_weapons(vehicle.id, self.names_map, self.file_index)

    def weapon_model_hints(self, weapon_id: str) -> list[str]:
        """Model names mentioned inside the weapon's BLK."""
        blk = resolve_file_from_index(weapon_id, self.file_index)
        hints: list[str] = []
        if blk and blk.exists():
            try:
                text = blk.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                text = ""
            for _key, value in _MODEL_KEY_RE.findall(text):
                stem = value.replace("\\", "/").rsplit("/", 1)[-1]
                stem = re.sub(r"\.(blk|blkx|dyn|ri)$", "", stem, flags=re.I)
                if stem and stem not in hints:
                    hints.append(stem)
        return hints