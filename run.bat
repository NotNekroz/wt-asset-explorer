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
    py -3 -m venv .venv || goto :fail
    ".venv\Scripts\python.exe" -m pip install --upgrade pip
    ".venv\Scripts\python.exe" -m pip install -r requirements.txt || goto :fail
)

start "" ".venv\Scripts\pythonw.exe" main.py
exit /b 0

:fail
pause