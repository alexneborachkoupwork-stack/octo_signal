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
import hashlib
import json
import re
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

BASE       = "https://pedidodevistos.mne.gov.pt"
LOGIN_URL  = BASE + "/VistosOnline/login"
AUTH_URL   = BASE + "/VistosOnline/Authentication.jsp"
WAF_VERIFY = BASE + "/ch/v"

HEADERS_XHR = {
    "Accept":           "*/*",
    "Content-Type":     "application/x-www-form-urlencoded; charset=UTF-8",
    "X-Requested-With": "XMLHttpRequest",
    "Origin":           BASE,
    "Referer":          AUTH_URL,
    "Sec-Fetch-Dest":   "empty",
    "Sec-Fetch-Mode":   "cors",
    "Sec-Fetch-Site":   "same-origin",
}

_ENV_FILE        = Path(__file__).parent / ".env"
_ACCOUNT_FILE    = Path(__file__).parent / "data" / "accounts.csv"
_ISP_PROXY_FILE      = Path(__file__).parent / "data" / "proxies_isp.txt"
_SOAX_PROXY_FILE     = Path(__file__).parent / "data" / "proxies_soax.txt"
_WEBSHARE_PROXY_FILE = Path(__file__).parent / "data" / "proxies_webshare.txt"


_csv_lock   = threading.Lock()
_print_lock = threading.Lock()


# ── WAF PoW bypass ────────────────────────────────────────────────────────────

def _is_waf_challenge(text: str) -> bool:
    return "/ch/bd.js" in text


def _parse_waf_challenge(html: str) -> dict | None:
    """Extract nonce/token/difficulty from the WAF challenge HTML."""
    m = re.search(
        r'"nonce"\s*:\s*"([a-f0-9]+)"[^}]*"token"\s*:\s*"([a-f0-9]+)"[^}]*"difficulty"\s*:\s*(\d+)',
        html, re.DOTALL,
    )
    if not m:
        m = re.search(
            r'"difficulty"\s*:\s*(\d+)[^}]*"token"\s*:\s*"([a-f0-9]+)"[^}]*"nonce"\s*:\s*"([a-f0-9]+)"',
            html, re.DOTALL,
        )
        if not m:
            return None
        return {"difficulty": int(m.group(1)), "token": m.group(2), "nonce": m.group(3)}
    return {"nonce": m.group(1), "token": m.group(2), "difficulty": int(m.group(3))}


def _solve_pow(nonce: str, token: str, difficulty: int) -> str:
    """Brute-force SHA256 PoW: find p s.t. sha256(nonce+token+p).startswith('0'*difficulty)."""
    prefix = "0" * difficulty
    p = 0
    base = (nonce + token).encode()
    while True:
        candidate = base + str(p).encode()
        if hashlib.sha256(candidate).hexdigest().startswith(prefix):
            return str(p)
        p += 1


def _waf_bypass(session, html: str, label: str) -> bool:
    """
    Parse WAF challenge from html, solve PoW, POST solution to /ch/v via the
    browser context (same connection as the challenge was received on).
    Returns True if server accepted the proof.
    """
    challenge = _parse_waf_challenge(html)
    if not challenge:
        print(f"[waf] {label}: could not parse challenge params")
        return False

    nonce, token, difficulty = challenge["nonce"], challenge["token"], challenge["difficulty"]
    print(f"[waf] {label}: solving PoW difficulty={difficulty} nonce={nonce[:8]}...")
    proof = _solve_pow(nonce, token, difficulty)
    print(f"[waf] {label}: proof={proof} — submitting via browser fetch")

    signals = {"eval_length": {"triggered": False, "confidence": 0},
               "product_sub": {"triggered": False, "confidence": 0}}

    waf_js = f"""
    async () => {{
        const r = await fetch('/ch/v', {{
            method: 'POST',
            headers: {{
                'Content-Type': 'application/json',
                'X-Requested-With': 'XMLHttpRequest',
                'Origin': '{BASE}',
            }},
            body: JSON.stringify({{
                nonce:   {json.dumps(nonce)},
                token:   {json.dumps(token)},
                proof:   {json.dumps(proof)},
                signals: {json.dumps(signals)},
            }}),
        }});
        return {{ status: r.status, ok: r.ok }};
    }}
    """
    try:
        result = session.browser_eval(waf_js, timeout=30)
        ok = result.get("ok", False)
        print(f"[waf] {label}: /ch/v status={result.get('status')}  ok={ok}")
        return ok
    except Exception as e:
        print(f"[waf] {label}: /ch/v browser_eval error: {e}")
        return False



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

# How many proxies to search to find one with a fresh exit IP.
_IP_SEARCH_LIMIT = 20


