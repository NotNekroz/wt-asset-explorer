@echo off
cd /d "%~dp0"

if not exist "wt_ext_cli.exe" (
    powershell -NoProfile -ExecutionPolicy Bypass -Command ^
        "$ErrorActionPreference = 'Stop';" ^
        "$release = Invoke-RestMethod -Uri 'https://api.github.com/repos/Warthunder-Open-Source-Foundation/wt_ext_cli/releases/latest';" ^
        "$asset = $release.assets | Where-Object { $_.name -like '*win*' -or $_.name -like '*.exe' } | Select-Object -First 1;" ^
        "if (-not $asset) { write-error 'No suitable executable asset found in latest release.'; exit 1; }" ^
        "Invoke-WebRequest -Uri $asset.browser_download_url -OutFile 'wt_ext_cli.exe';" || goto :fail
)

if not exist "texconv.exe" (
    powershell -NoProfile -ExecutionPolicy Bypass -Command ^
        "$ErrorActionPreference = 'Stop';" ^
        "$release = Invoke-RestMethod -Uri 'https://api.github.com/repos/microsoft/DirectXTex/releases/latest';" ^
        "$asset = $release.assets | Where-Object { $_.name -like '*texconv*' -and $_.name -like '*.exe' } | Select-Object -First 1;" ^
        "if (-not $asset) { $release = Invoke-RestMethod -Uri 'https://api.github.com/repos/Microsoft/DirectXTex/releases'; $asset = $release | ForEach-Object { $_.assets } | Where-Object { $_.name -like '*texconv*' -and $_.name -like '*.exe' } | Select-Object -First 1; }" ^
        "if (-not $asset) { write-error 'No suitable texconv executable found.'; exit 1; }" ^
        "Invoke-WebRequest -Uri $asset.browser_download_url -OutFile 'texconv.exe';" || goto :fail
)

if not exist ".venv\Scripts\python.exe" (
    rem Python 3.13 / 3.12 first: every dependency has a ready-made Windows wheel there (zstandard would need a C++ compiler on newer versions)
    py -3.13 -m venv .venv 2>nul || py -3.12 -m venv .venv 2>nul || py -3 -m venv .venv || goto :fail
    ".venv\Scripts\python.exe" -m pip install --upgrade pip
    del ".venv\requirements.installed" 2>nul
)

:: (re)install the dependencies whenever requirements.txt changed since the last install
fc /b "requirements.txt" ".venv\requirements.installed" >nul 2>&1
if errorlevel 1 (
    ".venv\Scripts\python.exe" -m pip install -r requirements.txt || goto :fail
    copy /y "requirements.txt" ".venv\requirements.installed" >nul
)

start "" ".venv\Scripts\pythonw.exe" main.py
exit /b 0

:fail
pause