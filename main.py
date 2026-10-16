import os
import sys
from pathlib import Path

# Set base directory (handles PyInstaller temp extraction path vs normal script execution)
if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
  base_dir = Path(sys._MEIPASS)
else:
  base_dir = Path(__file__).parent

# 1. Add dae_core to sys.path for module resolution ('util', 'parse')
dae_core_path = base_dir / "dae_core"
if dae_core_path.exists() and str(dae_core_path) not in sys.path:
  sys.path.insert(0, str(dae_core_path))

# 2. Add lib/ to Windows C-DLL search path for daKernel-dev.dll & dae_intrinsics.dll
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

# Prepare sys.path for wtx backend
import wtx  # noqa: F401

from wtx.gui import run

if __name__ == "__main__":
  sys.exit(run())