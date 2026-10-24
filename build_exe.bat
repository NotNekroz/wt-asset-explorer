@echo off
rem Builds ONE self-contained WT-Asset-Explorer.exe.
rem Installs Python 3.13 automatically if no compatible Python is found.
setlocal
cd /d "%~dp0"

if not exist wt_ext_cli.exe (
  echo [ERROR] wt_ext_cli.exe is missing - run run.bat once first
  goto :fail
)
if not exist texconv.exe (
  echo [ERROR] texconv.exe is missing - run run.bat once first
  goto :fail
)
if not exist lib\daKernel-dev.dll (
  echo [ERROR] lib\daKernel-dev.dll is missing - the export would fail without the lib folder
  goto :fail
)

rem ---- Find a compatible Python (3.13 preferred, then 3.12) ----
set PYCMD=
py -3.13 -c "pass" >nul 2>&1 && set PYCMD=py -3.13
if not defined PYCMD py -3.12 -c "pass" >nul 2>&1 && set PYCMD=py -3.12
if not defined PYCMD if exist "%LOCALAPPDATA%\Programs\Python\Python313\python.exe" set PYCMD="%LOCALAPPDATA%\Programs\Python\Python313\python.exe"

rem ---- Install Python 3.13 if not found ----
if not defined PYCMD (
  echo Python 3.13 or 3.12 not found. Installing Python 3.13 for the current user...
  winget install -e --id Python.Python.3.13 --scope user --silent --accept-package-agreements --accept-source-agreements
  if errorlevel 1 (
    echo winget failed or is unavailable. Downloading the installer from python.org...
    curl -L -o "%TEMP%\python-3.13-installer.exe" https://www.python.org/ftp/python/3.13.7/python-3.13.7-amd64.exe || goto :fail
    "%TEMP%\python-3.13-installer.exe" /quiet InstallAllUsers=0 PrependPath=1 Include_launcher=0 || goto :fail
  )
  if exist "%LOCALAPPDATA%\Programs\Python\Python313\python.exe" set PYCMD="%LOCALAPPDATA%\Programs\Python\Python313\python.exe"
)

if not defined PYCMD (
  echo [ERROR] Python could not be installed or found. Install Python 3.13 from python.org, then run this again.
  goto :fail
)

echo Using Python: %PYCMD%

if not exist .venv-build\Scripts\python.exe (
  %PYCMD% -m venv .venv-build || goto :fail
)
call .venv-build\Scripts\activate.bat
python -m pip install --upgrade pip
python -m pip install pyinstaller -r requirements.txt || goto :fail

rmdir /s /q build dist 2>nul
python -m PyInstaller --noconfirm --clean wt_asset_explorer.spec || goto :fail

echo.
echo Done: dist\WT-Asset-Explorer.exe
pause
exit /b 0

:fail
echo.
echo Build stopped. Read the message above.
pause
exit /b 1
