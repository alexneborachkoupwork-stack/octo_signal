"""
Batch login for verified/active accounts.

Reads all accounts with status=verified (or active if --include-active),
attempts login for each, and updates status to active on success.

Usage:
  python batch_login.py                         # all verified accounts, 10 concurrent
  python batch_login.py --count 20 --concurrency 5
  python batch_login.py --proxy-type soax       # use SOAX rotating proxies
  python batch_login.py --account paucun9244    # single account test
  python batch_login.py --dry-run
"""

import argparse
import asyncio
import json
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

BASE       = "https://pedidodevistos.mne.gov.pt"
LOGIN_URL  = BASE + "/VistosOnline/login"
AUTH_URL   = BASE + "/VistosOnline/Authentication.jsp"

_ENV_FILE        = Path(__file__).parent / ".env"
_ACCOUNT_FILE    = Path(__file__).parent / "data" / "accounts.csv"
_ISP_PROXY_FILE      = Path(__file__).parent / "data" / "proxies_isp.txt"
_SOAX_PROXY_FILE     = Path(__file__).parent / "data" / "proxies_soax.txt"
_WEBSHARE_PROXY_FILE = Path(__file__).parent / "data" / "proxies_webshare.txt"

MAX_PROXY_RETRIES = 15

_csv_lock   = threading.Lock()
_print_lock = threading.Lock()



def _log(account: str, msg: str) -> None:
    with _print_lock:
        print(f"[{account}] {msg}", flush=True)


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _load_dotenv() -> dict:
    env = {}
    if not _ENV_FILE.exists():
        return env
    for line in _ENV_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        env[k.strip()] = v.strip()
    return env


def _fix_proxy_url(raw: str) -> str:
    """URL-encode proxy password so that SOAX-style semicolons (wifi;pt;) don't break URL parsing."""
    if not raw.startswith(("http://", "https://", "socks5://")):
        raw = "http://" + raw
    try:
        from urllib.parse import urlparse, urlunparse, quote
        p = urlparse(raw)
        if p.password and any(c in p.password for c in (";", "+")):
            encoded = quote(p.password, safe="")
            netloc = f"{p.username}:{encoded}@{p.hostname}"
            if p.port:
                netloc += f":{p.port}"
            raw = urlunparse((p.scheme, netloc, p.path, p.params, p.query, p.fragment))
    except Exception:
        pass
    return raw


def _load_proxies_from_file(path: Path) -> list[str]:
    if not path.exists():
        return []
    lines = [l.strip() for l in path.read_text(encoding="utf-8").splitlines()
             if l.strip() and not l.startswith("#")
             and l.strip() != "username:password@server:port"]  # skip header
    return [_fix_proxy_url(l) for l in lines]


def _proxy_host(proxy: str) -> str:
    return proxy.split("@")[-1] if proxy and "@" in proxy else (proxy or "")


def _csv_update(username: str, **fields) -> None:
    with _csv_lock:
        from account_pool import AccountPool
        pool = AccountPool(_ACCOUNT_FILE)
        pool.update(username, **fields)


# ── Per-account login worker ───────────────────────────────────────────────────

# How many proxies to search per login attempt to find one with a fresh exit IP.
_IP_SEARCH_PER_ATTEMPT = 20


