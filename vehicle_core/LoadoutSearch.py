import csv
import json
import os
import re
import shutil
import string
import subprocess
import tempfile
from pathlib import Path

IGNORE_KEYWORDS = {
    "none",
    "null",
    "sensor",
    "container",
    "false",
    "true",
    "aircraft",
    "dummy",
    "dummy_weapon",
    "flare",
    "chaff",
    "countermeasure",
    "fuel_tank",
    "pylon",
    "adapter",
    "dm_parts",
    "metaparts",
    "commonexplosion",
    "overheat",
    "1000l",
    "2000l",
    "1500l",
    "800l",
}


def auto_find_wt_path() -> Path | None:
    """Scans all system drive letters for standard Steam War Thunder installation paths."""
    subpaths = [
        Path("SteamLibrary/steamapps/common/War Thunder"),
        Path("Program Files (x86)/Steam/steamapps/common/War Thunder"),
        Path("Program Files/Steam/steamapps/common/War Thunder"),
    ]

    drives = [
        f"{d}:\\" for d in string.ascii_uppercase if os.path.exists(f"{d}:\\")
    ]

    for drive in drives:
        for subpath in subpaths:
            candidate = Path(drive) / subpath
            if candidate.exists() and (candidate / "aces.vromfs.bin").exists():
                return candidate
    return None


def auto_find_wt_ext() -> Path | None:
    """Locates wt_ext_cli.exe in the same directory as this script."""
    script_dir_exe = Path(__file__).parent / "wt_ext_cli.exe"
    if script_dir_exe.is_file():
        return script_dir_exe

    cwd_exe = Path.cwd() / "wt_ext_cli.exe"
    if cwd_exe.is_file():
        return cwd_exe

    return None


def run_wt_ext(
    wt_ext_path: Path,
    vromf_file: Path,
    output_dir: Path,
    folder_subpath: str = None,
    fmt: str = "BlkText",
):
    if not vromf_file.exists():
        return False

    cmd = [
        str(wt_ext_path),
        "unpack_vromf",
        "-i",
        str(vromf_file),
        "-o",
        str(output_dir),
        "--format",
        fmt,
        "--continue",
        "Quiet",
    ]

    if folder_subpath:
        cmd.extend(["--folder", folder_subpath])

    try:
        subprocess.run(
            cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True
        )
        return True
    except Exception:
        return False


def build_file_index(temp_dir: Path) -> dict[str, Path]:
    file_map = {}
    for f in temp_dir.rglob("*"):
        if f.is_file() and f.suffix.lower() in [".blk", ".blkx"]:
            stem = f.stem.lower()
            name = f.name.lower()

            if stem not in file_map or f.suffix.lower() == ".blkx":
                file_map[stem] = f
            file_map[name] = f

            clean_rel = str(f).replace("\\", "/").lower()
            if "gamedata/" in clean_rel:
                rel_part = clean_rel.split("gamedata/", 1)[1]
                file_map[rel_part] = f
                file_map[str(Path(rel_part).with_suffix(""))] = f

    return file_map


def resolve_file_from_index(
    ref_str: str, file_index: dict[str, Path]
) -> Path | None:
    clean = ref_str.replace("\\", "/").strip().strip("\"'").lower()
    if clean.startswith("gamedata/"):
        clean = clean[9:]

    stem = Path(clean).stem.lower()
    no_ext = str(Path(clean).with_suffix(""))

    return (
        file_index.get(clean)
        or file_index.get(no_ext)
        or file_index.get(Path(clean).name)
        or file_index.get(stem)
    )


def load_all_localizations(lang_dir: Path) -> dict[str, str]:
    names = {}
    if not lang_dir.exists():
        return names

    for csv_file in lang_dir.rglob("*.csv"):
        for encoding in ["utf-8-sig", "utf-8", "latin-1"]:
            try:
                with open(
                    csv_file, mode="r", encoding=encoding, errors="ignore"
                ) as f:
                    reader = csv.reader(f, delimiter=";")
                    for row in reader:
                        if not row or len(row) < 2:
                            continue
                        raw_key = row[0].strip('"').strip()
                        english_name = row[1].strip('"').strip()

                        if not english_name or english_name.startswith("<"):
                            continue

                        clean_key = raw_key.lower()
                        names[clean_key] = english_name

                        if clean_key.startswith("weapons/"):
                            base_k = clean_key.replace("weapons/", "")
                            names[base_k] = english_name
                            if base_k.endswith("/short"):
                                names[base_k.replace("/short", "")] = (
                                    english_name
                                )
                        if clean_key.endswith("/short"):
                            names[clean_key.replace("/short", "")] = english_name
                        if clean_key.endswith("_shop"):
                            names[clean_key[:-5]] = english_name
                break
            except Exception:
                continue

    return names


