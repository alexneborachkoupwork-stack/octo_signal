"""
Full end-to-end workflow for a single account: login → questionnaire → form → slot → PDF.

Usage:
  uv run python full_workflow.py --account <username>
  uv run python full_workflow.py --account <username> --posto 2032 --residence IRL
  uv run python full_workflow.py --account <username> --accounts-file data/test_accounts.csv --posto 2032 --residence IRL

Session strategy:
  1. Try to restore from data/sessions/<username>.json (Playwright storage_state).
  2. Check if restored session is alive via GET /VistosOnline/ (302 = expired).
  3. If expired or no saved state: run browser_login() with a fresh ISP proxy → save_state.
  4. Pass the live PlaywrightSession to apply_one().
  5. On success: PDF is saved to data/pdfs/<username>.pdf.
"""

import argparse
import asyncio
import csv
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)

sys.path.insert(0, str(Path(__file__).parent))

import session as sess
import solver as solvermod
from proxy_pool import PersistentProxyPool
from batch_login import _load_dotenv, _screen_proxy, _is_waf_challenge, _waf_bypass, _csv_update, _now, _proxy_host, HOME_URL
from batch_apply import apply_one

_DATA           = Path(__file__).parent / "data"
_DEFAULT_ACCT   = _DATA / "accounts.csv"
_ISP_PROXY_FILE = _DATA / "proxies_isp.txt"
_SESSIONS_DIR   = _DATA / "sessions"


