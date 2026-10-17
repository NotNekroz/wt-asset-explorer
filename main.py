import os
import sys
import argparse
import re
from pathlib import Path
import traceback

# 1. IMPORT PYSIDE6 FIRST (Prevents native DLL initialization crashes)
try:
    from PySide6.QtCore import QCoreApplication, QStandardPaths
except Exception as e:
    print(f"[CRITICAL ERROR] Failed to import PySide6: {e}")
    traceback.print_exc()
    sys.exit(1)

# Set base directory (handles PyInstaller temp extraction path vs normal script execution)
if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
  base_dir = Path(sys._MEIPASS)
else:
  base_dir = Path(__file__).parent

# 2. Add dae_core to sys.path for module resolution ('util', 'parse')
dae_core_path = base_dir / "dae_core"
if dae_core_path.exists() and str(dae_core_path) not in sys.path:
  sys.path.insert(0, str(dae_core_path))

# 3. Add lib/ to Windows C-DLL search path AFTER PySide6 is safely loaded
lib_dir = base_dir / "lib"
if lib_dir.exists():
  if hasattr(os, "add_dll_directory"):
    try:
      os.add_dll_directory(str(lib_dir))
    except Exception:
      pass
  os.environ["PATH"] = (
      str(lib_dir) + os.path.pathsep + os.environ.get("PATH", "")
  )

# Prepare backend imports
try:
    import wtx  # noqa: F401
    from wtx import ROOT
    from wtx.assets import AssetLibrary
    from wtx.vehicles import VehicleCatalog, find_wt_ext, looks_like_wt_dir
except Exception as e:
    print(f"[CRITICAL ERROR DURING WTX IMPORTS]: {e}")
    traceback.print_exc()
    sys.exit(1)


def parse_vehicle_queries_from_file(file_path: Path) -> list[str]:
    """Reads a plain text list or a Windows 'tree /F /A' text export and extracts vehicle folder names dynamically."""
    queries = []
    content = file_path.read_text(encoding="utf-8", errors="ignore")
    
    # Check if the file looks like a Windows tree output
    is_tree_format = any(char in content for char in ["├──", "└──", "│", "---", "\\---", "+---"])
    
    if is_tree_format:
        print("[CLI] Detected tree format structure. Parsing directory hierarchy...")
        lines = content.splitlines()
        stack = []  # List of tuples: (indent_level, folder_name)
        
        for line in lines:
            # Skip header lines, volume info, or empty lines
            if not line.strip() or "Folder PATH listing" in line or "Volume serial" in line or "C:\\" in line.upper():
                continue
            
            # Parse indentation and item name using regex matching tree branches
            match = re.match(r'^([\|\s]*)([\+\\]?---)?\s*(.*)$', line)
            if not match:
                continue
            
            prefix, branch, name = match.groups()
            name = name.strip()
            if not name:
                continue
            
            indent = len(prefix)
            if branch:
                indent += len(branch)
            
            # Check if the line represents a file (contains a dot and valid extension)
            is_file = "." in name and not name.startswith(".")
            
            if is_file:
                # When an exported .obj file is found, the vehicle folder is its immediate parent in the stack
                if name.lower().endswith(".obj"):
                    if stack:
                        valid_folders = [f_name for f_indent, f_name in stack if f_indent < indent]
                        if valid_folders:
                            vehicle_name = valid_folders[-1]
                            if vehicle_name not in queries:
                                queries.append(vehicle_name)
            else:
                # It's a directory. Manage the stack by popping items at or deeper than this indent
                while stack and stack[-1][0] >= indent:
                    stack.pop()
                stack.append((indent, name))
    else:
        # Standard line-by-line plain text file
        for line in content.splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                queries.append(line)
                
    # Deduplicate while preserving order
    return list(dict.fromkeys(queries))


