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
import pickle
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from vehicle_core.Icons import IconStore, avif_supported, unpack_icons
from vehicle_core.Loadout import LoadoutData, load_loadout
from vehicle_core.LoadoutSearch import (
    build_file_index,
    get_vehicle_weapons,
    load_all_localizations,
    resolve_display_name,
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

# --- which BLKs are real, playable vehicles (found with dump_vehicle_index.py on the full 3692-entry list) -----------------
# * A vehicle's BLK lies directly in one of these three folders. flightmodels/fm|dm|performance|exhausteffects|fueldumping
#   and units/ships/debris hold flight-model / damage-model / effect data (122 entries) and are never vehicles.
# * Its id is listed in shop.blk (the tech tree plus premium / pack / event vehicles). Everything else in those folders
#   (256 entries after the folder rule) is not playable: tutorial and *_missile_test variants, AI-only SAM launchers,
#   trucks, guns, trains and wagons, aircraft carriers, cargo / fishing boats, *_ec ship variants, strike drones ...
# On the real data this keeps exactly the 3271 vehicles that are in shop.blk, unittags.blk and wpcost.blk, none with nation N/A.
# SHOP_ONLY = False lists the unlisted entries again. LIST_VERSION is stored in catalog_cache.pkl: raising it rebuilds the
# vehicle index once (the unpacked game data is kept).
LIST_VERSION = 2
SHOP_ONLY = True
_VEHICLE_FOLDERS = ("/gamedata/flightmodels", "/gamedata/units/tankmodels", "/gamedata/units/ships")


def load_shop_ids(work_dir: Path) -> set[str]:
    """Every word (lower case) that appears in a file named shop.* of the unpacked data. Vehicle ids are among them."""
    ids: set[str] = set()
    for p in work_dir.rglob("*"):
        if p.is_file() and p.stem.lower() == "shop":
            try:
                ids.update(t.lower() for t in re.findall(r"[A-Za-z0-9_\-\.]+", p.read_text(encoding="utf-8", errors="ignore")))
            except OSError:
                pass
    return ids

# --- what the unpacked-data cache keeps -------------------------------------------------------------------------
# wt_ext_cli unpacks every file of gamedata/ and config/. The app only ever reads the BLK text files there (vehicles,
# weapons, presets, nations) plus the few files whose NAME marks them as nation / tech-tree data (any extension).
# Everything else is deleted right after the unpack, so the cache holds what tanks and jets need and nothing more.
# CACHE_LAYOUT is part of the cache signature: raising it makes older, unpruned caches rebuild once.
CACHE_LAYOUT = 2
KEEP_SUFFIXES = (".blk", ".blkx")
KEEP_STEMS = ("shop", "techtree", "char", "units")        # read by VehicleLook.load_nations_from_shop (any suffix)
SKIP_DIRS: tuple = ()                                    # folders to drop entirely, e.g. ("/ships/",); empty = every vehicle type is kept


import sys  # Ensure sys is imported at the top of wtx/vehicles.py


def find_wt_ext(root: Path) -> Path | None:
  # Check if running as a compiled PyInstaller EXE
  if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
    meipass_exe = Path(sys._MEIPASS) / "wt_ext_cli.exe"
    if meipass_exe.is_file():
      return meipass_exe

  for cand in (
      root / "wt_ext_cli.exe",
      Path.cwd() / "wt_ext_cli.exe",
      Path(__file__).parent / "wt_ext_cli.exe",
  ):
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
        self._loadouts: dict[str, LoadoutData | None] = {}
        self.icons: IconStore | None = None      # weapon icons (gameuiskin); None until extract() ran

    # ------------------------------------------------------------------ extraction
    def _signature(self) -> str:
        parts = []
        for name in ("aces.vromfs.bin", "char.vromfs.bin", "lang.vromfs.bin", "ui/atlases.vromfs.bin"):
            p = self.wt_dir / name
            if p.exists():
                st = p.stat()
                parts.append(f"{name}:{st.st_size}:{int(st.st_mtime)}")
        lang_dir = self.wt_dir / "lang"
        if lang_dir.exists():
            parts.append(f"lang_dir:{sum(1 for _ in lang_dir.glob('*.csv'))}")
        parts.append(f"layout:{CACHE_LAYOUT}:{'/'.join(SKIP_DIRS)}")
        return hashlib.sha1("|".join(parts).encode()).hexdigest()[:12]

    def extract(self, status: Status, use_cache: bool = True) -> None:
        """Unpack gamedata / config / lang."""
        self.cache_root.mkdir(parents=True, exist_ok=True)
        self.work_dir = self.cache_root / f"gamedata_{self._signature()}"
        marker = self.work_dir / ".complete"

        if use_cache and marker.exists():
            status(f"Using cached game data ({self.work_dir.name})")
            self._prepare_icons(status)
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

        self._prune_unpacked(status)
        marker.write_text("ok")
        self._prepare_icons(status)

    def _prune_unpacked(self, status: Status) -> None:
        """Delete the unpacked files the app never reads (see KEEP_SUFFIXES / KEEP_STEMS / SKIP_DIRS)."""
        before = after = kept = dropped = 0
        for sub in ("gamedata", "config"):
            root = self.work_dir / sub
            if not root.is_dir():
                continue
            for f in root.rglob("*"):
                if not f.is_file():
                    continue
                size = f.stat().st_size
                before += size
                rel = "/" + f.relative_to(self.work_dir).as_posix().lower() + "/"
                wanted = (f.suffix.lower() in KEEP_SUFFIXES or f.stem.lower() in KEEP_STEMS) \
                    and not any(d in rel for d in SKIP_DIRS)
                if wanted:
                    kept += 1
                    after += size
                else:
                    dropped += 1
                    try:
                        f.unlink()
                    except OSError:
                        after += size
            for d in sorted((x for x in root.rglob("*") if x.is_dir()), key=lambda x: len(x.parts), reverse=True):
                try:
                    d.rmdir()                      # only succeeds on empty folders
                except OSError:
                    pass
        status(f"Game-data cache trimmed: kept {kept} files ({after / 1048576:.1f} MB), "
               f"dropped {dropped} ({(before - after) / 1048576:.1f} MB)")

    def _prepare_icons(self, status: Status) -> None:
        """Weapon icons are optional: a failure here only means the loadout panel shows names instead of pictures."""
        try:
            self.icons = IconStore.for_work_dir(self.work_dir)
            if not self.icons.available():
                unpack_icons(self.wt_dir, self.wt_ext, self.work_dir, status)
            if self.icons.available() and not avif_supported():
                status("[warning] Pillow cannot decode AVIF - weapon icons will be missing "
                       "(pip install -U pillow, or pip install pillow-avif-plugin)")
        except Exception as e:              # noqa: BLE001
            status(f"[warning] weapon icons skipped: {type(e).__name__}: {e}")

    def clear_cache(self) -> None:
        for old in self.cache_root.glob("gamedata_*"):
            shutil.rmtree(old, ignore_errors=True)

    # ------------------------------------------------------------------ scanning
    def scan(self, status: Status) -> list[Vehicle]:
        """Build the vehicle list from the unpacked data (with caching)."""
        assert self.work_dir is not None, "extract() first"

        cache_file = self.work_dir / "catalog_cache.pkl"

        # Try loading from cache if it exists
        if cache_file.exists():
            status("Loading cached localizations, nations, and vehicle indices...")
            try:
                with open(cache_file, "rb") as f:
                    cached_data = pickle.load(f)
                if cached_data.get("list_version") == LIST_VERSION and cached_data.get("shop_only") == SHOP_ONLY:
                    self.names_map = cached_data["names_map"]
                    self.nations_map = cached_data["nations_map"]
                    self.file_index = cached_data["file_index"]
                    self.vehicles = cached_data["vehicles"]
                    status(f"{len(self.vehicles)} vehicles found (loaded from cache)")
                    return self.vehicles
                status("Vehicle list rules changed - rebuilding the vehicle index...")
            except Exception as e:
                status(f"[warning] Failed to load cache, re-scanning: {e}")

        # Otherwise, perform the full scan
        status("Reading localization...")
        self.names_map = load_all_localizations(self.work_dir / "lang")
        status("Reading nations...")
        self.nations_map = load_nations_from_shop(self.work_dir)
        status("Indexing weapon files...")
        self.file_index = build_file_index(self.work_dir)

        status("Sorting vehicles by nation and type...")
        vehicles: list[Vehicle] = []
        seen: set[str] = set()
        shop_ids = load_shop_ids(self.work_dir) if SHOP_ONLY else set()
        if SHOP_ONLY and not shop_ids:
            status("[warning] no shop.blk found - the list is not limited to shop vehicles")
        hidden = {"folder": 0, "not in shop": 0}

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
            if SHOP_ONLY:
                if not fpath.parent.as_posix().lower().endswith(_VEHICLE_FOLDERS):
                    hidden["folder"] += 1              # fm / dm / effect data, ship debris
                    continue
                if shop_ids and vid not in shop_ids:
                    hidden["not in shop"] += 1         # tutorial / test / AI-only / unplayable entries
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

        # Save everything to cache for future launches
        try:
            with open(cache_file, "wb") as f:
                pickle.dump({
                    "names_map": self.names_map,
                    "nations_map": self.nations_map,
                    "file_index": self.file_index,
                    "vehicles": self.vehicles,
                    "list_version": LIST_VERSION,
                    "shop_only": SHOP_ONLY,
                }, f)
        except Exception as e:
            status(f"[warning] Failed to save cache: {e}")

        if SHOP_ONLY:
            status(f"Hidden {sum(hidden.values())} non-vehicle / unplayable entries "
                   f"({hidden['folder']} data files, {hidden['not in shop']} not in the shop)")
        status(f"{len(vehicles)} vehicles found")
        return vehicles

    def nations(self) -> list[str]:
        found = {v.nation for v in self.vehicles if v.nation and v.nation != "N/A"}
        ordered = [n for n in NATION_NAMES.values() if n in found]
        return ordered + sorted(found - set(ordered)) + (["N/A"] if any(v.nation == "N/A" for v in self.vehicles) else [])

    # ------------------------------------------------------------------ weapons
    def weapons(self, vehicle: Vehicle) -> list[tuple[str, str]]:
        return get_vehicle_weapons(vehicle.id, self.names_map, self.file_index)

    def weapon_display_name(self, stem: str) -> str | None:
        """Localized name of a weapon BLK stem, or None when the game has no text for it."""
        name = resolve_display_name(stem, self.names_map)
        return None if name == "N/A" else name

    def loadout(self, vehicle: Vehicle) -> LoadoutData | None:
        """Weapon slots, their choices and the pylon / pod nodes each choice shows (read from the vehicle's BLK).

        Cached per vehicle. None when the vehicle's BLK has no slot that changes the model."""
        if vehicle.id in self._loadouts:
            return self._loadouts[vehicle.id]

        name_for = self.weapon_display_name

        try:
            data = load_loadout(
                vehicle.path, vehicle.id,
                lambda ref: resolve_file_from_index(ref, self.file_index),
                name_for,
            )
        except Exception:
            data = None
        self._loadouts[vehicle.id] = data
        return data

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