async def _login_account(acct: dict, pool,  # PersistentProxyPool
                          capsolver_keys: list[str], anticaptcha_keys: list[str],
                          twocaptcha_keys: list[str], capmonster_keys: list[str],
                          lang: str, executor: ThreadPoolExecutor,
                          headed: bool = False) -> str:
    """
    Login flow for one verified account.
    Uses PersistentProxyPool for cross-run deduplication via real exit IP.
    Returns: "active", "unverified", "error"
    """
    username = acct["username"]
    password = acct["password"]
    loop = asyncio.get_event_loop()

    import session as sess

    async def _next_fresh_proxy() -> tuple[str, str] | None:
        for _ in range(_IP_SEARCH_PER_ATTEMPT):
            candidate = pool.advance()
            ip = await loop.run_in_executor(
                executor, pool.verify_and_claim, candidate, username,
            )
            if ip is not None:
                return candidate, ip
            _log(username, f"IP already used or dead on {_proxy_host(candidate)} — skipping")
        return None

    for proxy_attempt in range(MAX_PROXY_RETRIES):
        fresh = await _next_fresh_proxy()
        if fresh is None:
            _log(username, "FAIL no fresh exit IPs within search budget")
            break
        proxy, exit_ip = fresh
        if proxy_attempt > 0:
            _log(username, f"retry proxy_attempt={proxy_attempt}  exit_ip={exit_ip}")

        # Get session — browser bypasses WAF challenge; cookies land in primp client
        try:
            s = await loop.run_in_executor(
                executor, lambda _p=proxy: sess.get_session(_p, headless=not headed))
        except Exception as e:
            _log(username, f"FAIL get_session ({exit_ip}): {e}")
            continue

        # Login via browser: types credentials for behavioral signals, clicks
        # reCAPTCHA checkbox, falls back to external CapSolver token if needed.
        solver_key = (capsolver_keys or twocaptcha_keys or capmonster_keys or [""])[0]
        try:
            lr = await loop.run_in_executor(
                executor, lambda _s=s: _s.browser_login(username, password, lang, solver_key))
            body = lr.get("body", "").strip()
            _log(username, f"login resp={body[:200]}")
        except Exception as e:
            _log(username, f"FAIL browser_login: {e}")
            try: s.close()
            except Exception: pass
            continue

        try:
            resp  = json.loads(body)
            rtype = resp.get("type", "")
        except json.JSONDecodeError:
            _log(username, f"FAIL non-JSON response (HTML?): {body[:120]}")
            try: s.close()
            except Exception: pass
            continue

        if rtype in ("", "200"):
            _log(username, f"LOGIN OK  exit_ip={exit_ip}")
            _csv_update(username, status="active", last_login=_now(),
                        proxy=_proxy_host(proxy))
            if not headed:
                try: s.close()
                except Exception: pass
            else:
                _log(username, "browser left open — close the window manually when done")
            return "active"

        try: s.close()
        except Exception: pass

        if rtype == "secblock":
            _log(username, f"secblock on {exit_ip} — burning, trying next")
            pool.burn_today(proxy, "login:secblock")
            continue

        if rtype == "ReCaptchaError":
            _log(username, f"CAPTCHA token rejected  exit_ip={exit_ip}")
            continue

        if rtype == "EmailSend":
            _log(username, "FAIL account not verified server-side (EmailSend)")
            _csv_update(username, status="registered", notes="EmailSend on login")
            return "unverified"

        _log(username, f"type={rtype!r} on {exit_ip} — retrying next proxy (no burn)")
        continue

    _log(username, f"FAIL exhausted {MAX_PROXY_RETRIES} login attempts")
    return "error"


# ── Main ───────────────────────────────────────────────────────────────────────

