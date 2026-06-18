"""
diag_slots.py — Definitive slots diagnosis probe.

Answers: "Are empty /slots responses genuine (no appointments) or a bot failure?"

Flow:
  1. Fresh login via SOAX proxy
  2. Full warmup (quest → formulario → schedulecontroller → schedule.jsp)
  3. Verify Schedule.jsp shows real scheduling markers (not auth-redirect)
  4. Test four methods against /slots with a fresh CAPTCHA per method
  5. Print comparison table + definitive verdict

Four methods (most → least trusted):
  A. Browser JS eval   — fetch() in browser's own JS context, same origin, same cookies
  B. browser_fetch     — Playwright XHR via browser session
  C. primp (same proxy)— direct HTTP with synced cookies, no browser
  D. primp (new proxy) — rotated SOAX proxy, same cookies

Usage:
  python -m probes.diag_slots --account eucgom6577 --posto 3088
  python -m probes.diag_slots --account eucgom6577 --posto 3088 --nat CPV --res FRA
  python -m probes.diag_slots --account eucgom6577 --posto 3088 --skip-warmup
"""

import argparse
import asyncio
import json
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

# Bootstrap: ensure project root is on sys.path so `import app` resolves
# regardless of which directory the user launched from.
_project_root = Path(__file__).resolve().parent.parent
if str(_project_root) not in sys.path:
    sys.path.insert(0, str(_project_root))

import app  # noqa: F401  — adds app/core to sys.path

_ROOT      = Path(__file__).resolve().parent.parent
_CORE      = _ROOT / "app" / "core"
_ENV_FILE  = _CORE / ".env"
_SOAX_FILE = _CORE / "data" / "proxies_soax.txt"
_ACCT_FILES = [
    _CORE / "data" / "accounts.csv",
    _CORE / "data" / "test_accounts.csv",
]

BASE      = "https://pedidodevistos.mne.gov.pt"
SLOTS_URL = BASE + "/VistosOnline/slots"
SCHED_JSP = BASE + "/VistosOnline/Schedule.jsp"
HOME_URL  = BASE + "/VistosOnline/"

_SCHED_MARKERS = ("getSlots", "captchaDiv", "SubmeterVistoCriaPDF", "f_date_c", "cmbPeriodo")


def _ts() -> str:
    return time.strftime("%H:%M:%S")


def log(msg: str, tag: str = "diag") -> None:
    print(f"[{_ts()}][{tag}] {msg}", flush=True)


# ── env + keys ──────────────────────────────────────────────────────────────────

def _load_env() -> dict:
    env: dict = {}
    if not _ENV_FILE.exists():
        return env
    for line in _ENV_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        env[k.strip()] = v.strip()
    return env


def _keys(env: dict, key: str) -> list[str]:
    return [x.strip() for x in env.get(key, "").split(",") if x.strip()]


# ── account loader ──────────────────────────────────────────────────────────────

def _load_account(username: str) -> dict:
    import csv
    for acct_file in _ACCT_FILES:
        if not acct_file.exists():
            continue
        rows = list(csv.DictReader(acct_file.read_text(encoding="utf-8").splitlines()))
        acct = next((r for r in rows if r["username"] == username), None)
        if acct:
            log(f"account found in {acct_file.name}")
            return acct
    searched = ", ".join(f.name for f in _ACCT_FILES)
    sys.exit(f"Account '{username}' not found in: {searched}")


# ── proxy ───────────────────────────────────────────────────────────────────────

_proxy_pool = None


def _next_proxy() -> str:
    global _proxy_pool
    from proxy_pool import PersistentProxyPool
    if _proxy_pool is None:
        _proxy_pool = PersistentProxyPool(_SOAX_FILE)
    return _proxy_pool.advance()


# ── checkpoint restore (fast path — no login needed) ───────────────────────────

