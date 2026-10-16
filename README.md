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
├── run.bat                     # Automatic launcher & environment setup
├── requirements.txt            # Dependency specifications
├── wt_ext_cli.exe              # War Thunder VROMF unpacking utility backend
├── wtx/
│   ├── assets.py               # Dagor Asset Explorer interface & indexer
│   └── vehicles.py             # Vehicle catalog scanner & BLK classifier
└── vehicle_core/
    ├── LoadoutSearch.py        # Weapon loadout extraction & resolution
    └── VehicleLook.py          # Path auto-detection & nation mapping
```

---

## How to Run

### Quick Start (Windows)

1. Ensure [Python 3.10+](https://www.python.org/) is installed on your computer.
2. Double-click **`run.bat`**.

> **Note:** On first launch, `run.bat` creates a `.venv` folder, upgrades `pip`, installs `PySide6` from `requirements.txt`, and opens the tool automatically. Subsequent launches start instantly.

---

### Manual Setup

1. **Clone the repository:**
   ```bash
   git clone https://github.com/NotNekroz/wt-asset-explorer.git
   cd wt-asset-explorer
   ```

2. **Create and activate a virtual environment:**
   ```bash
   python -m venv .venv
   .venv\Scripts\activate
   ```

3. **Install dependencies:**
   ```bash
   pip install -r requirements.txt
   ```

4. **Launch the application:**
   ```bash
   python main.py
   ```

---

## Building a Standalone Executable

To compile the application into a standalone Windows executable using **PyInstaller**:

```bash
Work In Progress
```

The output build will be placed inside `dist/main/`.

---

## Acknowledgments

* Game archive unpacking and VROMF extraction powered by `wt_ext_cli.exe` - Warthunder-Open-Source-Foundation.
* 3D model parsing and texture extraction routines built on **Dagor Asset Explorer** backend logic - quentin-dh.
