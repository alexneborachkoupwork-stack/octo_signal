"""
Batch account registration — registers and verifies N accounts concurrently.

Each worker: create mail.tm → get session → solve CAPTCHA → POST /register
             → poll email (async) → GET token pages → POST VerificarEmail → mark verified

Uses asyncio + ThreadPoolExecutor for blocking primp/CapSolver calls so that
hundreds of coroutines can wait for emails without blocking each other.

Usage:
  python batch_register.py                                     # 10 concurrent, all new accounts
  python batch_register.py --count 20 --concurrency 10
  python batch_register.py --proxy-type isp                    # use proxies_isp.txt round-robin
  python batch_register.py --proxy-type webshare --ws-start 1  # Webshare numbered, starting at #1
  python batch_register.py --dry-run                           # show plan without running
"""

import argparse
import asyncio
import json
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

BASE             = "https://pedidodevistos.mne.gov.pt"
REGISTER_URL     = BASE + "/VistosOnline/register"
AUTH_URL         = BASE + "/VistosOnline/Authentication.jsp"
VERIFY_URL       = BASE + "/VistosOnline/VerificarEmail"
INSERT_TOKEN_JSP = BASE + "/VistosOnline/partials/insertToken.jsp"

_ENV_FILE        = Path(__file__).parent / ".env"
_ACCOUNT_FILE    = Path(__file__).parent / "data" / "accounts.csv"
_ISP_PROXY_FILE       = Path(__file__).parent / "data" / "proxies_isp.txt"
_SOAX_PROXY_FILE      = Path(__file__).parent / "data" / "proxies_soax.txt"
_WEBSHARE_PROXY_FILE  = Path(__file__).parent / "data" / "proxies_webshare.txt"

MAX_PROXY_RETRIES = 5  # max different proxies tried per account before marking failed

_csv_lock = threading.Lock()
_print_lock = threading.Lock()


def _log(account: str, msg: str) -> None:
    with _print_lock:
        print(f"[{account}] {msg}", flush=True)


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


# ── Per-account registration worker ───────────────────────────────────────────