def resolve_display_name(w_id: str, names_map: dict) -> str:
    clean_id = w_id.lower().strip()
    base_id = re.sub(
        r"(_default|_launcher|_container|_rafale)$", "", clean_id
    )

    candidates = [
        clean_id,
        base_id,
        f"weapons/{clean_id}",
        f"weapons/{base_id}",
        f"weapons/{clean_id}/short",
        f"weapons/{base_id}/short",
        f"weapons/{clean_id}_short",
        f"weapons/{base_id}_short",
        f"weapons/type_{clean_id}",
        f"weapons/type_{base_id}",
    ]

    no_prefix = re.sub(r"^(fr|us|uk|ge|ru|jp|cn|it|se|il)_", "", base_id)
    candidates.extend(
        [
            no_prefix,
            f"weapons/{no_prefix}",
            f"weapons/{no_prefix}/short",
            f"weapons/type_{no_prefix}",
        ]
    )

    for cand in candidates:
        if cand in names_map:
            return names_map[cand]

    return "N/A"


def extract_leaf_weapons(
    file_path: Path,
    file_index: dict[str, Path],
    vehicle_id: str,
    visited=None,
) -> set[str]:
    if visited is None:
        visited = set()

    leaf_weapons = set()
    if not file_path.exists() or file_path in visited:
        return leaf_weapons

    visited.add(file_path)

    try:
        with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
            content = f.read()

        patterns = [
            r'(?:blk|weapon|bullet|containerUser|weaponSlot|preset)\s*[:=]\s*(?:t=)?["\']?([^"\';\s\}]+)["\']?',
            r'["\']([^"\']+\.(?:blk|blkx))["\']',
        ]

        matches = set()
        for pat in patterns:
            for match in re.findall(pat, content, re.IGNORECASE):
                matches.add(match)

        valid_child_found = False

        for raw_ref in matches:
            clean_ref = raw_ref.replace("\\", "/").strip().lower()
            stem = Path(clean_ref).stem.lower()

            if (
                not stem
                or stem == vehicle_id
                or any(k in stem for k in IGNORE_KEYWORDS)
                or stem.startswith("drop_tank")
                or stem.startswith("fuel_tank")
            ):
                continue

            target_file = resolve_file_from_index(clean_ref, file_index)

            if target_file and target_file != file_path:
                if target_file.stem.lower() == vehicle_id:
                    continue

                children = extract_leaf_weapons(
                    target_file, file_index, vehicle_id, visited
                )
                if children:
                    leaf_weapons.update(children)
                    valid_child_found = True
                else:
                    t_stem = target_file.stem.lower()
                    if (
                        t_stem != vehicle_id
                        and not any(k in t_stem for k in IGNORE_KEYWORDS)
                        and not t_stem.startswith("drop_tank")
                    ):
                        leaf_weapons.add(t_stem)
                        valid_child_found = True

        if not valid_child_found:
            self_stem = file_path.stem.lower()
            if (
                self_stem != vehicle_id
                and not self_stem.startswith(f"{vehicle_id}_")
                and not any(k in self_stem for k in IGNORE_KEYWORDS)
                and not self_stem.startswith("drop_tank")
            ):
                leaf_weapons.add(self_stem)

    except Exception:
        pass

    return leaf_weapons


def get_vehicle_weapons(
    vehicle_id: str, names_map: dict, file_index: dict
) -> list[tuple[str, str]]:
    clean_id = vehicle_id.strip().lower()
    id_underscore = clean_id.replace("-", "_")
    id_hyphen = clean_id.replace("_", "-")

    target_files = set()

    for key, fpath in file_index.items():
        f_stem = fpath.stem.lower()
        if (
            f_stem == id_underscore
            or f_stem == id_hyphen
            or f_stem.startswith(f"{id_underscore}_")
            or f_stem.startswith(f"{id_hyphen}_")
        ):
            target_files.add(fpath)

    extracted_leaf_ids = set()
    for f in target_files:
        leaf_ids = extract_leaf_weapons(f, file_index, clean_id)
        extracted_leaf_ids.update(leaf_ids)

    results = []
    seen_ids = set()

    for w_id in sorted(list(extracted_leaf_ids)):
        # Normalize duplicate '_default' suffixes to their base weapon ID
        effective_id = w_id[:-8] if w_id.endswith("_default") else w_id

        if (
            effective_id in seen_ids
            or effective_id == clean_id
            or effective_id.startswith(f"{clean_id}_")
        ):
            continue

        display_name = resolve_display_name(effective_id, names_map)

        # Skip non-weapons without localization (radars, RWRs, internal modules)
        if display_name == "N/A":
            continue

        disp_lower = display_name.lower()
        if "drop tank" in disp_lower or "fuel tank" in disp_lower:
            continue

        seen_ids.add(effective_id)
        results.append((display_name, effective_id))

    return results