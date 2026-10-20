# PyInstaller spec: one single .exe (--onefile) with everything inside.
# Build with build_exe.bat (or: pyinstaller --noconfirm --clean wt_asset_explorer.spec).
from pathlib import Path
from PyInstaller.utils.hooks import collect_all

root = Path(SPECPATH)

datas, binaries, hiddenimports = [], [], []

# The two helper programs the app runs as sub-processes. They are unpacked next to everything else at start-up;
# the code looks for them in sys._MEIPASS.
for exe in ("wt_ext_cli.exe", "texconv.exe"):
    if not (root / exe).is_file():
        raise SystemExit(f"{exe} is missing next to the spec file (run run.bat once to download it)")
    binaries.append((str(root / exe), "."))

# Folders the app reads at run time, bundled whole: lib/ holds the DLLs (daKernel-dev.dll does the Oodle
# decompression, dae_intrinsics.dll the mesh maths - without lib/ every export fails), res/ the icon and placeholder
# images. They are unpacked to sys._MEIPASS, which is where the code looks.
for folder in ("lib", "res", "ui"):
    if (root / folder).is_dir():
        datas.append((str(root / folder), folder))
if not (root / "lib" / "daKernel-dev.dll").is_file():
    raise SystemExit("lib\\daKernel-dev.dll is missing - the export would fail. Put the project's lib folder next to the spec.")

# Python packages that load parts of themselves dynamically
for pkg in ("pygltflib", "zstandard", "dataclasses_json"):
    try:
        d, b, h = collect_all(pkg)
        datas += d; binaries += b; hiddenimports += h
    except Exception:
        pass

a = Analysis(
    [str(root / "main.py")],
    # dae_core is imported as top-level 'util' / 'parse'; vehicle_core and wtx are normal packages in the root
    pathex=[str(root), str(root / "dae_core")],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports + ["util", "parse"],
    excludes=["tkinter", "PySide6.QtWebEngineCore", "PySide6.QtWebEngineWidgets", "PySide6.Qt3DCore",
              "PySide6.QtQuick", "PySide6.QtQml", "PySide6.QtMultimedia", "PySide6.QtCharts"],
    noarchive=False,
)
pyz = PYZ(a.pure)

icon = root / "res" / "icon.ico"
exe = EXE(
    pyz, a.scripts, a.binaries, a.datas, [],
    name="WT-Asset-Explorer",
    icon=str(icon) if icon.is_file() else None,
    console=False,          # GUI app: no console window. Set True to see the log / use the --cli mode output
    upx=False,              # UPX makes antivirus false positives worse
    runtime_tmpdir=None,
)
