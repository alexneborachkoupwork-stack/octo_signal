"""Fake Portuguese person data generator for E-VISA registration."""
import random
import string
import unicodedata
from faker import Faker

_fake = Faker("pt_PT")


def _ascii(s: str) -> str:
    return unicodedata.normalize("NFD", s).encode("ascii", "ignore").decode()
#_NATIONALITIES = ["BRA", "IND", "CHN", "MOZ", "ANG", "CPV", "PHL", "PAK", "MAR", "COL"]
_NATIONALITIES = ["BRA"]


def _gen_password(length: int = 16) -> str:
    """Min 14 chars, ≥1 special, ≥1 digit, ≥1 uppercase."""
    special = random.choice("!@#$%&*_+")
    digit   = random.choice(string.digits)
    upper   = random.choice(string.ascii_uppercase)
    filler  = random.choices(string.ascii_lowercase, k=length - 3)
    chars   = list(special + digit + upper + "".join(filler))
    random.shuffle(chars)
    return "".join(chars)


def generate_person() -> dict:
    gender = random.choice(["M", "F"])
    first  = _fake.first_name_male() if gender == "M" else _fake.first_name_female()
    last   = _fake.last_name()
    birth  = _fake.date_of_birth(minimum_age=20, maximum_age=60)
    uname  = (_ascii(first[:3]) + _ascii(last[:3]) + str(random.randint(10000000, 999999999))).lower()
    traveldoc = "".join(random.choices(string.digits, k=16))
    return {
        "name":        first,
        "surname":     last,
        "username":    uname,
        "password":    _gen_password(),
        "birth_date":  birth.strftime("%Y/%m/%d"),
        "gender":      gender,
        "nationality": random.choice(_NATIONALITIES),
        "traveldoc":   traveldoc,
    }