async def _register_one(acct: dict, all_proxies: list[str], base_proxy_idx: int,
                         proxy_pool,  # ProxyPool | None — for burn_today()
                         capsolver_keys: list[str], anticaptcha_keys: list[str],
                         twocaptcha_keys: list[str], capmonster_keys: list[str],
                         lang: str, email_provider: str,
                         executor: ThreadPoolExecutor) -> str:
    """
    Full register+verify flow for one account.
    On type=error from the server (proxy burned), rotates to the next proxy and retries.
    Returns final status: "verified", "registered", "failed", "exists".
    """
    username = acct["username"]
    loop = asyncio.get_event_loop()
    import session as sess
    import solver as solvermod

    # Step 1 — create email inbox (mail.tm or Cloudflare)
    try:
        if email_provider == "cloudflare":
            from email_pool import load_cf_mail_from_env
            mail = load_cf_mail_from_env()
            email_addr, email_jwt = await loop.run_in_executor(executor, mail.create)
            email_pass = ""
        else:
            from email_pool import MailTM
            mail = MailTM()
            email_addr, email_pass = await loop.run_in_executor(executor, mail.create)
            email_jwt = await loop.run_in_executor(executor, mail.token, email_addr, email_pass)
        _log(username, f"email={email_addr} provider={email_provider}")
    except Exception as e:
        _log(username, f"FAIL create email: {e}")
        return "failed"

    reg_token_raw = ""
    used_proxy = all_proxies[base_proxy_idx % len(all_proxies)] if all_proxies else ""
    used_proxy_idx = base_proxy_idx

    # ── Proxy retry loop ──────────────────────────────────────────────────────
    for proxy_attempt in range(MAX_PROXY_RETRIES):
        proxy = all_proxies[(base_proxy_idx + proxy_attempt) % len(all_proxies)]
        if proxy_attempt > 0:
            _log(username, f"retry proxy_attempt={proxy_attempt} proxy={_proxy_host(proxy)}")

        # Step 2 — fresh session per proxy attempt
        try:
            s = await loop.run_in_executor(executor, sess.get_session, proxy)
        except Exception as e:
            _log(username, f"FAIL get_session ({_proxy_host(proxy)}): {e}")
            continue

        # Step 3 — race all CAPTCHA solvers in parallel (first token wins)
        token = None
        try:
            _log(username, f"CAPTCHA race-all (proxy={_proxy_host(proxy)})")
            token = await loop.run_in_executor(
                executor, solvermod.race_all,
                capsolver_keys, anticaptcha_keys, twocaptcha_keys, capmonster_keys,
                "REGISTER_EVISA", proxy,
            )
        except Exception as e:
            _log(username, f"CAPTCHA race-all failed: {e}")

        if not token:
            _log(username, "FAIL all CAPTCHA attempts failed — skipping proxy")
            continue

        # Step 4 — POST /register
        payload = {
            "name":            acct.get("first_name", ""),
            "surname":         acct.get("last_name", ""),
            "username":        username,
            "password":        acct["password"],
            "valPassword":     acct["password"],
            "gender":          acct.get("gender", "M"),
            "bday":            acct.get("birthdate", "1985/01/01"),
            "nationality":     acct.get("nationality", "PRT"),
            "traveldoc":       acct.get("traveldoc", ""),
            "email":           email_addr,
            "language":        lang,
            "rgpd":            "Y",
            "captchaResponse": token,
        }
        try:
            def _do_register(s=s, payload=payload):
                return s.post(REGISTER_URL, data=payload, headers=sess.HEADERS_XHR, timeout=90)
            reg_resp = await loop.run_in_executor(executor, _do_register)
            reg_body = reg_resp.text.strip()
            _log(username, f"register status={reg_resp.status_code} body={reg_body[:150]}")
        except Exception as e:
            # Timeout means the server likely processed the registration but
            # didn't respond in time. Poll email rather than retrying (retrying
            # would burn the proxy with additional CAPTCHA POSTs).
            _log(username, f"register POST exception ({type(e).__name__}) — may be registered; polling email")
            reg_token_raw = ""
            used_proxy = proxy
            used_proxy_idx = base_proxy_idx + proxy_attempt
            break


        try:
            reg_result = json.loads(reg_body)
            reg_type   = reg_result.get("type", "")
            reg_desc   = reg_result.get("description", "")
        except json.JSONDecodeError:
            reg_type = ""
            reg_desc = reg_body[:80]

        if reg_type == "secblock":
            _log(username, f"BLOCKED secblock on {_proxy_host(proxy)} — trying next")
            if proxy_pool:
                proxy_pool.burn_today(proxy, "secblock")
            try: s.close()
            except Exception: pass
            continue

        if reg_type == "ReCaptchaError":
            _log(username, "FAIL CAPTCHA rejected by server (low score)")
            try: s.close()
            except Exception: pass
            continue

        if reg_type == "error":
            _log(username, f"proxy {_proxy_host(proxy)} burned (type=error) — trying next")
            if proxy_pool:
                proxy_pool.burn_today(proxy, "register:type=error")
            try: s.close()
            except Exception: pass
            continue

        if "already_exists" in reg_type or "already_exists" in reg_desc.lower():
            _log(username, "INFO account already on server — marking registered")
            _csv_update(username, status="registered", email=email_addr,
                        email_pass=email_pass, proxy=_proxy_host(proxy),
                        proxy_idx=base_proxy_idx + proxy_attempt)
            return "exists"

        if reg_type == "200":
            reg_token_raw = reg_result.get("description", "")
            used_proxy = proxy
            used_proxy_idx = base_proxy_idx + proxy_attempt
            _log(username, f"register SUCCESS  reg_token={reg_token_raw}  proxy={_proxy_host(proxy)}")
            _csv_update(username, status="registered", email=email_addr,
                        email_pass=email_pass, proxy=_proxy_host(proxy),
                        proxy_idx=used_proxy_idx)
            break  # proceed to verification

        # 503 = server-side LB error; registration may have been processed
        if reg_resp.status_code == 503:
            _log(username, f"503 from server — registration may have been processed; polling email")
            reg_token_raw = ""
            used_proxy = proxy
            used_proxy_idx = base_proxy_idx + proxy_attempt
            _csv_update(username, status="registered", email=email_addr,
                        email_pass=email_pass, proxy=_proxy_host(proxy),
                        proxy_idx=used_proxy_idx)
            break

        # 403 = WAF hard block; proxy is now burned
        if reg_resp.status_code == 403:
            _log(username, f"403 WAF on {_proxy_host(proxy)} — proxy burned, stopping")
            if proxy_pool:
                proxy_pool.burn_today(proxy, "register:403waf")
            try: s.close()
            except Exception: pass
            return "failed"

        # Other unexpected response — treat as transient failure and try next proxy
        _log(username, f"WARN unexpected reg response (HTTP {reg_resp.status_code}): {reg_desc[:80]}")
        try: s.close()
        except Exception: pass
        continue

    else:
        _log(username, f"FAIL exhausted {MAX_PROXY_RETRIES} proxy attempts")
        try:
            s.close()
        except Exception:
            pass
        _csv_update(username, status="failed",
                    notes=f"exhausted {MAX_PROXY_RETRIES} proxy attempts")
        return "failed"

    # ── Verification — browser stays alive until verify is done ─────────────────
    # The browser session (Playwright) is needed for email verification because
    # the server ties the challenge-bypass session to the browser TLS fingerprint.
    # primp alone would receive the bot challenge page on the token URL.
    tokens = await mail.async_poll_for_tokens(email_jwt, timeout=300, interval=5)
    if not tokens:
        _log(username, "WARN no email tokens received — staying registered")
        try: s.close()
        except Exception: pass
        return "registered"

    link_token = tokens.get("link") or ""
    code_token = tokens.get("code") or ""
    # Strip quoted-printable "3D" prefix if CF worker extracted it verbatim from email
    if link_token.upper().startswith("3D") and len(link_token) == 34:
        link_token = link_token[2:]
    # reg_token_raw (from API response) is always canonical; email link is fallback
    token_search = reg_token_raw or link_token
    token_input  = code_token or (
        f"{token_search[0:8]}-{token_search[8:12]}-{token_search[12:16]}-"
        f"{token_search[16:20]}-{token_search[20:]}" if len(token_search) == 32 else token_search
    )
    _log(username, f"email tokens: link={link_token}  code={code_token}")

    token_page = f"{AUTH_URL}?token={token_search}"
    try:
        # Use the browser for verification (it has the correct session/TLS context)
        def _do_verify(s=s):
            return s.verify_email(
                token_url=token_page,
                insert_jsp=INSERT_TOKEN_JSP,
                verify_url=VERIFY_URL,
                token_input=token_input,
                token_search=token_search,
                lang=lang,
                timeout=90,
            )
        vr_dict = await loop.run_in_executor(executor, _do_verify)
        vr_status = vr_dict.get("status", 0)
        vr_body   = vr_dict.get("body", "").strip()
        _log(username, f"verify status={vr_status}  body={vr_body[:150]}")
    except Exception as e:
        _log(username, f"FAIL verify request: {e}")
        try: s.close()
        except Exception: pass
        return "registered"
    finally:
        # Browser can be closed now — verification is complete
        try: s.close()
        except Exception: pass

    verified = False
    if vr_status == 200:
        try:
            vres  = json.loads(vr_body)
            if vres.get("type", "") != "error":
                verified = True
                _log(username, f"VERIFIED: {vres.get('description', '')}")
            else:
                _log(username, f"WARN verify type=error: {vres.get('description', '')}")
        except json.JSONDecodeError:
            # Server returned HTML (redirect to main page after successful verify)
            if "/ch/bd.js" not in vr_body:
                verified = True
                _log(username, "VERIFIED (HTML redirect response — no challenge)")

    if not verified:
        _log(username, "WARN verify returned unexpected response — staying registered")
        return "registered"

    _csv_update(username, status="verified")
    return "verified"