def _try_restore_checkpoint(username: str) -> tuple | None:
    """
    If a schedule_jsp session checkpoint exists, open a fresh browser session,
    restore the saved cookies, navigate to Schedule.jsp and verify markers.
    Returns (session, proxy, sched_html) on success, None if checkpoint is
    missing, expired, or the session is dead.
    """
    import session as sess
    import session_store

    meta = session_store.load_meta(username)
    if not meta:
        log(f"no saved session checkpoint for {username}", username)
        return None

    checkpoint = meta.get("checkpoint", "")
    proxy      = meta.get("proxy") or ""
    cookies    = meta.get("cookies", {})
    posto_id   = meta.get("posto_id", "")
    log(f"checkpoint={checkpoint}  proxy={proxy.split('@')[-1] if '@' in proxy else proxy}  "
        f"posto={posto_id}  cookies={list(cookies.keys())}", username)

    if checkpoint != "schedule_jsp" or not cookies:
        log("checkpoint not at schedule_jsp or no cookies — skipping restore", username)
        return None

    try:
        s = sess.get_session(proxy or _next_proxy(), headless=True)
    except Exception as e:
        log(f"get_session for restore failed: {e}", username)
        return None

    # Set cookies on the primp client; browser_nav will sync them to the browser
    primp_client = getattr(s, "client", None)
    if primp_client and cookies:
        try:
            primp_client.set_cookies(session_store.COOKIES_URL, cookies)
        except Exception as e:
            log(f"set_cookies failed: {e}", username)

    sched_url = f"https://pedidodevistos.mne.gov.pt/VistosOnline/Schedule.jsp?posto_id={posto_id}"
    try:
        log(f"browser_nav → {sched_url}", username)
        s.browser_nav(sched_url, timeout=30, fast_nav=True)
        sched_html = s.browser_eval("() => document.documentElement.outerHTML", timeout=15) or ""
    except Exception as e:
        log(f"restore nav failed: {e}", username)
        try: s.close()
        except Exception: pass
        return None

    has_markers = any(m in sched_html for m in _SCHED_MARKERS)
    log(f"Schedule.jsp len={len(sched_html)}  markers={has_markers}", username)
    if not has_markers:
        log("restored session landed on wrong page — session likely expired", username)
        try: s.close()
        except Exception: pass
        return None

    log("checkpoint restore SUCCESS — skipping login+warmup", username)
    return s, proxy, sched_html, posto_id


# ── login ───────────────────────────────────────────────────────────────────────

def _do_login(acct: dict, capsolver_keys: list[str], anticaptcha_keys: list[str],
              twocaptcha_keys: list[str], capmonster_keys: list[str],
              proxy_limit: int = 20):
    """Open an OctoSession and log in. Returns (session, proxy) or raises."""
    import session as sess
    import solver as sol

    username = acct["username"]
    password = acct["password"]

    for attempt in range(1, proxy_limit + 1):
        proxy_url = _next_proxy()
        host = proxy_url.split("@")[-1] if "@" in proxy_url else proxy_url
        log(f"proxy attempt {attempt}/{proxy_limit}: {host}", username)
        try:
            s = sess.get_session(proxy_url, headless=True)
        except Exception as e:
            log(f"get_session failed: {e}", username)
            continue

        # Quick WAF check before spending CAPTCHA budget
        try:
            page_html = s.browser_eval("() => document.documentElement.innerHTML", timeout=10) or ""
        except Exception as e:
            log(f"browser_eval failed: {e}", username)
            try: s.close()
            except Exception: pass
            continue

        if "/ch/bd.js" in page_html or '"/ch/' in page_html:
            log("WAF challenge on proxy — skip", username)
            try: s.close()
            except Exception: pass
            continue

        log(f"proxy OK — logging in", username)
        try:
            result = s.browser_login(
                username, password, lang="PT",
                solver_key=capsolver_keys[0] if capsolver_keys else "",
                twocaptcha_key=twocaptcha_keys[0] if twocaptcha_keys else "",
                anticaptcha_keys=anticaptcha_keys,
                capmonster_keys=capmonster_keys,
            )
        except Exception as e:
            log(f"browser_login failed: {e}", username)
            try: s.close()
            except Exception: pass
            continue

        if isinstance(result, dict) and result.get("status") == "success":
            log(f"login success", username)
            return s, proxy_url

        log(f"login result: {result}", username)
        try: s.close()
        except Exception: pass

    raise RuntimeError(f"Could not log in after {proxy_limit} proxy attempts")


# ── slots test methods ──────────────────────────────────────────────────────────

