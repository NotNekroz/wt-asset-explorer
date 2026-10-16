"""War Thunder Asset Explorer - glue between the Vehicle Explorer and the Dagor Asset Explorer backends."""
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# The vendored Dagor Asset Explorer backend expects to be imported as top-level
# packages ("util", "parse"), exactly like in its own repository.
os.environ.setdefault("WTX_ROOT", str(ROOT))
for _p in (ROOT, ROOT / "dae_core"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))