async def _verify_one(acct: dict, all_proxies: list[str], base_proxy_idx: int,
                      lang: str, executor: ThreadPoolExecutor) -> str:
    """
    Email-verify an already-registered account whose inbox may still hold the token.
    Polls the CF Worker (or mail.tm) once, then uses a browser session to submit.
    Returns "verified" or "registered" (unchanged on any failure).
    """
    username   = acct["username"]
    email      = acct.get("email", "")
    email_pass = acct.get("email_pass", "")
    loop       = asyncio.get_event_loop()
    import session as sess

    if not email:
        _log(username, "SKIP no email stored — cannot verify")
        return "registered"

    # ── Poll inbox for tokens ──────────────────────────────────────────────────
    if email_pass:
        from email_pool import MailTM
        mail = MailTM()
        try:
            email_jwt = await loop.run_in_executor(executor, mail.token, email, email_pass)
        except Exception as e:
            _log(username, f"FAIL mail.tm re-auth: {e}")
            return "registered"
        tokens = await mail.async_poll_for_tokens(email_jwt, timeout=60, interval=5)
    else:
        from email_pool import load_cf_mail_from_env
        cf = load_cf_mail_from_env()
        tokens = await cf.async_poll_for_tokens(email, timeout=60, interval=5)

    if not tokens or (not tokens.get("link") and not tokens.get("code")):
        _log(username, "WARN no tokens in inbox — cannot verify")
        return "registered"

    link_token = tokens.get("link") or ""
    code_token = tokens.get("code") or ""
    if link_token.upper().startswith("3D") and len(link_token) == 34:
        link_token = link_token[2:]
    token_search = link_token
    token_input  = code_token or (
        f"{token_search[0:8]}-{token_search[8:12]}-{token_search[12:16]}-"
        f"{token_search[16:20]}-{token_search[20:]}" if len(token_search) == 32 else token_search
    )
    _log(username, f"tokens: link={link_token}  code={code_token}")

    # ── Browser verify ─────────────────────────────────────────────────────────
    token_page = f"{AUTH_URL}?token={token_search}"
    proxy = all_proxies[base_proxy_idx % len(all_proxies)] if all_proxies else ""
    try:
        s = await loop.run_in_executor(executor, sess.get_session, proxy)
    except Exception as e:
        _log(username, f"FAIL get_session ({_proxy_host(proxy)}): {e}")
        return "registered"

    try:
        def _do_verify(s=s):
            return s.verify_email(
                token_url=token_page,
                insert_jsp=INSERT_TOKEN_JSP,
                verify_url=VERIFY_URL,
                token_input=token_input,
                token_search=token_search,
                lang=lang,
                timeout=90,
            )
        vr_dict   = await loop.run_in_executor(executor, _do_verify)
        vr_status = vr_dict.get("status", 0)
        vr_body   = vr_dict.get("body", "").strip()
        _log(username, f"verify status={vr_status}  body={vr_body[:150]}")
    except Exception as e:
        _log(username, f"FAIL verify: {e}")
        return "registered"
    finally:
        try: s.close()
        except Exception: pass

    verified = False
    if vr_status == 200:
        try:
            vres = json.loads(vr_body)
            if vres.get("type", "") != "error":
                verified = True
                _log(username, f"VERIFIED: {vres.get('description', '')}")
            else:
                _log(username, f"WARN verify type=error: {vres.get('description', '')}")
        except json.JSONDecodeError:
            if "/ch/bd.js" not in vr_body:
                verified = True
                _log(username, "VERIFIED (HTML redirect — no challenge)")

    if not verified:
        _log(username, "WARN verify returned unexpected response — staying registered")
        return "registered"

    _csv_update(username, status="verified")
    return "verified"


