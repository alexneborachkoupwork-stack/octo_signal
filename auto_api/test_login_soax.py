"""
Diagnostic login test: SOAX proxy + CAPTCHA infra pre-screening.

Phase 0 — SOLVER HEALTH CHECK
  Solve 3 ProxyLess tokens via race_all() in parallel.
  Report len/score for each. Stop if avg_score < --min-captcha-score.

Phase 1 — PER-ACCOUNT LOGIN
  For each account:
    PROXY   — advance SOAX pool → get_session() to open browser + WAF bypass.
              WAF status checked immediately. Session kept alive (reused for login).
              1 browser launch (screen+login combined, avoids batch_login.py double-launch).
    CAPTCHA — score reported by session.py during browser_login.
    LOGIN   — browser_login(skip_checkbox=True) [default].
              If WAF challenge in POST response: solve PoW → fresh CAPTCHA → browser_fetch retry.
    RESULT  — active | waf_bypassed_ok | waf_bypass_fail | waf_challenge | error

Default mode: skip_checkbox=True (race_all, 1 race).
Pass --use-checkbox to test checkbox + race_best x3 (min_score=85) instead.

Usage:
  uv run python test_login_soax.py --mode test --count 1
  uv run python test_login_soax.py --mode test --count 3
  uv run python test_login_soax.py --mode test --count 1 --use-checkbox
  uv run python test_login_soax.py --mode test --count 1 --headed --save-state
"""

import argparse
import hashlib
import json
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

BASE      = "https://pedidodevistos.mne.gov.pt"
HOME_URL  = BASE + "/VistosOnline/"
AUTH_URL  = BASE + "/VistosOnline/Authentication.jsp"
LOGIN_URL = BASE + "/VistosOnline/login"

_HERE          = Path(__file__).parent
_ENV_FILE      = _HERE / ".env"
_SOAX_FILE     = _HERE / "data" / "proxies_soax.txt"
_WEBSHARE_FILE = _HERE / "data" / "proxies_webshare.txt"
_SESSIONS_DIR = _HERE / "sessions"

_csv_lock = threading.Lock()


# ── env helpers ───────────────────────────────────────────────────────────────

def _load_env() -> dict:
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


def _keys(env: dict, name: str) -> list[str]:
    raw = env.get(name, "")
    return [k.strip() for k in raw.split(",") if k.strip()]


# ── CSV helpers ───────────────────────────────────────────────────────────────

def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _csv_update(accounts_file: Path, username: str, **fields) -> None:
    import csv
    with _csv_lock:
        with open(accounts_file, encoding="utf-8", newline="") as f:
            rows = list(csv.DictReader(f))
            fieldnames = list(rows[0].keys()) if rows else []
        for r in rows:
            if r["username"] == username:
                for k, v in fields.items():
                    if k in r:
                        r[k] = v
                break
        with open(accounts_file, "w", encoding="utf-8", newline="") as f:
            w = csv.DictWriter(f, fieldnames=fieldnames)
            w.writeheader()
            w.writerows(rows)


def _is_waf_challenge(text: str) -> bool:
    return "/ch/bd.js" in text


def _parse_waf_challenge(html: str) -> dict | None:
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
            print(f"[waf] parse FAIL — body len={len(html)}, head={html[:500]!r}")
            return None
        return {"difficulty": int(m.group(1)), "token": m.group(2), "nonce": m.group(3)}
    return {"nonce": m.group(1), "token": m.group(2), "difficulty": int(m.group(3))}


def _solve_pow(nonce: str, token: str, difficulty: int) -> str:
    prefix = "0" * difficulty
    p = 0
    base = (nonce + token).encode()
    while True:
        candidate = base + str(p).encode()
        if hashlib.sha256(candidate).hexdigest().startswith(prefix):
            return str(p)
        p += 1


