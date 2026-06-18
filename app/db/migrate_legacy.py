"""
One-time (but idempotent/re-runnable) importer: legacy CSV/TXT/JSON files -> SQLite.

Source files are NOT deleted or modified — they remain the fallback until a full
booking event has run successfully through the DB-backed path (Phase B+).

Usage:
    python -m app.db.migrate_legacy
"""
from __future__ import annotations

import csv
import json
import re
from pathlib import Path

import app  # noqa: F401 -- sys.path setup
from app.db.models import Account, Proxy, ProxyPool, SessionCheckpoint
from app.db.session import get_session

_CORE_DIR = Path(__file__).parent.parent / "core"
_DATA_DIR = _CORE_DIR / "data"
_SESSIONS_DIR = _CORE_DIR / "sessions"

_ACCOUNT_FILES = [
    (_DATA_DIR / "accounts.csv", "real"),
    (_DATA_DIR / "test_accounts.csv", "test"),
]

# (file, provider, geo_regex-or-None) — geo_regex captures the embedded geo code per line
_PROXY_FILES = [
    (_DATA_DIR / "proxies_soax.txt", "soax", re.compile(r"country-([a-z]{2})-", re.I)),
    (_DATA_DIR / "proxies_webshare.txt", "webshare", re.compile(r"Mylist1234-([A-Z]{2})-", re.I)),
    (_DATA_DIR / "proxies_isp.txt", "isp", None),  # no geo encoding -> single flat pool
]


def _import_accounts(session) -> dict[str, int]:
    counts = {"in": 0, "imported": 0, "skipped": 0}
    for path, account_type in _ACCOUNT_FILES:
        if not path.exists():
            continue
        with open(path, newline="", encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
        counts["in"] += len(rows)
        for row in rows:
            username = row.get("username", "").strip()
            if not username:
                continue
            existing = session.query(Account).filter_by(username=username).one_or_none()
            if existing is not None:
                counts["skipped"] += 1
                continue
            session.add(Account(
                legacy_csv_id=row.get("id") or None,
                username=username,
                password=row.get("password", ""),
                status=row.get("status", "new"),
                account_type=row.get("account_type", account_type),
                first_name=row.get("first_name") or None,
                last_name=row.get("last_name") or None,
                gender=row.get("gender") or None,
                birthdate=row.get("birthdate") or None,
                nationality=row.get("nationality") or None,
                traveldoc=row.get("traveldoc") or None,
                email=row.get("email") or None,
                email_pass=row.get("email_pass") or None,
                proxy_last_used=row.get("proxy") or None,
                proxy_idx=int(row["proxy_idx"]) if row.get("proxy_idx", "").strip().isdigit() else None,
                registered_at=row.get("registered_at") or None,
                last_login=row.get("last_login") or None,
                appointment_ref=row.get("appointment_ref") or None,
                notes=row.get("notes") or None,
            ))
            counts["imported"] += 1
        session.commit()
    return counts


def _load_proxy_state(proxy_file: Path) -> dict:
    state_file = proxy_file.with_name(proxy_file.name + ".proxy_state.json")
    if not state_file.exists():
        return {"cursor": 0, "ip_cache": {}}
    try:
        return json.loads(state_file.read_text(encoding="utf-8-sig"))
    except Exception:
        return {"cursor": 0, "ip_cache": {}}


def _import_proxies(session) -> dict[str, int]:
    counts = {"in": 0, "imported": 0, "skipped": 0, "pools_created": 0}
    for path, provider, geo_re in _PROXY_FILES:
        if not path.exists():
            continue
        lines = [ln.strip() for ln in path.read_text(encoding="utf-8-sig").splitlines() if ln.strip()]
        counts["in"] += len(lines)
        state = _load_proxy_state(path)
        ip_cache = state.get("ip_cache", {})
        cursor = state.get("cursor", 0)

        # Group lines by geo (or "default" for ISP / unmatched lines).
        by_geo: dict[str, list[str]] = {}
        for ln in lines:
            geo = "default"
            if geo_re is not None:
                m = geo_re.search(ln)
                if m:
                    geo = m.group(1).lower()
            by_geo.setdefault(geo, []).append(ln)

        for geo, geo_lines in by_geo.items():
            pool_name = f"{provider}-{geo}"
            pool = session.query(ProxyPool).filter_by(name=pool_name).one_or_none()
            if pool is None:
                pool = ProxyPool(name=pool_name, provider=provider, geo=geo, cursor=cursor if geo != "default" or len(by_geo) == 1 else 0)
                session.add(pool)
                session.flush()
                counts["pools_created"] += 1
            for conn_str in geo_lines:
                existing = session.query(Proxy).filter_by(connection_string=conn_str).one_or_none()
                if existing is not None:
                    counts["skipped"] += 1
                    continue
                session.add(Proxy(
                    proxy_pool_id=pool.id,
                    connection_string=conn_str,
                    exit_ip_cached=ip_cache.get(conn_str),
                    is_rotating=(provider == "webshare"),
                ))
                counts["imported"] += 1
        session.commit()
    return counts


def _import_session_checkpoints(session) -> dict[str, int]:
    counts = {"in": 0, "imported": 0, "skipped": 0}
    if not _SESSIONS_DIR.exists():
        return counts
    files = list(_SESSIONS_DIR.glob("*.json"))
    counts["in"] = len(files)
    for f in files:
        username = f.stem
        account = session.query(Account).filter_by(username=username).one_or_none()
        if account is None:
            counts["skipped"] += 1
            continue
        existing = session.query(SessionCheckpoint).filter_by(account_id=account.id).one_or_none()
        if existing is not None:
            counts["skipped"] += 1
            continue
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            counts["skipped"] += 1
            continue
        session.add(SessionCheckpoint(
            account_id=account.id,
            proxy=data.get("proxy"),
            cookies_json=json.dumps(data.get("cookies", {})),
            checkpoint=data.get("checkpoint", "login"),
            posto_id=data.get("posto_id"),
            posto_pdf=data.get("posto_pdf"),
            nationality=data.get("nationality"),
            residence=data.get("residence"),
        ))
        counts["imported"] += 1
    session.commit()
    return counts


def main() -> None:
    session = get_session()
    try:
        acct_counts = _import_accounts(session)
        proxy_counts = _import_proxies(session)
        sess_counts = _import_session_checkpoints(session)
    finally:
        session.close()

    print("=== Migration summary ===")
    print(f"accounts:            in={acct_counts['in']:5d}  imported={acct_counts['imported']:5d}  skipped={acct_counts['skipped']:5d}")
    print(f"proxies:             in={proxy_counts['in']:5d}  imported={proxy_counts['imported']:5d}  skipped={proxy_counts['skipped']:5d}  pools_created={proxy_counts['pools_created']}")
    print(f"session_checkpoints: in={sess_counts['in']:5d}  imported={sess_counts['imported']:5d}  skipped={sess_counts['skipped']:5d}")


if __name__ == "__main__":
    main()
