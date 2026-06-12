"""
Global run-mode configuration.

mode=test  → Dublin embassy (posto 2032), IRL residence, test_accounts.csv
mode=real  → production posto (from POSTO_ID env), CPV residence, accounts.csv

Usage in scripts:
    from mode_config import get_mode_cfg
    cfg = get_mode_cfg(env, args.mode)
    posto_id = args.posto or cfg["posto_id"]
"""

import os
from pathlib import Path

_DATA = Path(__file__).parent / "data"

MODES: dict[str, dict] = {
    "test": {
        "accounts_file": str(_DATA / "test_accounts.csv"),
        "posto_id":      "2032",
        "nationality":   "CPV",
        "residence":     "IRL",
        "account_type":  "test",
    },
    "real": {
        "accounts_file": str(_DATA / "accounts.csv"),
        "posto_id":      "",    # filled from POSTO_ID env below
        "nationality":   "CPV",
        "residence":     "CPV",
        "account_type":  "real",
    },
}


def get_mode_cfg(env: dict, cli_mode: str = "") -> dict:
    """Return mode config dict. Resolution: CLI --mode → env MODE → default 'real'."""
    mode = (cli_mode or env.get("MODE", "") or os.environ.get("MODE", "real")).strip().lower()
    cfg = dict(MODES.get(mode, MODES["real"]))
    if not cfg["posto_id"]:
        cfg["posto_id"] = env.get("POSTO_ID", "") or os.environ.get("POSTO_ID", "5086")
    return cfg
