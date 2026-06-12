"""
Parse auto_api/data/paria_new_unique.txt → append new rows to accounts.csv.
Skips entries whose traveldoc already exists in the CSV (idempotent).

Usage:
  uv run python gen_accounts.py [--file data/paria_new_unique.txt] [--dry-run]
"""

import argparse
import csv
import random
import re
import string
import unicodedata
from pathlib import Path

_ACCOUNT_FILE  = Path(__file__).parent / "data" / "accounts.csv"
_DEFAULT_INPUT = Path(__file__).parent / "data" / "paria_new_unique.txt"
_DEFAULT_ACCOUNT_TYPE = "real"

FIELDNAMES = [
    "id", "username", "password", "status", "account_type",
    "first_name", "last_name", "gender", "birthdate",
    "nationality", "traveldoc", "email", "email_pass",
    "proxy", "proxy_idx", "registered_at", "last_login",
    "appointment_ref", "notes",
]

NATIONALITY_MAP = {
    "CABO-VERDIANA": "CPV",
    "CABO-VERDIANO": "CPV",
    "IVOIRIENNE":    "CIV",
    "IVOIRIEN":      "CIV",
    "GUINEENSE":     "GNB",
    "SENEGALESA":    "SEN",
    "ANGOLANA":      "AGO",
    "PORTUGUESA":    "PRT",
    "BRASILEIRA":    "BRA",
    "NIGERIANA":     "NGA",
}

# Known OCR artifacts found in this dataset
_OCR_RE = re.compile(
    r"KXALEX|KTAVARES|KCARDOSO|PVSEMEDO|GABRLIEL|VLOPES|FRELRE|BRYCEC|ADILSONK|"
    r"\bRENDES\b|\bOMES\b|\bMARID\b",
    re.IGNORECASE,
)


def _normalize(s: str) -> str:
    nfkd = unicodedata.normalize("NFKD", s)
    return "".join(c for c in nfkd if not unicodedata.combining(c))


def _alpha(s: str) -> str:
    return re.sub(r"[^a-z]", "", _normalize(s).lower())


def _make_username(first: str, last: str, existing: set[str]) -> str:
    a = _alpha(first)[:3].ljust(3, "x")
    b = _alpha(last)[:3].ljust(3, "x")
    base = a + b
    for _ in range(300):
        candidate = base + str(random.randint(1000, 9999))
        if candidate not in existing:
            existing.add(candidate)
            return candidate
    raise RuntimeError(f"Cannot generate unique username for {first} {last}")


def _make_password() -> str:
    upper  = random.choices(string.ascii_uppercase, k=2)
    digits = random.choices(string.digits, k=4)
    lower  = random.choices(string.ascii_lowercase, k=6)
    pool   = upper + digits + lower
    random.shuffle(pool)
    return "".join(pool) + "!#"


def _parse_date(raw: str) -> tuple[str, str]:
    """DD-MM-YYYY (tolerates OCR errors) → YYYY/MM/DD.  Returns (date, note)."""
    raw = raw.strip()
    note = ""

    # Fix missing dash: 27-051989 → 27-05-1989
    m = re.match(r"^(\d{2})-(\d{6})$", raw)
    if m:
        raw = f"{m.group(1)}-{m.group(2)[:2]}-{m.group(2)[2:]}"
        note = f"date-repaired:{raw}"

    # Fix single-digit month: 13-1-2013 → 13-01-2013
    m = re.match(r"^(\d{1,2})-(\d{1})-(\d{4})$", raw)
    if m:
        raw = f"{m.group(1).zfill(2)}-{m.group(2).zfill(2)}-{m.group(3)}"
        if not note:
            note = f"date-repaired:{raw}"

    m = re.match(r"^(\d{2})-(\d{2})-(\d{4})$", raw)
    if m:
        return f"{m.group(3)}/{m.group(2)}/{m.group(1)}", note

    return raw, f"date-unparseable:{raw}"


def _ocr_note(first: str, last: str) -> str:
    combined = f"{first} {last}"
    if _OCR_RE.search(combined):
        return f"ocr-artifact:{combined.strip()}"
    return ""


def _parse_entries(txt: str) -> list[dict]:
    entries, current = [], {}
    field_map = {
        "First Name":      "first_name",
        "Last Name":       "last_name",
        "Date of Birth":   "birthdate",
        "Gender":          "gender",
        "Nationality":     "nationality",
        "Passport Number": "traveldoc",
    }
    for line in txt.splitlines():
        line = line.strip()
        if line.startswith("==="):
            if current:
                entries.append(current)
            current = {}
            continue
        if ":" not in line:
            continue
        key, _, val = line.partition(":")
        key = key.strip()
        if key in field_map:
            current[field_map[key]] = val.strip()
    if current:
        entries.append(current)
    return entries


