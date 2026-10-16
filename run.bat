@echo off
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
    echo First run: creating a virtual environment and installing dependencies...
    py -3 -m venv .venv || goto :fail
    ".venv\Scripts\python.exe" -m pip install --upgrade pip
    ".venv\Scripts\python.exe" -m pip install -r requirements.txt || goto :fail
)
".venv\Scripts\python.exe" main.py
goto :eof
:fail
echo Setup failed. Make sure Python 3.10 - 3.12 is installed (python.org) and try again.
pause
