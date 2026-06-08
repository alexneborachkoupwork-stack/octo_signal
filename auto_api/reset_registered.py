"""
Reset registered accounts for re-registration.

For every account with status=registered:
  - Generate a new username (old one is burned on the server)
  - Generate a new password
  - Clear: email, email_pass, proxy, proxy_idx, registered_at, last_login, notes
  - Set status -> new

Personal data (first_name, last_name, gender, birthdate, nationality, traveldoc)
is preserved unchanged.

Usage:
  uv run python reset_registered.py [--dry-run] [--account-type real|fake] [--status registered|failed|all]
"""

import argparse
import csv
import random
import re
import string
import unicodedata
from pathlib import Path

_ACCOUNT_FILE = Path(__file__).parent / "data" / "accounts.csv"

FIELDNAMES = [
    "id", "username", "password", "status", "account_type",
    "first_name", "last_name", "gender", "birthdate",
    "nationality", "traveldoc", "email", "email_pass",
    "proxy", "proxy_idx", "registered_at", "last_login",
    "appointment_ref", "notes",
]


def _normalize(s: str) -> str:
    nfkd = unicodedata.normalize("NFKD", s)
    return "".join(c for c in nfkd if not unicodedata.combining(c))


def _alpha(s: str) -> str:
    return re.sub(r"[^a-z]", "", _normalize(s).lower())


def _make_username(first: str, last: str, taken: set[str]) -> str:
    a = _alpha(first)[:3].ljust(3, "x")
    b = _alpha(last)[:3].ljust(3, "x")
    base = a + b
    for _ in range(500):
        candidate = base + str(random.randint(1000, 9999))
        if candidate not in taken:
            taken.add(candidate)
            return candidate
    raise RuntimeError(f"Cannot generate unique username for {first} {last}")


def _make_password() -> str:
    upper  = random.choices(string.ascii_uppercase, k=2)
    digits = random.choices(string.digits, k=4)
    lower  = random.choices(string.ascii_lowercase, k=6)
    pool   = upper + digits + lower
    random.shuffle(pool)
    return "".join(pool) + "!#"


def main() -> None:
    import sys
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--account-type", choices=("fake", "real"), default=None,
                        dest="account_type")
    parser.add_argument("--status", choices=("registered", "failed", "all"),
                        default="registered",
                        help="Which status to reset (default: registered)")
    args = parser.parse_args()

    rows = list(csv.DictReader(_ACCOUNT_FILE.read_text(encoding="utf-8").splitlines()))
    taken = {r["username"] for r in rows if r.get("username")}

    want = {"registered", "failed"} if args.status == "all" else {args.status}
    targets = [r for r in rows if r["status"] in want]
    if args.account_type:
        targets = [r for r in targets if r.get("account_type") == args.account_type]

    print(f"[reset] {len(targets)} accounts (status={args.status}) to reset "
          f"(account_type={args.account_type or 'all'})")

    changes: list[tuple[str, str, str]] = []  # (id, old_username, new_username)
    for row in targets:
        new_user = _make_username(row["first_name"], row["last_name"], taken)
        new_pass = _make_password()
        changes.append((row["id"], row["username"], new_user))
        if args.dry_run:
            print(f"  id={row['id']:>3}  {row['username']} -> {new_user}  "
                  f"({row['first_name']} {row['last_name']})")
        else:
            row["username"]       = new_user
            row["password"]       = new_pass
            row["status"]         = "new"
            row["email"]          = ""
            row["email_pass"]     = ""
            row["proxy"]          = ""
            row["proxy_idx"]      = ""
            row["registered_at"]  = ""
            row["last_login"]     = ""
            row["notes"]          = ""

    if args.dry_run:
        print(f"\n[reset] dry-run: no changes written ({len(changes)} would be reset)")
        return

    with _ACCOUNT_FILE.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDNAMES)
        writer.writeheader()
        writer.writerows(rows)

    print(f"[reset] wrote {_ACCOUNT_FILE.name}")
    print(f"[reset] {len(changes)} accounts reset to status=new with new credentials")
    for acct_id, old, new in changes:
        print(f"  id={acct_id:>3}  {old} -> {new}")


if __name__ == "__main__":
    main()
