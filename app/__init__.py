"""
app — the project's single importable package.

Adds app/core/ to sys.path so its modules can be imported directly by their
own names (import session, import solver, from batch_apply import ...), the
same flat-namespace style they already used as auto_api/. This is a deliberate
Phase 0 choice: it lets ~80 existing call sites across app/, probes/ keep
working unchanged while the directory layout is cleaned up. Converting those
imports to proper package-relative form (from app.core import session) is
deferred to a later cleanup phase, done once and carefully.
"""
import sys
from pathlib import Path

_core = Path(__file__).parent / "core"
if str(_core) not in sys.path:
    sys.path.insert(0, str(_core))
