from __future__ import annotations

import sys
from pathlib import Path

# allow running the suite without `pip install -e .`
SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))
