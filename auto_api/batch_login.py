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

MAX_PROXY_RETRIES = 5

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

async def _login_account(acct: dict, all_proxies: list[str], base_proxy_idx: int,
                          proxy_pool,  # ProxyPool | None
                          capsolver_keys: list[str], anticaptcha_keys: list[str],
                          twocaptcha_keys: list[str], capmonster_keys: list[str],
                          lang: str, executor: ThreadPoolExecutor) -> str:
    """
    Login flow for one verified account.
    Returns: "active", "blocked", "error", "captcha_failed"
    """
    username = acct["username"]
    password = acct["password"]
    loop = asyncio.get_event_loop()

    import session as sess

    for proxy_attempt in range(MAX_PROXY_RETRIES):
        proxy = all_proxies[(base_proxy_idx + proxy_attempt) % len(all_proxies)]
        if proxy_attempt > 0:
            _log(username, f"retry proxy_attempt={proxy_attempt}  proxy={_proxy_host(proxy)}")

        # Get session (handles WAF challenge)
        try:
            s = await loop.run_in_executor(executor, sess.get_session, proxy)
        except Exception as e:
            _log(username, f"FAIL get_session ({_proxy_host(proxy)}): {e}")
            continue

        # Login via browser: Playwright clicks the reCAPTCHA checkbox on AUTH_URL,
        # gets a genuine Enterprise token, submits via jQuery $.ajax (doLogin).
        # External solver tokens score too low for the login Enterprise threshold.
        try:
            def _do_login(s=s):
                return s.browser_login(username, password, lang=lang, timeout=60)
            lr_dict = await loop.run_in_executor(executor, _do_login)
            body = lr_dict.get("body", "").strip()
            lr_status = lr_dict.get("status", 0)
            _log(username, f"login status={lr_status}  resp={body[:150]}")
        except Exception as e:
            _log(username, f"FAIL login POST: {e}")
            continue
        finally:
            # Close browser regardless of login outcome
            try: s.close()
            except Exception: pass

        try:
            resp  = json.loads(body)
            rtype = resp.get("type", "")
        except json.JSONDecodeError:
            rtype = ""

        if rtype in ("", "200"):
            _log(username, f"LOGIN OK  proxy={_proxy_host(proxy)}")
            _csv_update(username, status="active", last_login=_now(),
                        proxy=_proxy_host(proxy))
            return "active"

        if rtype == "secblock":
            _log(username, f"secblock on {_proxy_host(proxy)} — trying next")
            if proxy_pool and proxy:
                proxy_pool.burn_today(proxy, "login:secblock")
            continue

        if rtype == "ReCaptchaError":
            _log(username, "CAPTCHA token rejected by server")
            continue

        if rtype == "EmailSend":
            _log(username, "FAIL account not verified server-side (EmailSend)")
            _csv_update(username, status="registered", notes="EmailSend on login")
            return "unverified"

        # type=error — could be wrong creds or rate-limited proxy
        _log(username, f"type=error on {_proxy_host(proxy)} — burning, trying next")
        if proxy_pool and proxy:
            proxy_pool.burn_today(proxy, "login:type=error")
        continue

    _log(username, f"FAIL exhausted {MAX_PROXY_RETRIES} proxy attempts")
    return "error"


# ── Main ───────────────────────────────────────────────────────────────────────

async def main_async(args: argparse.Namespace,
                     capsolver_keys: list[str], anticaptcha_keys: list[str],
                     twocaptcha_keys: list[str], capmonster_keys: list[str]) -> None:
    from account_pool import AccountPool
    from proxy_pool import ProxyPool

    acc_pool = AccountPool(_ACCOUNT_FILE)

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

    print(f"\n[batch_login] {len(accounts)} accounts  concurrency={args.concurrency}  proxy-type={args.proxy_type}")

    # Build proxy list
    proxy_pool_obj = None
    if False:  # placeholder — "none" proxy type removed (datacenter IP blocked by target)
        pass
    elif args.proxy_type == "isp":
        all_proxies = _load_proxies_from_file(_ISP_PROXY_FILE)
        if not all_proxies:
            print("[batch_login] ERROR no ISP proxies loaded")
            return
        try:
            proxy_pool_obj = ProxyPool(_ISP_PROXY_FILE)
        except Exception:
            pass
    elif args.proxy_type == "soax":
        all_proxies = _load_proxies_from_file(_SOAX_PROXY_FILE)
        if not all_proxies:
            print("[batch_login] ERROR no SOAX proxies loaded")
            return
        try:
            proxy_pool_obj = ProxyPool(_SOAX_PROXY_FILE)
        except Exception:
            pass
    elif args.proxy_type == "webshare":
        all_proxies = _load_proxies_from_file(_WEBSHARE_PROXY_FILE)
        if not all_proxies:
            print("[batch_login] ERROR no Webshare proxies loaded from data/proxies_webshare.txt")
            return
        try:
            proxy_pool_obj = ProxyPool(_WEBSHARE_PROXY_FILE)
        except Exception:
            pass
    else:
        # Use the proxy stored in accounts.csv for each account (registration proxy)
        all_proxies = []
        for a in accounts:
            p = a.get("proxy", "")
            if p and ":" in p:
                full = f"http://WHnPcVTJPqBz:le6VhGXHYF0UPR@{p}" if "@" not in p else p
                all_proxies.append(full)
        if not all_proxies:
            print("[batch_login] ERROR no stored proxies found in accounts.csv — specify --proxy-type")
            return
        print(f"[batch_login] using stored per-account proxies ({len(all_proxies)} found)")

    print(f"[batch_login] proxy pool: {len(all_proxies)} proxies")

    if args.dry_run:
        print("\n[dry-run] Would login:")
        for i, acct in enumerate(accounts):
            proxy = all_proxies[i % len(all_proxies)]
            print(f"  {acct['username']}  status={acct['status']}  proxy={_proxy_host(proxy)}")
        return

    sem      = asyncio.Semaphore(args.concurrency)
    executor = ThreadPoolExecutor(max_workers=args.concurrency + 4)

    async def bounded(acct, base_idx):
        async with sem:
            return await _login_account(acct, all_proxies, base_idx, proxy_pool_obj,
                                         capsolver_keys, anticaptcha_keys,
                                         twocaptcha_keys, capmonster_keys,
                                         args.lang, executor)

    tasks   = [bounded(acct, i % len(all_proxies)) for i, acct in enumerate(accounts)]
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