def load_existing(path: Path) -> tuple[set[str], set[str], int]:
    if not path.exists():
        return set(), set(), 0
    rows = list(csv.DictReader(path.read_text(encoding="utf-8").splitlines()))
    traveldocs = {r["traveldoc"] for r in rows if r.get("traveldoc")}
    usernames  = {r["username"]  for r in rows if r.get("username")}
    max_id = max((int(r["id"]) for r in rows if r.get("id", "").isdigit()), default=0)
    return traveldocs, usernames, max_id


def generate_rows(entries: list[dict], existing_traveldocs: set[str],
                  existing_usernames: set[str], start_id: int,
                  account_type: str = "real") -> list[dict]:
    rows = []
    next_id = start_id + 1

    for e in entries:
        traveldoc = e.get("traveldoc", "").strip()
        if not traveldoc or traveldoc in existing_traveldocs:
            continue
        existing_traveldocs.add(traveldoc)

        first = e.get("first_name", "").strip()
        last  = e.get("last_name",  "").strip()
        nat   = NATIONALITY_MAP.get(e.get("nationality", "").strip().upper(),
                                    e.get("nationality", "").strip().upper())
        birthdate, date_note = _parse_date(e.get("birthdate", "").strip())
        ocr   = _ocr_note(first, last)
        notes = "; ".join(n for n in [date_note, ocr] if n)

        rows.append({
            "id":              next_id,
            "username":        _make_username(first, last, existing_usernames),
            "password":        _make_password(),
            "status":          "new",
            "account_type":    account_type,
            "first_name":      first,
            "last_name":       last,
            "gender":          e.get("gender", "").strip(),
            "birthdate":       birthdate,
            "nationality":     nat,
            "traveldoc":       traveldoc,
            "email":           "",
            "email_pass":      "",
            "proxy":           "",
            "proxy_idx":       "",
            "registered_at":   "",
            "last_login":      "",
            "appointment_ref": "",
            "notes":           notes,
        })
        next_id += 1

    return rows


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--file",         default=str(_DEFAULT_INPUT))
    parser.add_argument("--out",          default="",
                        help="Output CSV file (default: driven by --mode)")
    parser.add_argument("--account-type", default="",
                        choices=["real", "test", ""],
                        help="account_type written to CSV (default: driven by --mode)")
    parser.add_argument("--mode",         default="",
                        help="Run mode: test or real (default: real; or MODE from .env)")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    from mode_config import get_mode_cfg
    cfg = get_mode_cfg({}, args.mode)

    out_file     = Path(args.out) if args.out else Path(cfg["accounts_file"])
    account_type = args.account_type or cfg["account_type"]

    src = Path(args.file)
    if not src.exists():
        print(f"ERROR: {src} not found")
        return

    entries = _parse_entries(src.read_text(encoding="utf-8"))
    print(f"[gen] parsed {len(entries)} entries from {src.name}")

    existing_traveldocs, existing_usernames, max_id = load_existing(out_file)
    print(f"[gen] existing accounts: {len(existing_traveldocs)}  (max id={max_id})")

    rows = generate_rows(entries, existing_traveldocs, existing_usernames, max_id,
                         account_type=account_type)
    flagged = [r for r in rows if r["notes"]]
    print(f"[gen] new rows: {len(rows)}  ({len(flagged)} with flags)")

    if args.dry_run:
        print("\n--- DRY RUN (first 10) ---")
        for r in rows[:10]:
            flag = f"  [!{r['notes']}]" if r["notes"] else ""
            print(f"  id={r['id']}  {r['username']}  {r['traveldoc']}"
                  f"  {r['first_name']} {r['last_name']}  {r['birthdate']}"
                  f"  {r['nationality']}{flag}")
        if len(rows) > 10:
            print(f"  ... and {len(rows)-10} more")
        if flagged:
            print(f"\n--- Flagged entries ({len(flagged)}) ---")
            for r in flagged:
                print(f"  id={r['id']}  {r['first_name']} {r['last_name']}  {r['notes']}")
        return

    if not rows:
        print("[gen] nothing to add — all traveldocs already in CSV")
        return

    write_header = not out_file.exists()
    with out_file.open("a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDNAMES)
        if write_header:
            writer.writeheader()
        writer.writerows(rows)

    print(f"[gen] wrote {len(rows)} rows -> {out_file}")
    if flagged:
        print(f"\n[gen] flagged entries (review before registration):")
        for r in flagged:
            print(f"  id={r['id']}  {r['first_name']} {r['last_name']}  {r['notes']}")


if __name__ == "__main__":
    main()