async def _login_account(acct: dict, pool,  # PersistentProxyPool
                          capsolver_keys: list[str], anticaptcha_keys: list[str],
                          twocaptcha_keys: list[str], capmonster_keys: list[str],
                          lang: str, executor: ThreadPoolExecutor,
                          stop_event: asyncio.Event,
                          headed: bool = False) -> str:
    """
    Login flow for one verified account. One attempt per account — no proxy retry.
    Returns: "active", "unverified", "error"
    stop_event: set when the proxy pool is exhausted — all accounts stop immediately.
    """
    username = acct["username"]
    password = acct["password"]
    loop = asyncio.get_event_loop()

    import session as sess
    import solver as solvermod

    if stop_event.is_set():
        _log(username, "pool exhausted — skipping")
        return "error"

    # Find one fresh proxy
    fresh = None
    for _ in range(_IP_SEARCH_LIMIT):
        candidate = pool.advance()
        ip = await loop.run_in_executor(
            executor, pool.verify_and_claim, candidate, username,
        )
        if ip is not None:
            fresh = (candidate, ip)
            break
        _log(username, f"IP already used or dead on {_proxy_host(candidate)} — skipping")

    if fresh is None:
        _log(username, "FAIL no fresh exit IPs — pool exhausted, stopping batch")
        stop_event.set()
        return "error"

    proxy, exit_ip = fresh

    # Step 1: browser WAF bypass — cookies land in primp client (same as registration)
    try:
        s = await loop.run_in_executor(
            executor, lambda _p=proxy: sess.get_session(_p, headless=not headed))
    except Exception as e:
        _log(username, f"FAIL get_session ({exit_ip}): {e}")
        return "error"

    # Step 2: full browser login — types credentials (builds reCAPTCHA behavioral signals),
    # clicks the checkbox, handles consent popup, and fires doLogin() AJAX from within
    # the real browser context.  Falls back to external CapSolver token if auto-solve fails.
    # This produces shorter, higher-quality tokens vs race_all() + browser_fetch.
    solver_key        = capsolver_keys[0]   if capsolver_keys   else ""
    twocaptcha_key    = twocaptcha_keys[0]  if twocaptcha_keys  else ""
    body = ""
    status = 0
    try:
        _log(username, f"browser_login: typing creds + clicking checkbox (exit_ip={exit_ip})")
        result = await loop.run_in_executor(
            executor,
            lambda: s.browser_login(username, password, lang=lang,
                                    solver_key=solver_key,
                                    twocaptcha_key=twocaptcha_key,
                                    timeout=360),
        )
        body   = (result.get("body") or "").strip()
        status = result.get("status", 0)
        _log(username, f"login status={status}  waf={_is_waf_challenge(body)}  resp={body[:200]}")
    except Exception as e:
        _log(username, f"FAIL browser_login: {e}")
        _csv_update(username, status="login_failed", notes=f"browser_login error: {e}")
        try: s.close()
        except Exception: pass
        return "error"

    # WAF PoW challenge — solve and retry login via browser_login (fresh CAPTCHA token)
    if _is_waf_challenge(body):
        _log(username, "WAF challenge received — solving PoW")
        bypassed = await loop.run_in_executor(
            executor,
            lambda: _waf_bypass(s, body, username),
        )
        if not bypassed:
            _log(username, "FAIL WAF bypass rejected")
            _csv_update(username, status="login_failed", notes="WAF PoW rejected")
            try: s.close()
            except Exception: pass
            return "error"
        # After bypass, get a fresh CAPTCHA token and POST via browser_fetch.
        # browser_fetch uses the browser's fetch() (with bypass cookie) — no consent popup needed.
        _log(username, "WAF bypassed — getting fresh CAPTCHA token for retry")
        retry_token = ""
        try:
            retry_token = await loop.run_in_executor(
                executor, solvermod.race_all,
                capsolver_keys, anticaptcha_keys, twocaptcha_keys, capmonster_keys,
                "LOGIN_EVISA", proxy,
            )
        except Exception as e:
            _log(username, f"WARN retry CAPTCHA race_all failed: {e} — retrying with empty token")

        login_data = {"username": username, "password": password,
                      "language": lang, "rgpd": "Y",
                      "captchaResponse": retry_token}
        try:
            result = await loop.run_in_executor(
                executor,
                lambda: s.browser_fetch(LOGIN_URL, data=login_data, timeout=60),
            )
            body   = (result.get("body") or "").strip()
            status = result.get("status", 0)
            _log(username, f"login retry status={status}  waf={_is_waf_challenge(body)}  resp={body[:200]}")
        except Exception as e:
            _log(username, f"FAIL login retry browser_fetch: {e}")
            _csv_update(username, status="login_failed", notes=f"retry browser_fetch error: {e}")
            try: s.close()
            except Exception: pass
            return "error"

    try: s.close()
    except Exception: pass

    if not body:
        _log(username, f"FAIL empty response body  status={status}")
        _csv_update(username, status="login_failed", notes=f"empty body status={status}")
        return "error"

    try:
        resp  = json.loads(body)
        rtype = resp.get("type", "__missing__")
    except json.JSONDecodeError:
        _log(username, f"FAIL non-JSON response (WAF/HTML?): {body[:120]}")
        _csv_update(username, status="login_failed", notes="non-JSON response")
        return "error"

    # Server returns {"type":""} or {"type":"200"} on success (200 OK, small JSON body).
    # Any 4xx or JSON without a "type" key is a failure.
    if status == 200 and rtype in ("", "200"):
        _log(username, f"LOGIN OK  exit_ip={exit_ip}")
        _csv_update(username, status="active", last_login=_now(),
                    proxy=_proxy_host(proxy))
        return "active"

    if rtype == "EmailSend":
        _log(username, "FAIL account not verified server-side (EmailSend)")
        _csv_update(username, status="registered", notes="EmailSend on login")
        return "unverified"

    _log(username, f"FAIL type={rtype!r}  exit_ip={exit_ip}")
    _csv_update(username, status="login_failed", notes=f"type={rtype}")
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

    sem        = asyncio.Semaphore(args.concurrency)
    executor   = ThreadPoolExecutor(max_workers=args.concurrency + 4)
    stop_event = asyncio.Event()   # set when proxy pool exhausted — all workers stop

    async def bounded(acct):
        if stop_event.is_set():
            return "skipped"
        async with sem:
            if stop_event.is_set():
                return "skipped"
            return await _login_account(acct, pool,
                                         capsolver_keys, anticaptcha_keys,
                                         twocaptcha_keys, capmonster_keys,
                                         args.lang, executor,
                                         stop_event,
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
