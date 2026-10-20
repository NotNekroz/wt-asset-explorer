@echo off
rem Builds ONE self-contained WT-Asset-Explorer.exe (Python and the packages from requirements.txt, the lib folder,
rem wt_ext_cli.exe and texconv.exe all inside). Run it from the project folder on Windows.
rem Needs wt_ext_cli.exe, texconv.exe and the lib folder next to this file (run.bat downloads the two exes on its first run).
setlocal
cd /d "%~dp0"

if not exist wt_ext_cli.exe ( echo [ERROR] wt_ext_cli.exe is missing - run run.bat once first & exit /b 1 )
if not exist texconv.exe    ( echo [ERROR] texconv.exe is missing - run run.bat once first & exit /b 1 )
if not exist lib\daKernel-dev.dll ( echo [ERROR] lib\daKernel-dev.dll is missing - the export would fail without the lib folder & exit /b 1 )

rem Python 3.13 (or 3.12): every dependency has a ready-made Windows wheel there. Python 3.15 does not (zstandard
rem would be compiled from source and needs Microsoft C++ Build Tools).
if not exist .venv-build\Scripts\python.exe (
  py -3.13 -m venv .venv-build 2>nul || py -3.12 -m venv .venv-build 2>nul || (
    echo [ERROR] Python 3.13 or 3.12 is not installed. Install one from python.org, then run this again.
    exit /b 1
  )
)
call .venv-build\Scripts\activate.bat
python -m pip install --upgrade pip
python -m pip install pyinstaller -r requirements.txt || exit /b 1

rmdir /s /q build dist 2>nul
python -m PyInstaller --noconfirm --clean wt_asset_explorer.spec || exit /b 1

echo.
echo Done: dist\WT-Asset-Explorer.exe
