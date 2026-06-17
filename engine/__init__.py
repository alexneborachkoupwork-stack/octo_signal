"""
Engine layer — Manager + Worker architecture.

Adds auto_api/ to sys.path so all engine modules can import from it directly.
"""
import sys
from pathlib import Path

_auto_api = Path(__file__).parent.parent / "auto_api"
if str(_auto_api) not in sys.path:
    sys.path.insert(0, str(_auto_api))
