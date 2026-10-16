import csv
import json
import os
import re
import shutil
import string
import subprocess
import tempfile
from pathlib import Path

NATION_NAMES = {
    "country_usa": "USA",
    "country_germany": "Germany",
    "country_ussr": "USSR",
    "country_britain": "Britain",
    "country_japan": "Japan",
    "country_china": "China",
    "country_italy": "Italy",
    "country_france": "France",
    "country_sweden": "Sweden",
    "country_israel": "Israel",
}

STRUCTURAL_KEYWORDS = {
    "aircraft",
    "army",
    "helicopters",
    "ships",
    "tanks",
    "submarines",
    "range",
    "group",
    "folder",
    "ranks",
    "rank_1",
    "rank_2",
    "rank_3",
    "rank_4",
    "rank_5",
    "rank_6",
    "rank_7",
    "rank_8",
    "premiums",
    "pack",
    "reqair",
    "reqtank",
    "reqship",
    "units",
}

IGNORE_KEYWORDS = {"metaparts", "dummy", "nt"}


def is_ignored(text: str) -> bool:
    """Checks if text contains any of the ignore keywords."""
    if not text:
        return False
    lower = text.lower()
    for kw in IGNORE_KEYWORDS:
        if kw == "nt":
            if re.search(r"(?:^|[_.-])nt(?:[_.-]|$)", lower):
                return True
        else:
            if kw in lower:
                return True
    return False


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


def register_nation(nations_map: dict, vid: str, nation: str):
    if not vid or not nation or nation == "N/A":
        return
    clean_id = vid.strip('"\'').strip()
    clean_lower = clean_id.lower()

    if not clean_id or clean_lower in STRUCTURAL_KEYWORDS or is_ignored(clean_id):
        return

    nations_map[clean_id] = nation
    nations_map[clean_lower] = nation
    nations_map[clean_id.replace("-", "_")] = nation
    nations_map[clean_id.replace("_", "-")] = nation


def load_english_names(lang_dir: Path) -> dict:
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
                        key = row[0].strip('"').strip()
                        english_name = row[1].strip('"').strip()

                        if not english_name:
                            continue

                        names[key] = english_name
                        if key.endswith("_shop"):
                            names[key[:-5]] = english_name
                break
            except Exception:
                continue

    return names


def parse_blk_file_for_nations(file_path: Path) -> dict:
    nations_map = {}
    if not file_path.exists():
        return nations_map

    try:
        with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
            content = f.read()
    except Exception:
        return nations_map

    content = re.sub(r"//.*|/\*.*?\*/", "", content, flags=re.DOTALL)
    token_pattern = re.compile(
        r'([a-zA-Z0-9_\-\.]+)\s*\{|(\})|([a-zA-Z0-9_\-]+)\s*:\s*t\s*=\s*["\']([^"\']+)["\']'
    )

    stack = []
    for match in token_pattern.finditer(content):
        block_open, block_close, key, val = match.groups()

        if block_open:
            clean_name = block_open.strip("\"'")
            stack.append(clean_name)

            current_nation = "N/A"
            for item in stack:
                if item.lower() in NATION_NAMES:
                    current_nation = NATION_NAMES[item.lower()]
                    break

            if current_nation != "N/A" and not clean_name.lower().startswith(
                "country_"
            ):
                register_nation(nations_map, clean_name, current_nation)

        elif block_close and stack:
            stack.pop()

        elif key and val:
            current_nation = "N/A"
            for item in stack:
                if item.lower() in NATION_NAMES:
                    current_nation = NATION_NAMES[item.lower()]
                    break

            if current_nation != "N/A":
                register_nation(nations_map, val, current_nation)

    return nations_map


def load_nations_from_shop(temp_dir: Path) -> dict:
    nations_map = {}
    for p in temp_dir.rglob("*"):
        if p.is_file() and p.stem.lower() in [
            "shop",
            "techtree",
            "char",
            "units",
        ]:
            parsed = parse_blk_file_for_nations(p)
            nations_map.update(parsed)
    return nations_map


def extract_file_details(file_path: Path) -> tuple[str, str]:
    model_id = "N/A"
    country = "N/A"

    if not file_path or not file_path.exists():
        return model_id, country

    try:
        with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
            content = f.read()

        m_match = re.search(
            r'["\']?model["\']?\s*[:=]\s*(?:t\s*=\s*)?["\']?([a-zA-Z0-9_\-\.]+)(?:["\']|\s|;|\n|$)',
            content,
            re.IGNORECASE,
        )
        if m_match:
            model_id = m_match.group(1).strip()

        content_lower = content.lower()
        for c_key, c_name in NATION_NAMES.items():
            if c_key in content_lower:
                country = c_name
                break
    except Exception:
        pass

    if model_id == "N/A" and file_path:
        model_id = file_path.stem

    return model_id, country