def _classify(status: int, body: str) -> str:
    """Classify the /slots response."""
    snip = body[:400]
    if "body{margin:0;background:#fff}" in snip or "datadome" in snip.lower():
        return "DATADOME_BLOCK"
    if status == 302:
        return "REDIRECT_302"
    if status >= 400:
        return f"HTTP_{status}"
    try:
        j = json.loads(body)
        data = j.get("data", "MISSING")
        if isinstance(data, dict) and data:
            # Extract available dates/periods
            dates = list(data.keys())
            return f"SLOTS_FOUND({len(dates)} dates: {dates[:3]})"
        elif isinstance(data, dict) and not data:
            return "EMPTY_data={}"
        else:
            return f"UNKNOWN_JSON({str(data)[:80]})"
    except Exception:
        # Browser wraps JSON in <pre>
        m = re.search(r"<pre[^>]*>(.*?)</pre>", body, re.DOTALL)
        if m:
            try:
                j = json.loads(m.group(1).strip())
                data = j.get("data", "MISSING")
                if isinstance(data, dict) and data:
                    return f"SLOTS_FOUND_PRE({list(data.keys())[:3]})"
                elif isinstance(data, dict):
                    return "EMPTY_data={}_PRE"
            except Exception:
                pass
        return f"NON_JSON(len={len(body)})"


def _solve_captcha(capsolver_keys, anticaptcha_keys, twocaptcha_keys, capmonster_keys,
                   proxy: str, label: str) -> str | None:
    import solver as sol
    log(f"CAPTCHA solve (proxy={proxy.split('@')[-1]})", label)
    t0 = time.time()
    try:
        token = sol.race_all(
            capsolver_keys, anticaptcha_keys, twocaptcha_keys, capmonster_keys,
            "SCHEDULE_EVISA", proxy, min_score=50,
        )
        log(f"CAPTCHA OK in {time.time()-t0:.1f}s (len={len(token)})", label)
        return token
    except Exception as e:
        log(f"CAPTCHA failed: {e}", label)
        return None


def _method_a_browser_js(client, posto_id: str, token: str) -> tuple[int, str]:
    """Fetch /slots via browser JS eval (fetch() in page context). Most trusted."""
    # Token and posto_id are JSON-encoded to avoid breaking the JS string — tokens
    # can contain base64 padding (=) and + which would corrupt a raw f-string embed.
    token_js  = json.dumps(token)
    posto_js  = json.dumps(posto_id)
    js = f"""
async () => {{
    try {{
        const posto = {posto_js};
        const captcha = {token_js};
        const body = new URLSearchParams({{posto_id: posto, captcha: captcha}}).toString();
        const resp = await fetch('/VistosOnline/slots?posto_id=' + encodeURIComponent(posto), {{
            method: 'POST',
            headers: {{
                'X-Requested-With': 'XMLHttpRequest',
                'Content-Type': 'application/x-www-form-urlencoded; charset=UTF-8'
            }},
            body: body
        }});
        const text = await resp.text();
        return JSON.stringify({{status: resp.status, url: resp.url, body: text}});
    }} catch(e) {{
        return JSON.stringify({{status: 0, url: '', body: 'JS_ERROR: ' + e.message}});
    }}
}}
"""
    raw = client.browser_eval(js, timeout=30)
    try:
        parsed = json.loads(raw)
        return parsed.get("status", 0), parsed.get("body", "")
    except Exception:
        return 0, str(raw)


def _method_b_browser_fetch(client, posto_id: str, token: str, sched_url: str) -> tuple[int, str]:
    """Fetch /slots via browser_fetch (Playwright XHR)."""
    raw = client.browser_fetch(
        SLOTS_URL, {"posto_id": posto_id, "captcha": token},
        params={"posto_id": posto_id},
        headers={"X-Requested-With": "XMLHttpRequest", "Referer": sched_url},
        timeout=30,
    )
    return raw["status"], raw["body"]


def _method_c_primp_same(client, posto_id: str, token: str, proxy: str,
                          sched_url: str) -> tuple[int, str]:
    """Fetch /slots via primp with the same proxy and synced cookies."""
    import primp
    import session as sess
    import session_store as _ss

    primp_client = getattr(client, "client", None)
    cookies = {}
    if primp_client:
        try:
            cookies = primp_client.get_cookies(_ss.COOKIES_URL) or {}
        except Exception:
            pass

    c = primp.Client(proxy=proxy, impersonate="chrome_131",
                     verify=True, follow_redirects=False)
    if cookies:
        c.set_cookies(_ss.COOKIES_URL, cookies)
    r = c.post(SLOTS_URL, params={"posto_id": posto_id},
               data={"posto_id": posto_id, "captcha": token},
               headers={**sess.HEADERS_XHR, "Referer": sched_url}, timeout=30)
    return r.status_code, r.text