def run_cli_export(queries: list[str]):
    print("[CLI] Initializing headless asset exporter...")
    
    if not QCoreApplication.instance():
        app_instance = QCoreApplication(sys.argv)
    QCoreApplication.setApplicationName("WT Asset Explorer")
    
    settings_cache_root = Path(
        QStandardPaths.writableLocation(
            QStandardPaths.StandardLocation.AppLocalDataLocation
        )
        or (Path.home() / ".wt_asset_explorer")
    )
    
    from vehicle_core.VehicleLook import auto_find_wt_path
    game_dir = auto_find_wt_path()
    if not game_dir or not looks_like_wt_dir(game_dir):
        print("[ERROR] Could not automatically locate War Thunder folder.")
        sys.exit(1)
        
    wt_ext = find_wt_ext(ROOT)
    if not wt_ext:
        print("[ERROR] wt_ext_cli.exe not found.")
        sys.exit(1)

    print(f"[CLI] Game folder: {game_dir}")
    output_dir = Path.home() / "Documents" / "WT Exports"
    output_dir.mkdir(parents=True, exist_ok=True)

    print("[CLI] Loading vehicle catalog and indexing assets...")
    catalog = VehicleCatalog(game_dir, wt_ext, settings_cache_root)
    catalog.extract(print, use_cache=True)
    vehicles = catalog.scan(print)
    
    library = AssetLibrary(game_dir, settings_cache_root)
    library.build_index(print)
    library.preload_base_descs(print)

    id_map = {}
    name_map = {}
    for v in vehicles:
        id_map[v.id.lower()] = v
        id_map[v.id.replace("_", "-").lower()] = v
        if v.name and v.name != "N/A":
            name_map[v.name.lower()] = v

    print(f"\n[CLI] Processing {len(queries)} target(s)...")
    for query in queries:
        q_clean = query.lower().strip()
        veh = id_map.get(q_clean) or name_map.get(q_clean)
        
        if not veh:
            matching = [v for k, v in id_map.items() if q_clean in k or (v.name and q_clean in v.name.lower())]
            if matching:
                veh = matching[0]
                print(f"[CLI] Fuzzy matched '{query}' to '{veh.name} ({veh.id})'")
            else:
                print(f"[WARNING] Vehicle '{query}' not found in catalog.")
                continue

        print(f"\n[CLI] Processing vehicle: {veh.name} ({veh.id})...")
        refs = library.find_vehicle_models(veh.id, veh.model_id)
        
        if not refs:
            print(f"[WARNING] No 3D model found automatically for {veh.id}.")
            continue
            
        ref = refs[0]
        display_or_id = veh.name if veh.name and veh.name != "N/A" else veh.id
        dest_dir = output_dir / safe_name(veh.nation) / safe_name(veh.type) / safe_name(display_or_id)
        
        print(f"[CLI] Exporting model '{ref.name}' from {ref.grp_file} to {dest_dir}...")
        try:
            library.export_obj(ref, dest_dir, print, textures=True)
            print(f"[CLI] Successfully exported {veh.id}!")
        except Exception as e:
            print(f"[ERROR] Failed to export {veh.id}: {e}")

    print("\n[CLI] All requested exports finished.")


def safe_name(s: str) -> str:
  clean_name = re.sub(r'[\\/:*?"<>|]', "_", str(s)).strip(" ._")
  return clean_name or "unnamed"


def main():
    parser = argparse.ArgumentParser(description="War Thunder Asset Explorer CLI")
    parser.add_argument("vehicles", nargs="*", help="Vehicle IDs or names to export directly")
    parser.add_argument("-f", "--file", type=Path, help="Path to a text file or folder tree text export containing vehicle list")
    parser.add_argument("--cli", action="store_true", help="Run in headless CLI mode")
    args = parser.parse_args()

    targets = []
    if args.vehicles:
        targets.extend(args.vehicles)

    if args.file:
        if not args.file.exists():
            print(f"[ERROR] Target text file '{args.file}' does not exist.")
            sys.exit(1)
        extracted_targets = parse_vehicle_queries_from_file(args.file)
        targets.extend(extracted_targets)

    if args.cli or targets:
        if not targets:
            print("[ERROR] No vehicle IDs or file provided for export.")
            sys.exit(1)
        run_cli_export(targets)
        sys.exit(0)
    else:
        # Lazily import and launch the standard graphical user interface
        from wtx.gui import run
        sys.exit(run())


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"\n[CRITICAL ERROR] Uncaught exception: {e}")
        traceback.print_exc()
        sys.exit(1)