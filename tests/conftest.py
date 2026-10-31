"""Test configuration: make the vendored tlink package and tools importable."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "custom_components" / "dsc_tlink"))
sys.path.insert(0, str(ROOT / "tools"))