def _proxy_host(proxy: str) -> str:
    return proxy.split("@")[-1] if proxy and "@" in proxy else (proxy or "")


def _now() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _csv_update(username: str, **fields) -> None:
    with _csv_lock:
        from account_pool import AccountPool
        pool = AccountPool(_ACCOUNT_FILE)
        pool.update(username, **fields)


def _build_proxy_list(args: "argparse.Namespace") -> "list[str] | None":
    """Build proxy list from args. Returns None and prints error on failure."""
    from account_pool import webshare_proxy
    if getattr(args, "proxy", None):
        return [_fix_proxy_url(args.proxy)]
    if args.proxy_type == "isp":
        proxies = _load_proxies_from_file(_ISP_PROXY_FILE)
        if not proxies:
            print("[batch] ERROR no ISP proxies loaded from proxies_isp.txt")
            return None
        return proxies
    if args.proxy_type == "soax":
        proxies = _load_proxies_from_file(_SOAX_PROXY_FILE)
        if not proxies:
            print("[batch] ERROR no SOAX proxies loaded from proxies_soax.txt")
            return None
        return proxies
    if args.proxy_type == "webshare":
        if _WEBSHARE_PROXY_FILE.exists():
            return _load_proxies_from_file(_WEBSHARE_PROXY_FILE)
        return [webshare_proxy(args.ws_start + i) for i in range(200)]
    return [webshare_proxy(args.ws_start + i) for i in range(200)]