def search_vehicles(fm_dir: Path, names_map: dict, nations_map: dict, query: str):
    vehicle_files = {}
    for f in list(fm_dir.glob("*.blkx")) + list(fm_dir.glob("*.blk")):
        if is_ignored(f.stem):
            continue
        if f.stem not in vehicle_files or f.suffix == ".blkx":
            vehicle_files[f.stem] = f

    vehicle_ids = sorted(list(vehicle_files.keys()))
    query_clean = query.lower().strip()
    matches = []

    for vid in vehicle_ids:
        if is_ignored(vid):
            continue

        display_name = (
            names_map.get(vid)
            or names_map.get(vid.replace("_", "-"))
            or names_map.get(f"{vid}_shop")
            or names_map.get(f"{vid.replace('_', '-')}_shop")
            or "N/A"
        )

        vid_match = (
            query_clean in vid.lower()
            or query_clean in vid.replace("_", "-").lower()
        )
        name_match = (display_name != "N/A") and (
            query_clean in display_name.lower()
        )

        if vid_match or name_match:
            file_path = vehicle_files[vid]
            model_id, file_nation = extract_file_details(file_path)

            nation = (
                nations_map.get(vid)
                or nations_map.get(vid.lower())
                or nations_map.get(vid.replace("_", "-"))
                or file_nation
            )

            matches.append((vid, display_name, model_id, nation))

    print(f"\n--- Found {len(matches)} Result(s) for '{query}' ---\n")
    for vid, name, model_id, nation in matches:
        print(
            f"ID: {vid} | Display Name: {name} | Model ID: {model_id} | Nation:"
            f" {nation}"
        )


if __name__ == "__main__":
    wt_dir = auto_find_wt_path()
    if wt_dir:
        print(f"[Auto-Detect] War Thunder found at: {wt_dir}")
    else:
        wt_dir_input = input("Enter War Thunder root folder path: ").strip('"')
        wt_dir = Path(wt_dir_input)

    wt_ext_path = auto_find_wt_ext()
    if wt_ext_path:
        print(f"[Auto-Detect] wt_ext_cli.exe found at: {wt_ext_path}")
    else:
        wt_ext_input = input("Enter path to wt_ext_cli.exe: ").strip('"')
        wt_ext_path = Path(wt_ext_input)
        if wt_ext_path.is_dir():
            wt_ext_path = wt_ext_path / "wt_ext_cli.exe"

    if not wt_dir.exists():
        print(f"[Error] War Thunder path '{wt_dir}' does not exist.")
        exit(1)

    if not wt_ext_path.is_file():
        print(f"[Error] wt_ext_cli.exe not found at '{wt_ext_path}'.")
        exit(1)

    temp_dir = Path(tempfile.mkdtemp(prefix="wt_veh_search_"))

    try:
        print("\nExtracting minimal vehicle data from VROMF files...")

        run_wt_ext(
            wt_ext_path,
            wt_dir / "aces.vromfs.bin",
            temp_dir,
            folder_subpath="gamedata/flightmodels",
            fmt="BlkText",
        )

        run_wt_ext(
            wt_ext_path,
            wt_dir / "char.vromfs.bin",
            temp_dir,
            folder_subpath="config",
            fmt="BlkText",
        )

        lang_dest = temp_dir / "lang"
        if (wt_dir / "lang").exists():
            shutil.copytree(wt_dir / "lang", lang_dest, dirs_exist_ok=True)
        else:
            run_wt_ext(
                wt_ext_path,
                wt_dir / "lang.vromfs.bin",
                lang_dest,
                fmt="Raw",
            )

        print("Processing extracted indexes...")
        names_map = load_english_names(lang_dest)
        nations_map = load_nations_from_shop(temp_dir)

        fm_dir = None
        for p in temp_dir.rglob("flightmodels"):
            if p.is_dir():
                fm_dir = p
                break

        if not fm_dir or not fm_dir.exists():
            print(
                "\n[Error] Could not locate extracted 'flightmodels' directory inside temp folder."
            )
            exit(1)

        while True:
            search_term = input(
                "\nEnter vehicle name or ID (or 'q' to quit): "
            ).strip()
            if not search_term or search_term.lower() == "q":
                break
            search_vehicles(fm_dir, names_map, nations_map, search_term)

    finally:
        print("\nCleaning up temporary extracted files...")
        shutil.rmtree(temp_dir, ignore_errors=True)
        print("Done.")