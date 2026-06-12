"""
Generate synthetic (fake) test accounts and append them to test_accounts.csv.

All identity fields are randomly generated — no real passport data is used.
Safe for testing registration, login, and apply flows.

CPV passport format confirmed from live data: PA + 6 digits (e.g. PA381924)

Usage:
  uv run python gen_fake_accounts.py --count 50 --mode test
  uv run python gen_fake_accounts.py --count 50 --mode test --dry-run
"""

import argparse
import csv
import random
import string
from datetime import date, timedelta
from pathlib import Path

_DATA = Path(__file__).parent / "data"

FIELDNAMES = [
    "id", "username", "password", "status", "account_type",
    "first_name", "last_name", "gender", "birthdate",
    "nationality", "traveldoc", "email", "email_pass",
    "proxy", "proxy_idx", "registered_at", "last_login",
    "appointment_ref", "notes",
]

# ── Name pools ────────────────────────────────────────────────────────────────

_MALE_FIRST = [
    "CARLOS", "JOAO", "PAULO", "ANTONIO", "JOSE", "PEDRO", "LUIS", "MANUEL",
    "MIGUEL", "RAFAEL", "NELSON", "EDSON", "MARIO", "RICARDO", "FABIO",
    "AMILCAR", "ELISEU", "HERNANI", "CELESTINO", "ADILSON", "GILSON",
    "LEANDRO", "RONALDO", "SERGIO", "WANDERSON", "HELDER", "EDER", "NILTON",
    "DARIO", "FELICIANO", "EUCLIDES", "ERNESTO",
]

_FEMALE_FIRST = [
    "MARIA", "ANA", "SOFIA", "LAURA", "ISABEL", "MARTA", "CATARINA",
    "INES", "FERNANDA", "CLAUDIA", "PATRICIA", "VANESSA", "ELISA",
    "MARLENE", "EUNICE", "SILVANA", "ROSARIA", "FILOMENA", "ESPERANCA",
    "DULCE", "GRACIETE", "LURDES", "ALDINA", "EDNA", "LETICIA",
]

_LAST = [
    "SILVA", "SANTOS", "PEREIRA", "COSTA", "FERREIRA", "LOPES", "CARVALHO",
    "RODRIGUES", "MONTEIRO", "TAVARES", "GOMES", "BORGES", "SEMEDO",
    "FURTADO", "ANDRADE", "BARROS", "PIRES", "VARELA", "MOREIRA",
    "CUNHA", "MATOS", "RAMOS", "CORREIA", "ALMEIDA", "MENDES",
    "NEVES", "TEIXEIRA", "LIMA", "VIEIRA", "FERNANDES",
]


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_username(first: str, last: str, existing: set[str]) -> str:
    from gen_accounts import _make_username as _base
    return _base(first, last, existing)


def _make_password() -> str:
    from gen_accounts import _make_password as _base
    return _base()


def _random_birthdate() -> str:
    today = date.today()
    # age 18–60
    start = today - timedelta(days=60 * 365)
    end   = today - timedelta(days=18 * 365)
    delta = (end - start).days
    d = start + timedelta(days=random.randint(0, delta))
    return f"{d.year}/{d.month:02d}/{d.day:02d}"


def _random_traveldoc(existing: set[str]) -> str:
    for _ in range(1000):
        digits = "".join(random.choices(string.digits, k=6))
        td = f"PA{digits}"
        if td not in existing:
            existing.add(td)
            return td
    raise RuntimeError("Cannot generate unique traveldoc after 1000 attempts")


def _random_name(gender: str) -> tuple[str, str]:
    pool = _MALE_FIRST if gender == "M" else _FEMALE_FIRST
    first = random.choice(pool)
    # occasionally use a compound first name
    if random.random() < 0.2:
        first = first + " " + random.choice(pool)
    # occasionally use a compound last name
    last = random.choice(_LAST)
    if random.random() < 0.3:
        last = last + " " + random.choice(_LAST)
    return first, last


def load_existing(path: Path) -> tuple[set[str], set[str], int]:
    if not path.exists() or path.stat().st_size < 50:
        return set(), set(), 0
    rows = list(csv.DictReader(path.read_text(encoding="utf-8").splitlines()))
    traveldocs = {r["traveldoc"] for r in rows if r.get("traveldoc")}
    usernames  = {r["username"]  for r in rows if r.get("username")}
    max_id = max((int(r["id"]) for r in rows if r.get("id", "").isdigit()), default=0)
    return traveldocs, usernames, max_id


def generate_rows(count: int, existing_traveldocs: set[str],
                  existing_usernames: set[str], start_id: int) -> list[dict]:
    rows = []
    next_id = start_id + 1
    for _ in range(count):
        gender = "M" if random.random() < 0.55 else "F"
        first, last = _random_name(gender)
        traveldoc   = _random_traveldoc(existing_traveldocs)
        username    = _make_username(first, last, existing_usernames)
        password    = _make_password()
        birthdate   = _random_birthdate()
        rows.append({
            "id":              next_id,
            "username":        username,
            "password":        password,
            "status":          "new",
            "account_type":    "test",
            "first_name":      first,
            "last_name":       last,
            "gender":          gender,
            "birthdate":       birthdate,
            "nationality":     "CPV",
            "traveldoc":       traveldoc,
            "email":           "",
            "email_pass":      "",
            "proxy":           "",
            "proxy_idx":       "",
            "registered_at":   "",
            "last_login":      "",
            "appointment_ref": "",
            "notes":           "",
        })
        next_id += 1
    return rows


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate synthetic test accounts")
    parser.add_argument("--count", type=int, default=50,
                        help="Number of accounts to generate (default: 50)")
    parser.add_argument("--mode", default="test",
                        help="Run mode (default: test → test_accounts.csv)")
    parser.add_argument("--out", default="",
                        help="Override output CSV path")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print preview without writing")
    args = parser.parse_args()

    from mode_config import get_mode_cfg
    cfg = get_mode_cfg({}, args.mode)
    out_file = Path(args.out) if args.out else Path(cfg["accounts_file"])

    existing_traveldocs, existing_usernames, max_id = load_existing(out_file)
    print(f"[gen_fake] output: {out_file}")
    print(f"[gen_fake] existing accounts: {len(existing_traveldocs)}  (max id={max_id})")

    rows = generate_rows(args.count, existing_traveldocs, existing_usernames, max_id)
    print(f"[gen_fake] generating {len(rows)} new fake accounts")

    if args.dry_run:
        print("\n--- DRY RUN ---")
        for r in rows[:10]:
            print(f"  id={r['id']}  {r['username']}  {r['traveldoc']}"
                  f"  {r['first_name']} {r['last_name']}  {r['birthdate']}  {r['gender']}")
        if len(rows) > 10:
            print(f"  ... and {len(rows) - 10} more")
        return

    write_header = not out_file.exists() or out_file.stat().st_size < 50
    with out_file.open("a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDNAMES)
        if write_header:
            writer.writeheader()
        writer.writerows(rows)

    print(f"[gen_fake] wrote {len(rows)} rows -> {out_file}")


if __name__ == "__main__":
    main()
