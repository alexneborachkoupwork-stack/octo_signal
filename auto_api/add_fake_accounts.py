"""Add a few new fake accounts (status=new) for registration testing."""
import csv, random, string
from pathlib import Path

CSV = Path(__file__).parent / "data" / "accounts.csv"
rows = list(csv.DictReader(CSV.read_text(encoding="utf-8").splitlines()))
max_id        = max(int(r["id"]) for r in rows if r["id"].isdigit())
existing_users = {r["username"] for r in rows}
max_doc       = max((int(r["traveldoc"][2:]) for r in rows
                     if r["traveldoc"].startswith("TF") and r["traveldoc"][2:].isdigit()),
                    default=0)
FIELDNAMES = list(rows[0].keys())


def make_password() -> str:
    pool = (random.choices(string.ascii_uppercase, k=2) +
            random.choices(string.digits, k=4) +
            random.choices(string.ascii_lowercase, k=6))
    random.shuffle(pool)
    return "".join(pool) + "!#"


FAKE_PEOPLE = [
    ("MIGUEL", "SANTOS LIMA",   "M", "1990/03/12", "CPV", "mig", "san"),
    ("ROSA",   "TAVARES CRUZ",  "F", "1993/07/28", "CPV", "ros", "tav"),
    ("PEDRO",  "ALVES FONSECA", "M", "1987/11/05", "CPV", "ped", "alv"),
]

new_rows = []
next_id  = max_id + 1
next_doc = max_doc + 1

for first, last, gender, bday, nat, a, b in FAKE_PEOPLE:
    base = (a[:3] + b[:3])
    for _ in range(200):
        uname = base + str(random.randint(1000, 9999))
        if uname not in existing_users:
            break
    existing_users.add(uname)

    new_rows.append({
        "id": next_id, "username": uname, "password": make_password(),
        "status": "new", "account_type": "fake",
        "first_name": first, "last_name": last, "gender": gender,
        "birthdate": bday, "nationality": nat,
        "traveldoc": f"TF{next_doc:06d}",
        "email": "", "email_pass": "", "proxy": "", "proxy_idx": "",
        "registered_at": "", "last_login": "", "appointment_ref": "", "notes": "",
    })
    next_id  += 1
    next_doc += 1

with CSV.open("a", newline="", encoding="utf-8") as f:
    csv.DictWriter(f, fieldnames=FIELDNAMES).writerows(new_rows)

for r in new_rows:
    print(f"  id={r['id']}  {r['username']}  {r['traveldoc']}  "
          f"{r['first_name']} {r['last_name']}  pw={r['password']}")
print(f"\nAdded {len(new_rows)} fake accounts (status=new).")