async def main_async(args: argparse.Namespace,
                     capsolver_keys: list[str], anticaptcha_keys: list[str],
                     twocaptcha_keys: list[str], capmonster_keys: list[str]) -> None:
    from account_pool import AccountPool
    from proxy_pool import PersistentProxyPool

    acc_pool = AccountPool(_ACCOUNT_FILE)

    # Build persistent proxy pool (cursor + used IPs persist across runs)
    try:
        if args.proxy_type == "soax":
            pool = PersistentProxyPool(_SOAX_PROXY_FILE)
        elif args.proxy_type == "isp":
            pool = PersistentProxyPool(_ISP_PROXY_FILE)
        elif args.proxy_type == "webshare":
            pool = PersistentProxyPool(_WEBSHARE_PROXY_FILE)
        else:
            print("[batch_login] ERROR: specify --proxy-type soax|isp|webshare")
            return
    except Exception as e:
        print(f"[batch_login] ERROR building proxy pool: {e}")
        return

    # Pick accounts
    if args.account:
        acct = acc_pool.get_by_username(args.account)
        if not acct:
            print(f"[batch_login] account '{args.account}' not found in CSV")
            return
        accounts = [acct]
    else:
        want_statuses = {"verified"}
        if args.include_active:
            want_statuses.add("active")
        accounts = [a for a in acc_pool.all() if a["status"] in want_statuses]
        if args.count > 0:
            accounts = accounts[:args.count]

    if not accounts:
        print("[batch_login] No accounts to login")
        return

    print(f"\n[batch_login] {len(accounts)} accounts  concurrency={args.concurrency}  "
          f"proxy-type={args.proxy_type}  pool: cursor={pool.cursor}  used={pool.used_count}/{pool.total}")

    if args.dry_run:
        print("\n[dry-run] Would login:")
        for acct in accounts:
            print(f"  {acct['username']}  status={acct['status']}")
        return

    sem      = asyncio.Semaphore(args.concurrency)
    executor = ThreadPoolExecutor(max_workers=args.concurrency + 4)

    async def bounded(acct):
        async with sem:
            return await _login_account(acct, pool,
                                         capsolver_keys, anticaptcha_keys,
                                         twocaptcha_keys, capmonster_keys,
                                         args.lang, executor,
                                         headed=args.headed)

    tasks   = [bounded(acct) for acct in accounts]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    counts: dict[str, int] = {}
    for r in results:
        k = str(r) if isinstance(r, Exception) else r
        counts[k] = counts.get(k, 0) + 1

    print("\n" + "="*50)
    print("[batch_login] SUMMARY")
    for k, v in sorted(counts.items()):
        print(f"  {k:<14}: {v}")
    print("="*50)

    if args.headed:
        try:
            input("\n[batch_login] Browser is open — press Enter to close and exit...")
        except (KeyboardInterrupt, EOFError):
            pass


def main() -> None:
    import sys
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    env = _load_dotenv()

    parser = argparse.ArgumentParser(description="Batch login for verified accounts")
    parser.add_argument("--account",  default="",
                        help="Login a single account by username")
    parser.add_argument("--count",    type=int, default=0,
                        help="Max accounts to login (0=all verified)")
    parser.add_argument("--concurrency", type=int, default=10,
                        help="Max simultaneous workers (default: 10)")
    parser.add_argument("--proxy-type", choices=("isp", "soax", "webshare", "stored"), default="stored",
                        help="isp=data/proxies_isp.txt, soax=data/proxies_soax.txt, webshare=data/proxies_webshare.txt, stored=per-account proxy from CSV")
    parser.add_argument("--lang", default=env.get("SITE_LANGUAGE", "PT"),
                        help="Language (default: PT)")
    parser.add_argument("--include-active", action="store_true",
                        help="Also re-login accounts already marked active")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print plan without executing")
    parser.add_argument("--headed", action="store_true",
                        help="Open a visible browser window (for manual inspection)")

    args = parser.parse_args()

    def _keys(k: str) -> list[str]:
        return [x.strip() for x in env.get(k, "").split(",") if x.strip()]

    capsolver_keys   = _keys("CAPSOLVER_KEYS")
    anticaptcha_keys = _keys("ANTICAPTCHA_KEYS")
    twocaptcha_keys  = _keys("TWOCAPTCHA_KEYS")
    capmonster_keys  = _keys("CAPMONSTER_KEYS")

    if not any([capsolver_keys, anticaptcha_keys, twocaptcha_keys, capmonster_keys]):
        print("ERROR: no CAPTCHA solver keys in .env")
        sys.exit(1)

    print(f"[batch_login] solvers: capsolver={len(capsolver_keys)} anticaptcha={len(anticaptcha_keys)} "
          f"2captcha={len(twocaptcha_keys)} capmonster={len(capmonster_keys)}")
    print(f"[batch_login] proxy-type: {args.proxy_type}")
    asyncio.run(main_async(args, capsolver_keys, anticaptcha_keys, twocaptcha_keys, capmonster_keys))


if __name__ == "__main__":
    main()