# ── Main ───────────────────────────────────────────────────────────────────────

async def main_async(args: argparse.Namespace,
                     capsolver_keys: list[str], anticaptcha_keys: list[str],
                     twocaptcha_keys: list[str], capmonster_keys: list[str]) -> None:
    from account_pool import AccountPool, webshare_proxy
    from proxy_pool import ProxyPool

    acc_pool = AccountPool(_ACCOUNT_FILE)

    # ── verify-registered mode ─────────────────────────────────────────────────
    if getattr(args, "verify_registered", False):
        candidates = [a for a in acc_pool.all()
                      if a["status"] == "registered" and a.get("email")]
        if getattr(args, "account_type", None):
            candidates = [a for a in candidates if a.get("account_type") == args.account_type]
        accounts = candidates[:args.count] if args.count > 0 else candidates
        if not accounts:
            print("[batch] No registered accounts with email to verify")
            return
        print(f"\n[batch] verify-registered: {len(accounts)} accounts  "
              f"concurrency={args.concurrency}  proxy-type={args.proxy_type}")
        all_proxies = _build_proxy_list(args)
        if all_proxies is None:
            return
        print(f"[batch] proxy pool size: {len(all_proxies)}")
        if args.dry_run:
            for i, a in enumerate(accounts):
                print(f"  {a['username']}  email={a['email']}  proxy={_proxy_host(all_proxies[i % len(all_proxies)])}")
            return
        sem      = asyncio.Semaphore(args.concurrency)
        executor = ThreadPoolExecutor(max_workers=args.concurrency + 4)
        async def bounded_verify(acct, idx):
            async with sem:
                return await _verify_one(acct, all_proxies, idx, args.lang, executor)
        tasks   = [bounded_verify(a, i % len(all_proxies)) for i, a in enumerate(accounts)]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        counts: dict[str, int] = {}
        for r in results:
            k = str(r) if isinstance(r, Exception) else r
            counts[k] = counts.get(k, 0) + 1
        print("\n" + "="*50)
        print("[batch] VERIFY-REGISTERED SUMMARY")
        for k, v in sorted(counts.items()):
            print(f"  {k:<12}: {v}")
        print("="*50)
        return

    # Collect processable accounts
    want_statuses = {"new", "failed"} if args.include_failed else {"new"}
    all_new = [a for a in acc_pool.all() if a["status"] in want_statuses]

    # Optional account_type filter (fake / real)
    if getattr(args, "account_type", None):
        all_new = [a for a in all_new if a.get("account_type") == args.account_type]

    if not all_new:
        print("[batch] No accounts to process (check status/account_type filters)")
        return

    accounts = all_new[:args.count] if args.count > 0 else all_new
    print(f"\n[batch] {len(accounts)} accounts to process  concurrency={args.concurrency}  proxy-type={args.proxy_type}")

    # Build ordered proxy list
    proxy_pool_obj = None
    if getattr(args, "proxy", None):
        all_proxies = [_fix_proxy_url(args.proxy)]
        print(f"[batch] forced proxy: {_proxy_host(all_proxies[0])}")
    else:
        all_proxies = _build_proxy_list(args)
        if all_proxies is None:
            return
        try:
            if args.proxy_type == "isp":
                proxy_pool_obj = ProxyPool(_ISP_PROXY_FILE)
            elif args.proxy_type == "soax":
                proxy_pool_obj = ProxyPool(_SOAX_PROXY_FILE)
            elif args.proxy_type == "webshare" and _WEBSHARE_PROXY_FILE.exists():
                proxy_pool_obj = ProxyPool(_WEBSHARE_PROXY_FILE)
        except Exception:
            pass

    print(f"[batch] proxy pool size: {len(all_proxies)}  (each account gets up to {MAX_PROXY_RETRIES} retries)")

    if args.dry_run:
        print("\n[dry-run] Would process:")
        for i, acct in enumerate(accounts):
            proxy = all_proxies[i % len(all_proxies)]
            print(f"  {acct['username']}  base_proxy={_proxy_host(proxy)}  (retries up to {MAX_PROXY_RETRIES-1} more)")
        return

    sem = asyncio.Semaphore(args.concurrency)
    executor = ThreadPoolExecutor(max_workers=args.concurrency + 4)

    async def bounded(acct, base_idx):
        async with sem:
            return await _register_one(acct, all_proxies, base_idx, proxy_pool_obj,
                                        capsolver_keys, anticaptcha_keys,
                                        twocaptcha_keys, capmonster_keys,
                                        args.lang, args.email, executor)

    tasks = [bounded(acct, i % len(all_proxies)) for i, acct in enumerate(accounts)]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    # Summary
    counts: dict[str, int] = {}
    for r in results:
        k = str(r) if isinstance(r, Exception) else r
        counts[k] = counts.get(k, 0) + 1

    print("\n" + "="*50)
    print("[batch] SUMMARY")
    for k, v in sorted(counts.items()):
        print(f"  {k:<12}: {v}")
    print("="*50)


