from __future__ import annotations

import sys
from pathlib import Path

# The tools are plain scripts in the folder above, not a package.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
