"""Make scraper_lib importable in tests without installing the package.
The repo doesn't ship a setup.py / pyproject; scraper_lib/ is a flat module
dir loaded relative to its own location at runtime. Mirror that here so
`from normalize import ...` works inside tests/.
"""
from __future__ import annotations
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRAPER_LIB = REPO_ROOT / "scraper_lib"
if str(SCRAPER_LIB) not in sys.path:
    sys.path.insert(0, str(SCRAPER_LIB))
