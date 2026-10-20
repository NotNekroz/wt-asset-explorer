# War Thunder Asset Explorer

A standalone Windows desktop tool for **War Thunder** that lets you browse every vehicle in the game, pick a loadout the way the in-game editor does, and export the vehicle as a ready-to-use **3D model** (`.glb` or `.obj`) with its textures, animations and weapons. It is built with **PySide6** on top of the **Dagor Asset Explorer** backend and reads everything straight from your own game installation.

![Python](https://img.shields.io/badge/Python-3.10%2B-blue)
![PySide6](https://img.shields.io/badge/UI-PySide6-green)
![Platform](https://img.shields.io/badge/Platform-Windows-lightgrey)

---

## What it can do

### Browse vehicles
* **Vehicle catalog** built from the game's own data (`aces.vromfs.bin`, `char.vromfs.bin`, `lang.vromfs.bin`). Mission props, test dummies and internal entries are filtered out.
* **Search and filters** by name or id, nation and vehicle type (planes, tanks, helicopters, ships).
* **Real in-game names with their icons**: the premium, squadron and nation markers that the game puts in front of vehicle names are drawn with the game's own `symbols_skyquake` font, which is unpacked from your installation on first launch (nothing from the game is bundled with the tool).
* **Preview image** of the selected vehicle, shown with its id, nation, type and the matched 3D asset.
* **Automatic model matching**: each vehicle is matched to its `.grp` model; when that fails you get a picker with every model in the game instead of a dead end, and your choice is remembered.

### Loadout editor
* **Slot cells like the game's editor**: one icon cell per weapon slot, in the game's order. Click a cell to see that slot's weapons grouped by category, each with its icon, name and mass.
* **Stock loadouts**: a drop-down with the game's stock presets, named readably (for example `AIM-9M Default`, `GBU-54B PIDS`, `Mk 82 Snakeye`).
* **Game rules applied**: dependencies between slots, banned combinations, required modifications and weapon mass come straight from the vehicle data.
* **Weapon icons** from the game's UI atlases, or plain names if you prefer (setting).
* **Clear All Slots** to go back to a bare vehicle.

### Export
* **Formats**
  * **GLB**: one binary glTF file with materials, textures, animations and loadout models.
  * **OBJ**: `.obj` + `.mtl` + textures.
* **Textures**: exported as `.dds` / PNG-compatible files, converted with `texconv`. Textures that the finished model does not use are removed.
* **Animations** (GLB): the vehicle's `.a2d` animation (landing gear, flaps, hatches and so on) is decoded and written as named glTF animation clips.
* **Weapon models**: the 3D models of the chosen loadout (missiles, bombs, racks, tanks) are attached to the vehicle's weapon nodes.
* **Loadout-aware cleanup**: pylons, rails and pods that the chosen loadout does not show are removed, and in-game effect geometry (jet flames, gun fire, flares) is stripped so the model opens cleanly in Blender and similar tools.
* **Armament export**: export a single weapon on its own, or every weapon of the vehicle into organised folders.
* **Skin template**: creates a ready-to-edit custom skin for the game's UserSkins folder: the `.blk`, the vehicle's colour (`_c`) and normal (`_n`) textures as `.tga` (with the default camo baked into the colour textures as a starting point), an `INSTRUCTIONS.txt`, and a `uv_guides_ROUGH` folder with one UV outline PNG per colour texture to use as a painting guide.
* **Custom skin preview**: **Preview custom skin...** exports the selected vehicle (OBJ or GLB, whichever format is selected) with a skin folder's own textures on it, into a separate `skin_preview_<skin name>` folder, so you can check your painting in Blender before starting the game.
  * Textures the skin supplies (`.tga`, `.dds` or `.png`, matched through the `.blk` `from`/`to` entries or by file name) are used directly: no game texture is extracted for them and no camo is baked in. Textures the skin does not supply (tracks, machine guns, weapon models and so on) come from the game files as in a normal export.
  * Skin normal maps (`_n`) are read in the game's channel layout (red = smoothness, green = normal Y, blue = metalness, alpha = normal X), so panel lines you edited show up. Keep the alpha channel when saving an edited `_n`, because it holds the normal's X.
  * The export log ends with `[skin] used N skin texture(s)` and lists skin files the model does not use, which helps when a texture name does not match.
  * Use a fresh output folder (or delete the old `skin_preview_...` folder) when re-running a preview, otherwise baked files from an earlier run can stay in it.
* **Organised output**: files go to `<output folder>/<Nation>/<Type>/<Vehicle>/`.
* **High-resolution assets**: HQ content (`content.hq` and `-hq` texture packs) is preferred automatically when it is installed.

### Everything else
* **Works with Steam and Gaijin Launcher installs**: the game folder is detected automatically and can be changed in Settings.
* **Cached for fast start-ups**: the unpacked game data and the asset index are cached and rebuilt only when the game updates. Use **Clear cache and reload** in Settings to force it.
* **Settings dialog**: game folder, output folder, reload / clear cache, "Export Armament exports every weapon", and weapon icons on or off.
* **Command-line mode** for batch exports without opening the window.
* **Dark custom-titlebar UI** with a live log panel and status bar.

---

## Command-line mode

```bash
python main.py --cli fa_18e f_16c
python main.py --cli -f vehicles.txt
```

* Pass vehicle ids or names directly (fuzzy matching is used when there is no exact match).
* `-f / --file` reads a plain list (one vehicle per line) or a Windows `tree /F /A` text export of a folder of vehicles.
* Exports are written to `Documents/WT Exports`.

---

## Project structure

```text
├── main.py                     # Entry point (GUI, or headless with --cli)
├── run.bat                     # Automatic launcher & environment setup
├── build_exe.bat               # Builds the single-file WT-Asset-Explorer.exe (PyInstaller)
├── wt_asset_explorer.spec      # PyInstaller build definition used by build_exe.bat
├── requirements.txt            # Python dependencies
├── wt_ext_cli.exe              # War Thunder VROMF unpacking utility (downloaded by run.bat)
├── texconv.exe                 # DirectXTex texture converter (downloaded by run.bat)
├── wtx/                        # Application layer
│   ├── gui.py                  # Main window, title bar, export workflows
│   ├── theme.py                # Dark stylesheet
│   ├── loadout_panel.py        # Slot cells and weapon picker
│   ├── settings_dialog.py      # Settings window
│   ├── vehicles.py             # Vehicle catalog, caching, name resolution
│   ├── assets.py               # Model/texture index and export pipeline
│   ├── fonts.py                # Unpacks and applies the game's icon font
│   ├── skin_blk.py             # Custom skin .blk writer and skin folder reader
│   ├── skin_docs.py            # INSTRUCTIONS.txt written into skin templates
│   └── uv_guide.py             # UV outline guides for skin templates
├── vehicle_core/               # Vehicle data logic
│   ├── Loadout.py              # Weapon slots, presets, dependencies, stock loadouts
│   ├── LoadoutSearch.py        # Weapon lookup and localisation
│   ├── VehicleLook.py          # Game folder detection, nations, wt_ext_cli wrapper
│   └── Icons.py                # Weapon icon extraction and conversion
└── dae_core/                   # Vendored Dagor Asset Explorer backend
    ├── parse/                  # Model, material, animation and glTF/GLB/OBJ code
    │   ├── gltf_conv.py        # glTF / GLB writer
    │   ├── a2d_anim.py, acl_decode.py, anm_graph.py, anm_clips.py   # Animation decoding
    │   ├── glb_merge.py, obj_merge.py                               # Attach weapon models
    │   ├── loadout_filter.py, fx_strip.py, tex_prune.py             # Cleanup passes
    │   └── ...
    └── util/                   # Archives, decompression, caching, logging
```

---

## How to run

### Quick start (Windows)

1. Install [Python 3.10+](https://www.python.org/).
2. Double-click **`run.bat`**.

On first launch `run.bat` downloads `wt_ext_cli.exe` and `texconv.exe` if they are missing, creates a `.venv`, installs the dependencies and starts the tool. Later launches start straight away.

On first use, open **Settings**, check the game folder and press **Reload game data**. The first load unpacks the game data and builds the index, which takes a while; after that it is cached.

### Manual setup

1. **Clone the repository**
   ```bash
   git clone https://github.com/NotNekroz/wt-asset-explorer.git
   cd wt-asset-explorer
   ```
2. **Download the latest binaries** and place them next to `main.py`:
   * `wt_ext_cli.exe`: https://github.com/Warthunder-Open-Source-Foundation/wt_ext_cli/releases/latest
   * `texconv.exe`: https://github.com/microsoft/DirectXTex/releases/latest
3. **Create and activate a virtual environment**
   ```bash
   python -m venv .venv
   .venv\Scripts\activate
   ```
4. **Install the dependencies**
   ```bash
   pip install -r requirements.txt
   ```
5. **Launch**
   ```bash
   python main.py
   ```

### Requirements

* Windows with War Thunder installed.
* Python 3.10+.
* Python packages (installed from `requirements.txt`): PySide6, numpy, Pillow 11.3+ (AVIF support, for the weapon icons), pygltflib and zstandard. `pylzma` is optional.
* An internet connection is only needed for the vehicle preview images.

---

## Notes

* The tool only reads your own game files; no game data is shipped with it. Exported models are for personal use such as skinning, rendering and modding. War Thunder assets belong to Gaijin Entertainment.
* If vehicle-name icons show as boxes, check that the game folder contains `ui/fonts.vromfs.bin` and reload the game data.

---

## Building a standalone executable

On Windows, run **`build_exe.bat`** from the project folder. It creates a `.venv-build` environment (Python 3.13 or 3.12), installs the dependencies from `requirements.txt` plus PyInstaller, and builds one self-contained **`dist\WT-Asset-Explorer.exe`** from `wt_asset_explorer.spec`.

* `wt_ext_cli.exe` and `texconv.exe` must be next to the script (run `run.bat` once to download them). They are bundled into the executable.
* The project's `lib` folder (with `daKernel-dev.dll`, used for the game's Oodle decompression) must also be present. The build stops with a message if it is missing. `res` and `ui` folders are bundled too when they exist.
* Python 3.13 or 3.12 is needed for the build because every dependency has a ready-made Windows wheel there; on newer versions `zstandard` would have to be compiled from source.

---

## Acknowledgments

* Game archive unpacking and VROMF extraction powered by `wt_ext_cli.exe` - Warthunder-Open-Source-Foundation.
* 3D model parsing and texture extraction routines built on **Dagor Asset Explorer** backend logic - quentin-dh.
* ACL animation decoding ported from [Animation Compression Library](https://github.com/nfrechette/acl) (MIT) - Nicholas Frechette.
* Texture conversion with `texconv` from [DirectXTex](https://github.com/microsoft/DirectXTex) - Microsoft.
