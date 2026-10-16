import sys

import wtx  # noqa: F401  (prepares sys.path for the bundled Dagor Asset Explorer backend)
from wtx.gui import run

if __name__ == "__main__":
    sys.exit(run())
