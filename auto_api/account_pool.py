"""
Account pool manager.
Loads accounts from accounts.csv, tracks status, and persists changes.

CSV columns:
  id             — row number (stable identifier)
  username       — site login username
  password       — site login password
  status         — lifecycle state (see STATUSES below)
  first_name     — person first name (for registration form)
  last_name      — person last name (for registration form)
  gender         — M / F (for registration form)
  birthdate      — YYYY/MM/DD (for registration form)
  nationality    — 3-letter ISO code e.g. PRT, BRA, CPV (for registration form)
  traveldoc      — passport/travel doc number (for registration form)
  email          — mail.tm address assigned during registration
  email_pass     — mail.tm account password (to poll inbox)
  proxy          — last proxy used (host:port, for traceability)
  proxy_idx      — Webshare numbered proxy index (1-9999) assigned at registration
  registered_at  — ISO timestamp when /register succeeded
  last_login     — ISO timestamp of last successful login
  appointment_ref — appointment reference returned by /ScheduleController
  account_type   — "fake" (test account) | "real" (production passport data)
  notes          — free-text, e.g. error messages or block reasons

Status lifecycle:
  new         → registration not yet attempted
  registered  → /register succeeded, email verification pending
  verified    → email verified, ready to login
  active      → logged in at least once (session alive or recently seen)
  applied     → appointment submitted successfully
  blocked     → site rejected account (secblock / rate-limit)
  failed      → repeated hard failures (wrong creds, server error, etc.)
"""

import csv
import random as _random
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

STATUSES = ("new", "registered", "verified", "active", "applied", "blocked", "failed")

_FIELDS = (
    "id", "username", "password", "status", "account_type",
    "first_name", "last_name", "gender", "birthdate", "nationality", "traveldoc",
    "email", "email_pass",
    "proxy", "proxy_idx",
    "registered_at", "last_login", "appointment_ref", "notes",
)

_WEBSHARE_PASS = "Saulo12345"
_WEBSHARE_HOST = "p.webshare.io"
_WEBSHARE_PORT = 80


def webshare_proxy(idx: int) -> str:
    """Return the full HTTP proxy URL for Webshare residential sub-user #{idx}."""
    return f"http://Mylist1234-DE-ES-FR-IT-US-{idx}:{_WEBSHARE_PASS}@{_WEBSHARE_HOST}:{_WEBSHARE_PORT}"

_DEFAULT_FILE = Path(__file__).parent / "data" / "accounts.csv"


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class AccountPool:
    def __init__(self, csv_file: str | Path = _DEFAULT_FILE):
        self._file    = Path(csv_file)
        self._accounts: list[dict] = []
        self._load()

    # ── Public API ─────────────────────────────────────────────────────────────

    def get(
        self,
        status: str | tuple[str, ...] = "verified",
        strategy: str = "sequential",
        exclude_usernames: list[str] | None = None,
    ) -> Optional[dict]:
        """
        Return one account matching status (string or tuple of statuses).
        strategy:
          sequential — lowest id first (default, deterministic)
          random     — random pick
        Returns a copy of the row dict, or None if no match found.
        """
        wanted   = (status,) if isinstance(status, str) else tuple(status)
        excluded = set(exclude_usernames or [])
        pool = [
            a for a in self._accounts
            if a["status"] in wanted and a["username"] not in excluded
        ]
        if not pool:
            return None
        if strategy == "random":
            return deepcopy(_random.choice(pool))
        return deepcopy(pool[0])  # sequential: lowest id

    def get_by_username(self, username: str) -> Optional[dict]:
        for a in self._accounts:
            if a["username"] == username:
                return deepcopy(a)
        return None

    def update(self, _key: str, **fields) -> None:
        """
        Update one or more fields for the account and persist to CSV.
        Common calls:
          pool.update(username, status="registered", email="x@y.z", proxy="1.2.3.4:6666")
          pool.update(username, status="blocked", notes="secblock on login")
          pool.update(username, status="applied",  appointment_ref="REF123")
          pool.update(old_username, username=new_username, ...)  # rename the username itself
        """
        for a in self._accounts:
            if a["username"] == _key:
                for k, v in fields.items():
                    if k in _FIELDS:
                        a[k] = v
                    else:
                        raise KeyError(f"Unknown field '{k}'. Valid: {_FIELDS}")
                self._save()
                return
        raise ValueError(f"Account '{_key}' not found")

    def mark_registered(self, username: str, email: str = "", email_pass: str = "",
                        proxy: str = "", proxy_idx: int | str = "") -> None:
        kw: dict = dict(status="registered", email=email, email_pass=email_pass,
                        proxy=proxy, registered_at=_now())
        if proxy_idx != "":
            kw["proxy_idx"] = str(proxy_idx)
        self.update(username, **kw)

    def mark_verified(self, username: str) -> None:
        self.update(username, status="verified")

    def mark_active(self, username: str, proxy: str = "") -> None:
        self.update(username, status="active",
                    last_login=_now(), **({"proxy": proxy} if proxy else {}))

    def mark_applied(self, username: str, appointment_ref: str = "") -> None:
        self.update(username, status="applied", appointment_ref=appointment_ref)

    def mark_blocked(self, username: str, notes: str = "") -> None:
        self.update(username, status="blocked",
                    **({"notes": notes} if notes else {}))

    def mark_failed(self, username: str, notes: str = "") -> None:
        self.update(username, status="failed",
                    **({"notes": notes} if notes else {}))

    def counts(self) -> dict[str, int]:
        """Return count of accounts per status."""
        result = {s: 0 for s in STATUSES}
        for a in self._accounts:
            result[a["status"]] = result.get(a["status"], 0) + 1
        return result

    def status(self) -> None:
        """Print a summary table of all accounts."""
        counts = self.counts()
        print(f"[accounts] file: {self._file}  total: {len(self._accounts)}")
        print(f"  {'Status':<12}  {'Count':>5}")
        print(f"  {'-'*12}  {'-'*5}")
        for s in STATUSES:
            print(f"  {s:<12}  {counts[s]:>5}")
        print()
        print(f"  {'ID':<4}  {'Username':<20}  {'Status':<12}  {'Proxy':<22}  {'Notes'}")
        print(f"  {'-'*4}  {'-'*20}  {'-'*12}  {'-'*22}  {'-'*20}")
        for a in self._accounts:
            proxy_short = a["proxy"].split("@")[-1] if "@" in a["proxy"] else a["proxy"]
            print(f"  {a['id']:<4}  {a['username']:<20}  {a['status']:<12}  "
                  f"{proxy_short:<22}  {a['notes'][:40]}")

    def all(self) -> list[dict]:
        return [deepcopy(a) for a in self._accounts]

    # ── Internals ──────────────────────────────────────────────────────────────

    def _load(self) -> None:
        if not self._file.exists():
            raise FileNotFoundError(f"Accounts CSV not found: {self._file}")
        with self._file.open(newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            self._accounts = [dict(row) for row in reader]
        print(f"[accounts] loaded {len(self._accounts)} accounts from {self._file.name}")

    def _save(self) -> None:
        with self._file.open("w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=list(_FIELDS))
            writer.writeheader()
            writer.writerows(self._accounts)


# ── Module-level convenience ───────────────────────────────────────────────────

_pool: AccountPool | None = None


def get_pool(csv_file: str | Path = _DEFAULT_FILE) -> AccountPool:
    global _pool
    if _pool is None or Path(csv_file) != _DEFAULT_FILE:
        _pool = AccountPool(csv_file)
    return _pool