def main() -> None:
    import sys
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    env = _load_dotenv()

    parser = argparse.ArgumentParser(description="Batch account registration")
    parser.add_argument("--count", type=int, default=0,
                        help="Max accounts to register (0=all new)")
    parser.add_argument("--concurrency", type=int, default=10,
                        help="Max simultaneous workers (default: 10)")
    parser.add_argument("--proxy", default="",
                        help="Force a single proxy URL (overrides --proxy-type and pool)")
    parser.add_argument("--proxy-type", choices=("isp", "webshare", "soax"), default="isp",
                        help="isp=proxies_isp.txt, soax=proxies_soax.txt, webshare=numbered residential")
    parser.add_argument("--ws-start", type=int, default=1,
                        help="Webshare numbered proxy start index (default: 1)")
    parser.add_argument("--lang", default=env.get("SITE_LANGUAGE", "PT"),
                        help="Language for registration (default: PT)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print plan without executing")
    parser.add_argument("--include-failed", action="store_true",
                        help="Retry accounts with status=failed (default: new only)")
    parser.add_argument("--verify-registered", action="store_true", dest="verify_registered",
                        help="Skip registration; only verify accounts already in status=registered")
    parser.add_argument("--email", choices=("mailtm", "cloudflare"), default="cloudflare",
                        help="Email provider: cloudflare (default) or mailtm")
    parser.add_argument("--account-type", choices=("fake", "real"), default=None,
                        dest="account_type",
                        help="Filter: only process fake or real accounts")

    args = parser.parse_args()

    def _keys(env_key: str) -> list[str]:
        return [k.strip() for k in env.get(env_key, "").split(",") if k.strip()]

    capsolver_keys    = _keys("CAPSOLVER_KEYS")
    anticaptcha_keys  = _keys("ANTICAPTCHA_KEYS")
    twocaptcha_keys   = _keys("TWOCAPTCHA_KEYS")
    capmonster_keys   = _keys("CAPMONSTER_KEYS")

    if not any([capsolver_keys, anticaptcha_keys, twocaptcha_keys, capmonster_keys]):
        print("ERROR: no CAPTCHA solver keys set in .env")
        sys.exit(1)

    print(f"[batch] solvers: capsolver={len(capsolver_keys)} anticaptcha={len(anticaptcha_keys)} "
          f"2captcha={len(twocaptcha_keys)} capmonster={len(capmonster_keys)}")
    print(f"[batch] proxy type: {args.proxy_type}  email: {args.email}"
          + (f"  ws-start={args.ws_start}" if args.proxy_type == "webshare" else ""))

    asyncio.run(main_async(args, capsolver_keys, anticaptcha_keys, twocaptcha_keys, capmonster_keys))


if __name__ == "__main__":
    main()
