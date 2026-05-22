"""Generate a fresh batch of fake identities and write data/identities.json.

Usage:
    python -m data.gen_identities          # 120 identities (default)
    python -m data.gen_identities 200      # custom count
"""
import json
import random
import sys
import unicodedata
from pathlib import Path

from data.person import generate_person

_OUT = Path(__file__).parent / "identities.json"


def _deaccent(s: str) -> str:
    return unicodedata.normalize("NFD", s).encode("ascii", "ignore").decode()


def generate_batch(n: int = 120) -> list[dict]:
    seen = set()
    batch = []
    while len(batch) < n:
        p = generate_person()
        p["name"]    = _deaccent(p["name"])
        p["surname"] = _deaccent(p["surname"])
        if p["username"] in seen:
            base = (_deaccent(p["name"])[:3] + _deaccent(p["surname"])[:3]).lower()
            p["username"] = base + str(random.randint(1000, 9999))
        seen.add(p["username"])
        batch.append(p)
    return batch


if __name__ == "__main__":
    count = int(sys.argv[1]) if len(sys.argv) > 1 else 120
    batch = generate_batch(count)
    _OUT.write_text(json.dumps(batch, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"Wrote {len(batch)} identities -> {_OUT}")