def _waf_bypass(session, html: str, label: str) -> bool:
    """Solve WAF challenge from the login POST response.
    Variant A (plain JSON nonce/token/difficulty): parse + PoW + /ch/v POST.
    Variant B (obfuscated eval JS): document.write into current page, browser executes natively.
    Returns True if bypass cookie should now be set.
    """
    # ── Variant A: parse nonce/token/difficulty ──────────────────────────────
    challenge = _parse_waf_challenge(html)
    if challenge:
        nonce, token, difficulty = challenge["nonce"], challenge["token"], challenge["difficulty"]
        print(f"[waf] {label}: Variant A — solving PoW difficulty={difficulty} nonce={nonce[:8]}...")
        proof = _solve_pow(nonce, token, difficulty)
        print(f"[waf] {label}: proof={proof} — submitting to /ch/v")
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
            const body = await r.text();
            return {{ status: r.status, ok: r.ok, body: body }};
        }}
        """
        try:
            result = session.browser_eval(waf_js, timeout=30)
            ok = result.get("ok", False)
            print(f"[waf] {label}: /ch/v status={result.get('status')}  ok={ok}  body={result.get('body','')[:80]}")
            return ok
        except Exception as e:
            print(f"[waf] {label}: /ch/v error: {e} — falling through to Variant B")

    # ── Variant B: obfuscated JS — execute via document.write in page ────────
    # srcdoc iframe fails: WAF JS has anti-framing check (window !== window.top).
    # document.write replaces page content in-place keeping the portal origin
    # so the WAF JS runs freely, solves PoW, POSTs to /ch/v → sets bypass cookie.
    #
    # Use a SYNCHRONOUS JS function (no await) that returns immediately after
    # loading the HTML. This avoids leaving a pending async eval result in the
    # session queue that would poison the next browser_nav / browser_fetch call.
    # The WAF JS executes in the page while Python sleeps separately.
    print(f"[waf] {label}: Variant B — obfuscated WAF, loading via document.write (~14s wait)")
    load_js = f"""
    () => {{
        try {{
            document.open('text/html', 'replace');
            document.write({json.dumps(html)});
            document.close();
            return 'loaded';
        }} catch(e) {{
            return 'error:' + e.message;
        }}
    }}
    """
    try:
        r = session.browser_eval(load_js, timeout=5)
        print(f"[waf] {label}: Variant B HTML loaded → {r}")
    except Exception as e:
        # Navigation triggered during document.write: WAF JS took over immediately
        print(f"[waf] {label}: Variant B load note (navigation?): {e}")

    # Wait for WAF JS to execute its PoW + /ch/v POST + set bypass cookie
    time.sleep(14)

    # Re-navigate to AUTH_URL to restore page context for the retry fetch
    try:
        session.browser_nav(AUTH_URL, timeout=45)
        print(f"[waf] {label}: re-navigated to AUTH_URL — bypass cookie should be set")
        return True
    except Exception as e:
        print(f"[waf] {label}: AUTH_URL re-nav failed: {e}")
        return False


# ── Phase 0: CAPTCHA solver health check ─────────────────────────────────────

def _phase0_captcha_health(capsolver_keys, anticaptcha_keys,
                           twocaptcha_keys, capmonster_keys,
                           n_tokens: int = 3) -> list[int]:
    """
    Solve n_tokens ProxyLess LOGIN_EVISA tokens in parallel.
    Returns list of scores (empty list if all fail).
    """
    import solver as sol

    print(f"\n{'='*60}")
    print(f"[SOLVER-HEALTH] Solving {n_tokens} ProxyLess LOGIN_EVISA tokens in parallel")
    print(f"{'='*60}")

    scores = []
    errors = []

    def _one(i: int):
        try:
            token = sol.race_all(
                capsolver_keys, anticaptcha_keys, twocaptcha_keys, capmonster_keys,
                "LOGIN_EVISA", proxy=None,
            )
            score = sol.score_token(token)
            return i, token, score, None
        except Exception as e:
            return i, None, 0, str(e)

    with ThreadPoolExecutor(max_workers=n_tokens) as ex:
        futs = [ex.submit(_one, i + 1) for i in range(n_tokens)]
        for f in as_completed(futs):
            idx, token, score, err = f.result()
            if err:
                print(f"[SOLVER-HEALTH] token {idx}: FAIL — {err}")
                errors.append(err)
            else:
                print(f"[SOLVER-HEALTH] token {idx}: len={len(token)}  score={score}")
                scores.append(score)

    if scores:
        avg = sum(scores) / len(scores)
        print(f"[SOLVER-HEALTH] scores={scores}  avg={avg:.0f}  min={min(scores)}  max={max(scores)}")
    else:
        print(f"[SOLVER-HEALTH] ALL FAILED — solver services not responding")

    return scores


# ── Phase 1: per-account login ────────────────────────────────────────────────

def _login_one(acct: dict, accounts_file: Path,
               capsolver_keys, anticaptcha_keys,
               twocaptcha_keys, capmonster_keys,
               soax_pool, lang: str = "PT",
               skip_checkbox: bool = False,
               min_score: int = 80,
               headed: bool = False,
               save_state: bool = False,
               proxy_search_limit: int = 25) -> dict:
    """
    Single-account login with explicit per-stage logging.
    skip_checkbox=False (default): checkbox click + race_best x3 (min_score=85)
    skip_checkbox=True:            skip click, race_all single race
    Returns stage_results dict.
    """
    import session as sess
    import solver as sol

    username = acct["username"]
    password = acct["password"]

    stages = {
        "account":      username,
        "proxy_screen": "pending",
        "exit_ip":      "",
        "get_session":  "pending",
        "login":        "pending",
        "result":       "error",
    }

    # ── PROXY: find a SOAX session that clears the WAF ───────────────────────
    print(f"\n[{username}] --- PROXY SCREEN ---")
    open_session = None
    used_proxy   = None
    exit_ip_used = None

    for attempt in range(1, proxy_search_limit + 1):
        proxy_url = soax_pool.advance()
        host = proxy_url.split("@")[-1] if "@" in proxy_url else proxy_url

        print(f"[{username}] proxy attempt {attempt}/{proxy_search_limit}: {host}")
        try:
            s = sess.get_session(proxy_url, headless=not headed)
        except Exception as e:
            print(f"[{username}]   get_session FAIL: {e}")
            continue

        # Check WAF status from the already-open browser
        try:
            current_url = s.browser_eval("() => location.href", timeout=10)
            page_html   = s.browser_eval("() => document.documentElement.innerHTML", timeout=10)
        except Exception as e:
            print(f"[{username}]   browser_eval FAIL: {e}")
            try: s.close()
            except Exception: pass
            continue

        if "/ch/" in (current_url or "") or _is_waf_challenge(page_html or ""):
            print(f"[{username}]   WAF challenge — skip  url={current_url}")
            try: s.close()
            except Exception: pass
            continue

        # Passed WAF — keep session open
        try:
            exit_ip_used = s.browser_eval(
                "async () => { const r = await fetch('https://httpbin.org/ip'); "
                "const d = await r.json(); return d.origin; }", timeout=15,
            )
        except Exception:
            exit_ip_used = host

        print(f"[{username}]   PASS  exit_ip={exit_ip_used}  url={current_url}")
        stages["proxy_screen"] = f"PASS(attempt={attempt})"
        stages["exit_ip"]      = exit_ip_used
        stages["get_session"]  = "PASS"
        open_session = s
        used_proxy   = proxy_url
        break

    if open_session is None:
        print(f"[{username}] PROXY SCREEN: all {proxy_search_limit} attempts failed")
        stages["proxy_screen"] = f"FAIL(all {proxy_search_limit} dead/WAF)"
        stages["result"]       = "proxy_dead"
        return stages

    # ── LOGIN ─────────────────────────────────────────────────────────────────
    print(f"\n[{username}] --- LOGIN ---")
    solver_key     = capsolver_keys[0]  if capsolver_keys   else ""
    twocaptcha_key = twocaptcha_keys[0] if twocaptcha_keys  else ""

    mode_label = "skip_checkbox+race_all" if skip_checkbox else "checkbox+race_best(x3,min=85)"
    body   = ""
    status = 0
    try:
        print(f"[{username}] mode: browser_login({mode_label})")
        result = open_session.browser_login(
            username, password, lang=lang,
            solver_key=solver_key,
            twocaptcha_key=twocaptcha_key,
            anticaptcha_keys=anticaptcha_keys,
            twocaptcha_keys=twocaptcha_keys,
            capmonster_keys=capmonster_keys,
            skip_checkbox=skip_checkbox,
            min_score=min_score,
        )
        body          = (result.get("body") or "").strip()
        status        = result.get("status", 0)
        orig_token    = result.get("captcha_token", "")
        print(f"[{username}] login raw: status={status}  waf={_is_waf_challenge(body)}  resp={body[:200]}")
        stages["login"] = f"responded(status={status})"
    except Exception as e:
        print(f"[{username}] login EXCEPTION: {e}")
        stages["login"]  = f"EXCEPTION: {e}"
        stages["result"] = "error"
        _csv_update(accounts_file, username, status="login_failed",
                    notes=f"test_login exception: {e}")
        try: open_session.close()
        except Exception: pass
        return stages

    # ── WAF challenge in POST response → bypass + fresh token in parallel ────
    if _is_waf_challenge(body):
        import solver as sol
        print(f"[{username}] WAF challenge in login POST — starting solver + bypass in parallel")

        # Start fresh token solve IMMEDIATELY (WAF consumed the original token).
        # Bypass (Variant A PoW) takes ~2s; solver race runs concurrently.
        # Goal: fresh token ready within ~30s bypass TTL window.
        fresh_token_result: list[str] = []
        fresh_token_error:  list[str] = []

        def _solve_fresh_token():
            try:
                # min_score=50: capsolver gives ~55 in 3-6s; capmonster gives 25-40
                # which the portal rejects. Filtering at 50 accepts capsolver while
                # rejecting capmonster's low-score tokens.
                t = sol.race_all(
                    capsolver_keys, anticaptcha_keys, twocaptcha_keys, capmonster_keys,
                    "LOGIN_EVISA", proxy=None, min_score=50,
                )
                fresh_token_result.append(t)
            except Exception as e:
                fresh_token_error.append(str(e))

        solver_thread = threading.Thread(target=_solve_fresh_token, daemon=True)
        solver_thread.start()

        bypassed = _waf_bypass(open_session, body, username)
        if not bypassed:
            solver_thread.join(timeout=1)
            print(f"[{username}] RESULT: WAF bypass failed")
            stages["result"] = "waf_bypass_fail"
            _csv_update(accounts_file, username, status="login_failed", notes="waf_bypass_fail")
            try: open_session.close()
            except Exception: pass
            return stages

        # Check if Variant B WAF replay already logged us in (page navigated to HOME)
        try:
            cur_url = open_session.browser_eval("() => location.href", timeout=5)
            if cur_url and "Authentication.jsp" not in cur_url and "/ch/" not in cur_url:
                print(f"[{username}] Variant B WAF replay — page at {cur_url} — checking session")
                if "VistosOnline" in cur_url and "Authentication" not in cur_url:
                    print(f"[{username}] RESULT: LOGIN OK via WAF replay  url={cur_url}")
                    stages["result"] = "active"
                    _csv_update(accounts_file, username, status="active", last_login=_now())
                    solver_thread.join(timeout=1)
                    try: open_session.close()
                    except Exception: pass
                    return stages
        except Exception:
            pass  # page may still be loading after nav

        # Wait for fresh token (join max 25s to stay within ~30s bypass TTL)
        solver_thread.join(timeout=25)
        if fresh_token_result:
            retry_token = fresh_token_result[0]
            print(f"[{username}] WAF bypassed — fresh token ready  "
                  f"len={len(retry_token)}  score={sol.score_token(retry_token)}")
        else:
            retry_token = orig_token  # fallback: original (likely consumed, but try)
            print(f"[{username}] WAF bypassed — fresh solver slow, using original token  "
                  f"len={len(retry_token)}  score={sol.score_token(retry_token)}"
                  f"  solver_err={fresh_token_error}")

        login_data = {"username": username, "password": password,
                      "language": lang, "rgpd": "Y", "captchaResponse": retry_token}
        try:
            retry_result = open_session.browser_fetch(LOGIN_URL, data=login_data, timeout=90)
            body   = (retry_result.get("body") or "").strip()
            status = retry_result.get("status", 0)
            print(f"[{username}] retry login: status={status}  waf={_is_waf_challenge(body)}  resp={body[:200]}")
            stages["login"] = f"waf_bypass+retry(status={status})"
        except Exception as e:
            print(f"[{username}] retry login EXCEPTION: {e}")
            stages["result"] = "error"
            _csv_update(accounts_file, username, status="login_failed",
                        notes=f"waf_bypass retry exception: {e}")
            try: open_session.close()
            except Exception: pass
            return stages

        if _is_waf_challenge(body):
            # Variant B bypass often converts WAF to Variant A on the next attempt.
            # Check if the new challenge is parseable (Variant A) and try one more bypass.
            print(f"[{username}] WAF body len={len(body)} after first bypass")
            challenge2 = _parse_waf_challenge(body)
            if challenge2:
                print(f"[{username}] Variant B→A downgrade — starting second Variant A bypass + fresh solver")
                fresh2_result: list[str] = []

                def _solve_fresh2():
                    try:
                        t = sol.race_all(
                            capsolver_keys, anticaptcha_keys, twocaptcha_keys, capmonster_keys,
                            "LOGIN_EVISA", proxy=None, min_score=50,
                        )
                        fresh2_result.append(t)
                    except Exception:
                        pass

                solver2_thread = threading.Thread(target=_solve_fresh2, daemon=True)
                solver2_thread.start()

                bypassed2 = _waf_bypass(open_session, body, username)
                solver2_thread.join(timeout=20)

                if bypassed2 and fresh2_result:
                    retry2_token = fresh2_result[0]
                    print(f"[{username}] second bypass done — retry2 token len={len(retry2_token)}"
                          f"  score={sol.score_token(retry2_token)}")
                    login_data2 = {"username": username, "password": password,
                                   "language": lang, "rgpd": "Y",
                                   "captchaResponse": retry2_token}
                    try:
                        r2 = open_session.browser_fetch(LOGIN_URL, data=login_data2, timeout=90)
                        body   = (r2.get("body") or "").strip()
                        status = r2.get("status", 0)
                        print(f"[{username}] retry2: status={status}  waf={_is_waf_challenge(body)}"
                              f"  resp={body[:200]}")
                        stages["login"] = f"waf_bypass2+retry2(status={status})"
                    except Exception as e:
                        print(f"[{username}] retry2 EXCEPTION: {e}")
                        stages["result"] = "error"
                        _csv_update(accounts_file, username, status="login_failed",
                                    notes=f"retry2 exception: {e}")
                        try: open_session.close()
                        except Exception: pass
                        return stages
                else:
                    print(f"[{username}] second bypass failed or no token (bypassed2={bypassed2}"
                          f"  token_ready={bool(fresh2_result)})")

        if _is_waf_challenge(body):
            print(f"[{username}] RESULT: WAF challenge persists after all bypasses")
            stages["result"] = "waf_challenge"
            _csv_update(accounts_file, username, status="login_failed", notes="waf_challenge_after_bypass")
            try: open_session.close()
            except Exception: pass
            return stages

    if not body:
        print(f"[{username}] RESULT: empty body — treating as transient, keeping verified")
        stages["result"] = "empty_body"
        import re as _re
        cur_notes = acct.get("notes", "") or ""
        m = _re.search(r"err_attempts:(\d+)", cur_notes)
        err_n = int(m.group(1)) + 1 if m else 1
        new_notes = (_re.sub(r"err_attempts:\d+", "", cur_notes).strip() + f" err_attempts:{err_n}").strip()
        if err_n >= 5:
            _csv_update(accounts_file, username, status="login_failed", notes=new_notes + " PERSISTENT")
        else:
            _csv_update(accounts_file, username, notes=new_notes)
        try: open_session.close()
        except Exception: pass
        return stages

    try:
        resp  = json.loads(body)
        rtype = resp.get("type", "__missing__")
    except json.JSONDecodeError:
        print(f"[{username}] RESULT: non-JSON body (HTML?) — treating as transient, keeping verified  {body[:120]}")
        stages["result"] = "non_json"
        import re as _re
        cur_notes = acct.get("notes", "") or ""
        m = _re.search(r"err_attempts:(\d+)", cur_notes)
        err_n = int(m.group(1)) + 1 if m else 1
        new_notes = (_re.sub(r"err_attempts:\d+", "", cur_notes).strip() + f" err_attempts:{err_n}").strip()
        if err_n >= 5:
            _csv_update(accounts_file, username, status="login_failed", notes=new_notes + " PERSISTENT")
        else:
            _csv_update(accounts_file, username, notes=new_notes)
        try: open_session.close()
        except Exception: pass
        return stages

    if status == 200 and rtype in ("", "200", "success"):
        print(f"[{username}] RESULT: LOGIN OK  exit_ip={exit_ip_used}")
        stages["result"] = "active"
        _csv_update(accounts_file, username, status="active",
                    last_login=_now(), proxy=used_proxy.split("@")[-1] if "@" in used_proxy else used_proxy)
        if save_state:
            import session_store
            _SESSIONS_DIR.mkdir(exist_ok=True)
            _state_path = _SESSIONS_DIR / f"{username}.playwright.json"
            try:
                open_session.save_state(_state_path)
                session_store.save(username, open_session.client, used_proxy)
                print(f"[{username}] session state saved → {_state_path.name}")
            except Exception as e:
                print(f"[{username}] WARN save_state: {e}")
    else:
        print(f"[{username}] RESULT: login rejected  type={rtype!r}  status={status}  body={body[:200]}")
        stages["result"] = f"rejected(type={rtype!r},status={status})"
        if rtype == "error":
            # Transient portal error; keep verified up to 5 attempts.
            import re as _re
            cur_notes = acct.get("notes", "") or ""
            m = _re.search(r"err_attempts:(\d+)", cur_notes)
            err_n = int(m.group(1)) + 1 if m else 1
            new_notes = (_re.sub(r"err_attempts:\d+", "", cur_notes).strip() + f" err_attempts:{err_n}").strip()
            if err_n >= 5:
                print(f"[{username}] err_attempts={err_n} — marking login_failed (persistent)")
                _csv_update(accounts_file, username, status="login_failed",
                            notes=new_notes + " PERSISTENT")
            else:
                print(f"[{username}] err_attempts={err_n}/5 — keeping verified for retry")
                _csv_update(accounts_file, username, notes=new_notes)
        else:
            _csv_update(accounts_file, username, status="login_failed",
                        notes=f"rejected type={rtype!r} status={status}")

    try: open_session.close()
    except Exception: pass
    return stages


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(description="Diagnostic SOAX login test")
    parser.add_argument("--mode",             default="test")
    parser.add_argument("--count",            type=int, default=1,
                        help="Number of accounts to attempt (default: 1)")
    parser.add_argument("--min-captcha-score", type=int, default=30,
                        dest="min_captcha_score",
                        help="Abort if avg solver pre-screen score < N (default: 30)")
    parser.add_argument("--use-checkbox",     action="store_false", dest="skip_checkbox",
                        help="Click reCAPTCHA checkbox + race_best x3 (min_score=85). "
                             "Default: skip checkbox, use race_all (1 race, faster)")
    parser.add_argument("--min-score",        type=int, default=50, dest="min_score",
                        help="Minimum reCAPTCHA token score to accept (default: 50). "
                             "Use 80+ to wait for higher-quality tokens.")
    parser.set_defaults(skip_checkbox=True)
    parser.add_argument("--headed",           action="store_true",
                        help="Show browser window")
    parser.add_argument("--save-state",       action="store_true", dest="save_state",
                        help="Save Playwright + primp session state on successful login")
    parser.add_argument("--skip-captcha-health", action="store_true", dest="skip_captcha_health",
                        help="Skip Phase 0 solver health check")
    parser.add_argument("--proxy-search-limit", type=int, default=25,
                        dest="proxy_search_limit",
                        help="Max SOAX proxies to screen per account (default: 25)")
    parser.add_argument("--regen-only",         action="store_true", dest="regen_only",
                        help="Only pick accounts whose notes field contains 'regen' "
                             "(skips older accounts that lack re-registration notes)")
    parser.add_argument("--err-retry",           action="store_true", dest="err_retry",
                        help="Only pick accounts whose notes field contains 'err_attempts' "
                             "(retry accounts that previously got type:error)")
    parser.add_argument("--proxy-type",          default="soax", dest="proxy_type",
                        choices=["soax", "webshare"],
                        help="Proxy provider to use (default: soax)")
    parser.add_argument("--concurrency",         type=int, default=1, dest="concurrency",
                        help="Parallel accounts (default: 1 = sequential)")
    args = parser.parse_args()

    env = _load_env()
    capsolver_keys   = _keys(env, "CAPSOLVER_KEYS")
    anticaptcha_keys = _keys(env, "ANTICAPTCHA_KEYS")
    twocaptcha_keys  = _keys(env, "TWOCAPTCHA_KEYS")
    capmonster_keys  = _keys(env, "CAPMONSTER_KEYS")

    if not any([capsolver_keys, anticaptcha_keys, twocaptcha_keys, capmonster_keys]):
        print("ERROR: no CAPTCHA solver keys found in .env")
        sys.exit(1)

    print(f"[test_login_soax] solvers: capsolver={len(capsolver_keys)} "
          f"anticaptcha={len(anticaptcha_keys)} 2captcha={len(twocaptcha_keys)} "
          f"capmonster={len(capmonster_keys)}")

    # Accounts file
    import mode_config
    cfg = mode_config.get_mode_cfg(env, args.mode)
    accounts_file = Path(cfg["accounts_file"])

    import csv
    with open(accounts_file, encoding="utf-8") as f:
        all_accounts = list(csv.DictReader(f))

    candidates = [a for a in all_accounts if a["status"] == "verified"]
    if args.regen_only:
        candidates = [a for a in candidates if "regen" in a.get("notes", "")]
        print(f"[test_login_soax] --regen-only: filtered to {len(candidates)} regen accounts")
    if args.err_retry:
        candidates = [a for a in candidates if "err_attempts" in a.get("notes", "")]
        print(f"[test_login_soax] --err-retry: filtered to {len(candidates)} err_attempts accounts")
    if not candidates:
        print(f"ERROR: no verified accounts in {accounts_file}")
        sys.exit(1)

    accounts = candidates[: args.count]
    login_mode = "skip_checkbox+race_all(default)" if args.skip_checkbox else "checkbox+race_best(x3)"
    print(f"[test_login_soax] mode={args.mode}  accounts={len(accounts)}  "
          f"proxy={args.proxy_type}  login_mode={login_mode}  min_score={args.min_score}")
    print(f"[test_login_soax] accounts file: {accounts_file}")
    for a in accounts:
        print(f"  {a['username']}  ({a['nationality']} / {a['traveldoc']})")

    # ── Phase 0: CAPTCHA solver health check ──────────────────────────────────
    if not args.skip_captcha_health:
        scores = _phase0_captcha_health(
            capsolver_keys, anticaptcha_keys, twocaptcha_keys, capmonster_keys,
        )
        if not scores:
            print("\n[test_login_soax] ABORT: solver health check — all 3 tokens failed")
            sys.exit(1)
        avg = sum(scores) / len(scores)
        if avg < args.min_captcha_score:
            print(f"\n[test_login_soax] ABORT: avg solver score {avg:.0f} < "
                  f"threshold {args.min_captcha_score} — solver quality too low to proceed")
            sys.exit(1)
        print(f"[test_login_soax] solver health OK  avg_score={avg:.0f}")
    else:
        print("[test_login_soax] skipping solver health check (--skip-captcha-health)")

    # ── Phase 1: login attempts ────────────────────────────────────────────────
    from proxy_pool import PersistentProxyPool
    _proxy_file = _WEBSHARE_FILE if args.proxy_type == "webshare" else _SOAX_FILE
    soax_pool = PersistentProxyPool(_proxy_file)
    print(f"\n[test_login_soax] {args.proxy_type.upper()} pool: cursor={soax_pool.cursor}  "
          f"used={soax_pool.used_count}/{soax_pool.total}")

    all_stages = []
    workers = min(args.concurrency, len(accounts))

    def _run(acct):
        print(f"\n{'='*60}")
        print(f"[test_login_soax] ACCOUNT: {acct['username']}")
        print(f"{'='*60}")
        return _login_one(
            acct, accounts_file,
            capsolver_keys, anticaptcha_keys, twocaptcha_keys, capmonster_keys,
            soax_pool,
            skip_checkbox=args.skip_checkbox,
            min_score=args.min_score,
            headed=args.headed,
            save_state=args.save_state,
            proxy_search_limit=args.proxy_search_limit,
        )

    if workers <= 1:
        all_stages = [_run(a) for a in accounts]
    else:
        print(f"[test_login_soax] concurrency={workers}")
        with ThreadPoolExecutor(max_workers=workers) as ex:
            futures = {ex.submit(_run, a): a for a in accounts}
            for fut in as_completed(futures):
                try:
                    all_stages.append(fut.result())
                except Exception as e:
                    acct = futures[fut]
                    print(f"[test_login_soax] WORKER ERROR {acct['username']}: {e}")
                    all_stages.append({"account": acct["username"], "proxy_screen": "error",
                                       "exit_ip": "", "get_session": "error",
                                       "login": "error", "result": "worker_error"})

    # ── Summary ───────────────────────────────────────────────────────────────
    print(f"\n{'='*60}")
    print("[test_login_soax] SUMMARY")
    print(f"{'='*60}")
    result_counts: dict[str, int] = {}
    for s in all_stages:
        r = s["result"]
        result_counts[r] = result_counts.get(r, 0) + 1
        print(f"  {s['account']:<20} proxy={s['proxy_screen']:<25} "
              f"exit={s['exit_ip']:<18} result={s['result']}")

    print(f"\n  Totals: {dict(sorted(result_counts.items()))}")
    print(f"{'='*60}")


if __name__ == "__main__":
    main()