def _load_account(username: str, accounts_file: Path) -> dict | None:
    if not accounts_file.exists():
        return None
    with open(accounts_file, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            if row["username"] == username:
                return row
    return None


def _is_session_alive(s: sess.PlaywrightSession) -> bool:
    """Probe HOME_URL without following redirects. 302 = expired, 200 = alive."""
    try:
        r = s.get(HOME_URL, headers=sess.HEADERS_NAV, follow_redirects=False, timeout=15)
        return r.status_code == 200
    except Exception:
        return False


def _do_login(acct: dict, env: dict, proxy: str | None,
              headless: bool = False) -> sess.PlaywrightSession | None:
    """Open a fresh browser session and log in. Returns session on success, None on failure."""
    def _keys(k): return [x.strip() for x in env.get(k, "").split(",") if x.strip()]

    s = sess.get_session(proxy, headless=headless)
    try:
        result = s.browser_login(
            acct["username"], acct["password"], lang="PT",
            capsolver_keys=_keys("CAPSOLVER_KEYS"),
            anticaptcha_keys=_keys("ANTICAPTCHA_KEYS"),
            twocaptcha_keys=_keys("TWOCAPTCHA_KEYS"),
            capmonster_keys=_keys("CAPMONSTER_KEYS"),
            timeout=360,
        )
    except Exception as e:
        print(f"[full] browser_login error: {e}")
        try: s.close()
        except Exception: pass
        return None

    import json
    body  = (result.get("body") or "").strip()
    try:
        rtype = json.loads(body).get("type", "?")
    except Exception:
        rtype = "non-json"

    if result.get("status") == 200 and rtype in ("", "200", "success"):
        return s

    print(f"[full] login FAILED  type={rtype!r}  body={body[:120]!r}")
    try: s.close()
    except Exception: pass
    return None


async def run_full(acct: dict, posto_id: str, nationality: str, residence: str,
                   env: dict, headless: bool) -> str:
    """Run the full workflow for one account. Returns apply_one result string."""
    username = acct["username"]
    loop = asyncio.get_event_loop()
    executor = ThreadPoolExecutor(max_workers=4)

    def _keys(k): return [x.strip() for x in env.get(k, "").split(",") if x.strip()]

    # ── Step 1: Restore or create session ──────────────────────────────────────
    state_path = _SESSIONS_DIR / f"{username}.json"
    s: sess.PlaywrightSession | None = None

    if state_path.exists():
        print(f"[full] restoring session from {state_path.name}")
        try:
            s = sess.get_session_from_state(state_path, headless=headless)
        except Exception as e:
            print(f"[full] restore failed: {e} — will re-login")
            s = None

        if s is not None:
            alive = await loop.run_in_executor(executor, _is_session_alive, s)
            if not alive:
                print(f"[full] session expired — closing and re-logging in")
                try: s.close()
                except Exception: pass
                s = None

    if s is None:
        print(f"[full] no valid session — finding clean proxy and logging in")
        pool = PersistentProxyPool(_ISP_PROXY_FILE)
        proxy = None
        for _ in range(len(pool._proxies)):
            candidate = pool.advance()
            if candidate is None:
                break
            if not _screen_proxy(candidate):
                continue
            ip = pool.verify_and_claim(candidate, username)
            if ip:
                proxy = candidate
                print(f"[full] proxy selected: exit_ip={ip}")
                break

        if not proxy:
            print(f"[full] proxy pool exhausted — cannot login")
            return "error"

        s = await loop.run_in_executor(executor, lambda: _do_login(acct, env, proxy, headless))
        if s is None:
            _csv_update(username, status="login_failed", notes="full_workflow login failed")
            return "login_failed"

        _csv_update(username, status="active", last_login=_now(), proxy=_proxy_host(proxy))

        # Save session state for future reuse
        try:
            _SESSIONS_DIR.mkdir(exist_ok=True)
            s.save_state(_SESSIONS_DIR / f"{username}.json")
        except Exception as se:
            print(f"[full] WARNING: save_state failed: {se}")

    # ── Step 2: Run apply workflow ──────────────────────────────────────────────
    print(f"[full] session live — starting apply workflow")
    result = await apply_one(
        acct, posto_id,
        None,  # no slot manager
        _keys("CAPSOLVER_KEYS"), _keys("ANTICAPTCHA_KEYS"),
        _keys("TWOCAPTCHA_KEYS"), _keys("CAPMONSTER_KEYS"),
        executor,
        nationality=nationality,
        residence=residence,
        client=s,
    )

    try:
        s.close()
    except Exception:
        pass

    return result


def main() -> None:
    env = _load_dotenv()

    parser = argparse.ArgumentParser(description="Full workflow: login → questionnaire → slot → PDF")
    parser.add_argument("--account", required=True, help="Account username")
    parser.add_argument("--posto", default="",
                        help="Consular post ID (default: driven by --mode)")
    parser.add_argument("--nationality", default="",
                        help="Passport nationality ISO code (default: driven by --mode)")
    parser.add_argument("--residence", default="",
                        help="Country of residence ISO code (default: driven by --mode)")
    parser.add_argument("--accounts-file", default="", dest="accounts_file",
                        help="Path to accounts CSV (default: driven by --mode)")
    parser.add_argument("--mode", default="",
                        help="Run mode: test or real (default: real; or MODE from .env)")
    parser.add_argument("--headless", action="store_true",
                        help="Run browser headless (default: headed/visible)")
    args = parser.parse_args()

    from mode_config import get_mode_cfg
    cfg = get_mode_cfg(env, args.mode)
    posto_id    = args.posto       or cfg["posto_id"]
    nationality = args.nationality or cfg["nationality"]
    residence   = args.residence   or cfg["residence"]
    acct_file   = Path(args.accounts_file) if args.accounts_file else Path(cfg["accounts_file"])

    acct = _load_account(args.account, acct_file)
    if not acct:
        print(f"[full] account '{args.account}' not found in {acct_file}")
        sys.exit(1)

    if acct.get("status", "") == "login_failed":
        print(f"[full] WARNING: account has status=login_failed — proceeding anyway")

    print(f"[full] account={args.account}  posto={posto_id}"
          f"  nationality={nationality}  residence={residence}")

    result = asyncio.run(run_full(
        acct, posto_id, nationality, residence, env,
        headless=args.headless,
    ))
    print(f"\n[full] RESULT: {result}")

    if result == "applied":
        pdf_path = _DATA / "pdfs" / f"{args.account}.pdf"
        if pdf_path.exists():
            print(f"[full] PDF: {pdf_path}")
        sys.exit(0)
    else:
        sys.exit(1)


if __name__ == "__main__":
    main()
