"""
probe_slots.py — test /slots endpoint with existing warmed sessions.

Loads saved sessions for any account that has a checkpoint=schedule_jsp,
then POSTs to /slots for multiple posto_id values and logs the raw response.
Helps diagnose whether {"data":{}} is genuine (no availability) or a
request/session/captcha issue.

Usage:
  uv run python -m app.core.probes.probe_slots [--postos 3088,2032,3059,5084] [--account fercos2508]
"""

import argparse
import asyncio
import json
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import app  # noqa: F401 — sys.path setup

_CORE = Path(__file__).parent.parent
_ENV  = _CORE / ".env"


def _load_env() -> dict:
    env = {}
    if not _ENV.exists():
        return env
    for line in _ENV.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        env[k.strip()] = v.strip()
    return env


async def probe_slots_for_account(username: str, postos: list[str], solver_keys: dict,
                                   executor: ThreadPoolExecutor) -> None:
    import primp
    import session as sess
    import session_store

    loop = asyncio.get_event_loop()

    meta = session_store.load_meta(username)
    if not meta:
        print(f"[{username}] no saved session — skipping")
        return
    cookies = meta.get("cookies")
    if not cookies:
        print(f"[{username}] no cookies in session — skipping")
        return
    proxy = meta.get("proxy") or ""

    print(f"[{username}] loading session (proxy={proxy[:40]}...)")
    client = primp.Client(
        impersonate=sess.IMPERSONATE,
        proxy=proxy,
        follow_redirects=True,
        verify=True,
    )
    client.set_cookies(session_store.COOKIES_URL, cookies)

    alive = await loop.run_in_executor(executor, session_store.is_alive, client)
    if not alive:
        print(f"[{username}] session dead — probe skipped (need a fresh run to re-warm)")
        return
    print(f"[{username}] session alive")

    from app.core import solver as solvermod

    for posto_id in postos:
        print(f"\n[{username}] --- POST /slots?posto_id={posto_id} ---")
        sched_jsp_url = f"https://pedidodevistos.mne.gov.pt/VistosOnline/Schedule.jsp?posto_id={posto_id}"
        SLOTS_URL = "https://pedidodevistos.mne.gov.pt/VistosOnline/slots"

        # Solve CAPTCHA
        try:
            token = await loop.run_in_executor(
                executor,
                lambda: solvermod.race_all(
                    solver_keys.get("capsolver", []),
                    solver_keys.get("anticaptcha", []),
                    solver_keys.get("twocaptcha", []),
                    solver_keys.get("capmonster", []),
                    "SCHEDULE_EVISA", proxy, min_score=50,
                ),
            )
            print(f"[{username}] captcha token len={len(token)} (score varies by solver)")
        except Exception as e:
            print(f"[{username}] captcha failed: {e}")
            continue

        # POST /slots (primp, no browser — probe only)
        try:
            r = await loop.run_in_executor(executor, lambda: client.post(
                SLOTS_URL,
                params={"posto_id": posto_id},
                data={"posto_id": posto_id, "captcha": token},
                headers={
                    **sess.HEADERS_XHR,
                    "Referer": sched_jsp_url,
                },
                timeout=30,
            ))
            print(f"[{username}] status={r.status_code}  body_len={len(r.text)}")
            body = r.text.strip()
            print(f"[{username}] raw: {body[:800]}")

            try:
                parsed = json.loads(body)
                slots_data = parsed.get("data") or {} if isinstance(parsed, dict) else parsed
                if isinstance(slots_data, dict) and slots_data:
                    print(f"[{username}] *** SLOTS FOUND at posto {posto_id}: {list(slots_data.keys())[:5]} ***")
                elif isinstance(parsed, list) and parsed:
                    print(f"[{username}] *** SLOTS FOUND (list) at posto {posto_id}: {len(parsed)} entries ***")
                else:
                    print(f"[{username}] no slots at posto {posto_id}")
            except Exception:
                print(f"[{username}] non-JSON response at posto {posto_id}")
        except Exception as e:
            print(f"[{username}] request failed: {e}")


async def main(args: argparse.Namespace, env: dict) -> None:
    def _keys(k: str) -> list[str]:
        return [x.strip() for x in env.get(k, "").split(",") if x.strip()]

    solver_keys = {
        "capsolver":   _keys("CAPSOLVER_KEYS"),
        "anticaptcha": _keys("ANTICAPTCHA_KEYS"),
        "twocaptcha":  _keys("TWOCAPTCHA_KEYS"),
        "capmonster":  _keys("CAPMONSTER_KEYS"),
    }

    postos = [p.strip() for p in args.postos.split(",") if p.strip()]

    import session_store
    if args.account:
        accounts = [args.account]
    else:
        # Use all accounts with saved sessions
        sess_dir = _CORE / "sessions"
        if not sess_dir.exists():
            print("No sessions directory — run a test first to create warmed sessions")
            return
        accounts = [f.stem for f in sess_dir.glob("*.json")]
        if not accounts:
            print("No saved sessions — run a test first to create warmed sessions")
            return

    print(f"Probing postos: {postos}")
    print(f"Accounts: {accounts}")
    print()

    executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="probe")
    for username in accounts:
        await probe_slots_for_account(username, postos, solver_keys, executor)
    executor.shutdown(wait=False)


if __name__ == "__main__":
    env = _load_env()

    parser = argparse.ArgumentParser(description="Probe /slots at multiple postos using saved sessions")
    parser.add_argument("--postos",  default="3088,2032,3059,5084",
                        help="Comma-separated posto IDs to probe (default: 3088,2032,3059,5084)")
    parser.add_argument("--account", default="",
                        help="Specific account username to use (default: all saved sessions)")
    args = parser.parse_args()

    import os
    for k, v in env.items():
        os.environ.setdefault(k, v)

    asyncio.run(main(args, env))
