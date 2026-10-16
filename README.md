# War Thunder Asset Explorer

A standalone desktop tool built with **PySide6** and **Dagor Asset Explorer** logic to search War Thunder vehicles, preview CDN renderings, and export 3D models (`.obj`), materials (`.mtl`), and textures (`.dds`) for vehicles and armaments.

![Python](https://img.shields.io/badge/Python-3.10%2B-blue)
![PySide6](https://img.shields.io/badge/UI-PySide6-green)
![Platform](https://img.shields.io/badge/Platform-Windows-lightgrey)

---

## Key Features

* **Filtered Vehicle Catalog**: Automatically scans game VROMF containers (`aces.vromfs.bin`) and indexes units while stripping out mission props, test dummies, and internal metadata.
* **Auto-Detection**: Works with both **Steam** and **Standalone (Gaijin Launcher)** War Thunder installations.
* **High-Res / HQ DLC Support**: Prioritizes `content.hq` containers and high-definition texture packs automatically when installed.
* **Armament Extraction**: Extract individual mounted weapons or batch export all available loadouts for a vehicle into organized folders.
* **Smart Asset Matching**: Resolves weapon names and variants (such as `Snakeye` bombs, air-to-air missiles, and gun pods) directly to their `.grp` model containers.
* **Cached Indexing**: Uses local JSON signatures to make subsequent launches near-instantaneous.

---

## Project Structure

```text
├── main.py                     # Entry point
├── gui.py                      # Main PySide6 UI window & thread bridge
├── wt_ext_cli.exe              # War Thunder VROMF unpacking utility backend
├── wtx/
│   ├── assets.py               # Dagor Asset Explorer interface & indexer
│   └── vehicles.py             # Vehicle catalog scanner & BLK classifier
└── vehicle_core/
    ├── LoadoutSearch.py        # Weapon loadout extraction & resolution
    └── VehicleLook.py          # Path auto-detection & nation mapping