def _method_d_primp_new(posto_id: str, token: str, cookies: dict,
                         sched_url: str) -> tuple[int, str, str]:
    """Fetch /slots via primp with a fresh SOAX proxy and synced cookies."""
    import primp
    import session as sess
    import session_store as _ss

    new_proxy = _next_proxy()
    log(f"new proxy: {new_proxy.split('@')[-1]}", "method_D")
    c = primp.Client(proxy=new_proxy, impersonate="chrome_131",
                     verify=True, follow_redirects=False)
    if cookies:
        c.set_cookies(_ss.COOKIES_URL, cookies)
    r = c.post(SLOTS_URL, params={"posto_id": posto_id},
               data={"posto_id": posto_id, "captcha": token},
               headers={**sess.HEADERS_XHR, "Referer": sched_url}, timeout=30)
    return r.status_code, r.text, new_proxy


# ── main diagnostic loop ────────────────────────────────────────────────────────

async def run_diag(args: argparse.Namespace) -> None:
    env = _load_env()
    capsolver_keys   = _keys(env, "CAPSOLVER_KEYS")
    anticaptcha_keys = _keys(env, "ANTICAPTCHA_KEYS")
    twocaptcha_keys  = _keys(env, "TWOCAPTCHA_KEYS")
    capmonster_keys  = _keys(env, "CAPMONSTER_KEYS")

    if not any([capsolver_keys, anticaptcha_keys, twocaptcha_keys, capmonster_keys]):
        sys.exit("No CAPTCHA keys found in .env — cannot solve SCHEDULE_EVISA captcha")

    acct     = _load_account(args.account)
    posto_id = args.posto
    nat      = args.nat or acct.get("nationality", "CPV")
    res      = args.res or nat

    log(f"account={args.account}  posto={posto_id}  nat={nat}  res={res}")

    executor = ThreadPoolExecutor(max_workers=2)
    loop     = asyncio.get_event_loop()
    client   = None
    proxy    = ""

    try:
        # ── Step 1: Session (checkpoint restore OR fresh login) ───────────────────
        sched_html = ""
        sched_url  = SCHED_JSP + f"?posto_id={posto_id}"
        posto_pdf  = posto_id
        has_markers = False

        if not args.force_login:
            log("=== STEP 1: TRY CHECKPOINT RESTORE ===")
            restored = await loop.run_in_executor(executor,
                lambda: _try_restore_checkpoint(args.account))
            if restored:
                client, proxy, sched_html, saved_posto = restored
                # Use the saved posto_id if the user didn't override it on CLI
                if not args.posto_override:
                    posto_id = saved_posto or posto_id
                sched_url   = SCHED_JSP + f"?posto_id={posto_id}"
                posto_pdf   = posto_id
                has_markers = True
                log(f"using restored session — posto={posto_id}")

        if client is None:
            log("=== STEP 1: FRESH LOGIN ===")
            client, proxy = await loop.run_in_executor(executor, lambda: _do_login(
                acct, capsolver_keys, anticaptcha_keys, twocaptcha_keys, capmonster_keys))

            # ── Step 2: Warmup ────────────────────────────────────────────────────
            if args.skip_warmup:
                log("=== STEP 2: WARMUP SKIPPED (--skip-warmup) ===")
            else:
                log("=== STEP 2: WARMUP (quest → formulario → schedulecontroller → schedule.jsp) ===")
                from batch_apply import apply_warmup
                warmup_result = await apply_warmup(
                    acct, posto_id,
                    capsolver_keys, anticaptcha_keys, twocaptcha_keys, capmonster_keys,
                    executor, nationality=nat, residence=res, client=client, proxy=proxy,
                )
                if isinstance(warmup_result, str):
                    log(f"WARMUP FAILED: {warmup_result}")
                    return
                posto_pdf = warmup_result["posto_pdf"]
                sched_url = warmup_result["sched_url"]
                log(f"warmup OK — posto_pdf={posto_pdf}  sched_url={sched_url}")

            # ── Step 3: Verify Schedule.jsp ───────────────────────────────────────
            log("=== STEP 3: VERIFY SCHEDULE.JSP ===")
            sched_html = await loop.run_in_executor(executor, lambda: client.browser_eval(
                "() => document.documentElement.outerHTML", timeout=15)) or ""
            has_markers = any(m in sched_html for m in _SCHED_MARKERS)
        log(f"Schedule.jsp len={len(sched_html)}  has_scheduling_markers={has_markers}")
        if not has_markers:
            log(f"WARNING: Schedule.jsp does NOT show scheduling page. First 500 chars:")
            log(sched_html[:500])
            log("This means warmup did not establish ScheduleController state.")
            log("Slot tests will likely return empty regardless of method.")
        else:
            log("Schedule.jsp shows real scheduling page — ScheduleController state confirmed.")
        await loop.run_in_executor(executor, lambda: client.screenshot(args.account, "schedule_jsp_verified"))

        # ── Step 4: Four-method slot test ─────────────────────────────────────────
        log("=== STEP 4: FOUR-METHOD SLOT TEST ===")

        # Shared cookies for primp methods (C/D)
        import session_store as _ss
        primp_client = getattr(client, "client", None)
        shared_cookies: dict = {}
        if primp_client:
            try:
                shared_cookies = primp_client.get_cookies(_ss.COOKIES_URL) or {}
            except Exception:
                pass
        log(f"shared cookies: {list(shared_cookies.keys())}")

        results: list[tuple[str, int, str, str]] = []

        # Method A — browser JS fetch (most trusted, same origin, same session)
        log("--- Method A: browser JS fetch ---")
        token_a = await loop.run_in_executor(executor, lambda: _solve_captcha(
            capsolver_keys, anticaptcha_keys, twocaptcha_keys, capmonster_keys,
            proxy, "method_A"))
        if token_a:
            try:
                st_a, body_a = await loop.run_in_executor(executor,
                    lambda: _method_a_browser_js(client, posto_id, token_a))
                verdict_a = _classify(st_a, body_a)
            except Exception as e:
                st_a, body_a, verdict_a = 0, "", f"EXCEPTION:{e}"
        else:
            st_a, body_a, verdict_a = 0, "", "NO_CAPTCHA_TOKEN"
        results.append(("A:browser_js_eval", st_a, verdict_a, body_a[:600]))
        log(f"Method A → {verdict_a}  (status={st_a})")
        await loop.run_in_executor(executor, lambda: client.screenshot(args.account, "after_method_A"))

        # Method B — browser_fetch (Playwright XHR)
        log("--- Method B: browser_fetch ---")
        token_b = await loop.run_in_executor(executor, lambda: _solve_captcha(
            capsolver_keys, anticaptcha_keys, twocaptcha_keys, capmonster_keys,
            proxy, "method_B"))
        if token_b:
            try:
                st_b, body_b = await loop.run_in_executor(executor,
                    lambda: _method_b_browser_fetch(client, posto_id, token_b, sched_url))
                verdict_b = _classify(st_b, body_b)
            except Exception as e:
                st_b, body_b, verdict_b = 0, "", f"EXCEPTION:{e}"
        else:
            st_b, body_b, verdict_b = 0, "", "NO_CAPTCHA_TOKEN"
        results.append(("B:browser_fetch", st_b, verdict_b, body_b[:600]))
        log(f"Method B → {verdict_b}  (status={st_b})")

        # Method C — primp, same proxy, synced cookies
        log("--- Method C: primp (same proxy + synced cookies) ---")
        token_c = await loop.run_in_executor(executor, lambda: _solve_captcha(
            capsolver_keys, anticaptcha_keys, twocaptcha_keys, capmonster_keys,
            proxy, "method_C"))
        if token_c:
            try:
                st_c, body_c = await loop.run_in_executor(executor,
                    lambda: _method_c_primp_same(client, posto_id, token_c, proxy, sched_url))
                verdict_c = _classify(st_c, body_c)
            except Exception as e:
                st_c, body_c, verdict_c = 0, "", f"EXCEPTION:{e}"
        else:
            st_c, body_c, verdict_c = 0, "", "NO_CAPTCHA_TOKEN"
        results.append(("C:primp_same_proxy", st_c, verdict_c, body_c[:600]))
        log(f"Method C → {verdict_c}  (status={st_c})")

        # Method D — primp, fresh SOAX proxy, same cookies
        log("--- Method D: primp (fresh proxy + synced cookies) ---")
        token_d = await loop.run_in_executor(executor, lambda: _solve_captcha(
            capsolver_keys, anticaptcha_keys, twocaptcha_keys, capmonster_keys,
            proxy, "method_D"))
        if token_d:
            try:
                st_d, body_d, new_proxy_d = await loop.run_in_executor(executor,
                    lambda: _method_d_primp_new(posto_id, token_d, shared_cookies, sched_url))
                verdict_d = _classify(st_d, body_d)
            except Exception as e:
                st_d, body_d, verdict_d = 0, "", f"EXCEPTION:{e}"
        else:
            st_d, body_d, verdict_d = 0, "", "NO_CAPTCHA_TOKEN"
        results.append(("D:primp_fresh_proxy", st_d, verdict_d, body_d[:600]))
        log(f"Method D → {verdict_d}  (status={st_d})")

        # ── Step 5: Print verdict ──────────────────────────────────────────────────
        log("")
        log("=" * 72)
        log("RESULTS")
        log("=" * 72)
        if not has_markers:
            log("!! WARNING: Schedule.jsp had no scheduling markers — results below are")
            log("!! suspect because ScheduleController state was not established.")
        log("")
        log(f"  {'METHOD':<30}  {'STATUS':>6}  VERDICT")
        log(f"  {'-'*30}  {'-'*6}  {'-'*40}")
        for method, st, verdict, _ in results:
            log(f"  {method:<30}  {st:>6}  {verdict}")

        log("")
        slots_found = any("SLOTS_FOUND" in v for _, _, v, _ in results)
        all_empty   = all(v in ("EMPTY_data={}", "EMPTY_data={}_PRE") for _, _, v, _ in results if v != "NO_CAPTCHA_TOKEN")
        any_dd      = any("DATADOME" in v for _, _, v, _ in results)

        if slots_found:
            log("VERDICT: SLOTS AVAILABLE — bot successfully retrieved slot data.")
        elif has_markers and all_empty:
            log("VERDICT: GENUINE_EMPTY — server returned {\"data\":{}} on all methods.")
            log("  The server session state is correct (Schedule.jsp confirmed).")
            log("  This posto has no available appointments right now.")
            log("  No code change will help — slots are simply full.")
        elif not has_markers and all_empty:
            log("VERDICT: LIKELY_BOT_FAILURE (bad session state).")
            log("  Schedule.jsp did not show scheduling markers → ScheduleController")
            log("  state was never established. Server returns empty for un-warmed sessions.")
            log("  Fix: ensure warmup completes successfully before polling slots.")
        elif any_dd and all_empty:
            log("VERDICT: DATADOME_BLOCKED — server is rejecting our requests.")
            log("  The proxy IP is flagged; consider trying different geo/provider.")
        else:
            log("VERDICT: MIXED — investigate individual method outputs above.")

        log("")
        log("RESPONSE BODIES (first 600 chars each):")
        for method, st, verdict, body in results:
            log(f"  [{method}]")
            log(f"    {body[:200]}")

    finally:
        if client is not None:
            try:
                client.close()
            except Exception:
                pass
        executor.shutdown(wait=False)


# ── entrypoint ──────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="Slots diagnosis probe")
    parser.add_argument("--account", required=True, help="Username to test with")
    parser.add_argument("--posto",   default="",    help="posto_id override (default: from checkpoint or 3088)")
    parser.add_argument("--nat",     default="",    help="Nationality code (default: from accounts.csv)")
    parser.add_argument("--res",     default="",    help="Residence code (default: nat)")
    parser.add_argument("--skip-warmup", action="store_true",
                        help="Skip warmup after login (only relevant if --force-login is set)")
    parser.add_argument("--force-login", action="store_true",
                        help="Always do a fresh login+warmup, ignore any saved checkpoint")
    args = parser.parse_args()
    # Track whether the user explicitly set --posto so we know whether to
    # override the checkpoint's saved posto_id.
    args.posto_override = bool(args.posto)
    args.posto = args.posto or "3088"
    asyncio.run(run_diag(args))


if __name__ == "__main__":
    main()
