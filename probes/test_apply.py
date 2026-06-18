"""
End-to-end login → apply diagnostic script (test mode).

Picks one verified account from test_accounts.csv, logs in via SOAX,
then runs the full apply workflow: questionnaire → form → schedule → slots → PDF.

Usage:
  uv run python test_apply.py
  uv run python test_apply.py --count 3
  uv run python test_apply.py --account <username>
"""

import argparse
import asyncio
import csv
import hashlib
import json
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

BASE      = "https://pedidodevistos.mne.gov.pt"
HOME_URL  = BASE + "/VistosOnline/"
AUTH_URL  = BASE + "/VistosOnline/Authentication.jsp"
LOGIN_URL = BASE + "/VistosOnline/login"

_HERE = Path(__file__).parent
_ENV_FILE = _HERE / ".env"
_SOAX_FILE = _HERE / "data" / "proxies_soax.txt"

# ── env / key helpers ──────────────────────────────────────────────────────────

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

def _keys(env, k):
    return [x.strip() for x in env.get(k, "").split(",") if x.strip()]


# ── WAF helpers (copied from test_login_soax.py) ──────────────────────────────

def _is_waf_challenge(html: str) -> bool:
    return bool(html and ("/ch/bd.js" in html or '"/ch/' in html))

def _parse_waf_challenge(html: str):
    m = re.search(r'\{[^}]*"nonce"\s*:\s*"([^"]+)"[^}]*"token"\s*:\s*"([^"]+)"[^}]*"difficulty"\s*:\s*(\d+)', html)
    if not m:
        m = re.search(r'\{[^}]*"token"\s*:\s*"([^"]+)"[^}]*"nonce"\s*:\s*"([^"]+)"[^}]*"difficulty"\s*:\s*(\d+)', html)
        if m:
            return {"nonce": m.group(2), "token": m.group(1), "difficulty": int(m.group(3))}
    if m:
        return {"nonce": m.group(1), "token": m.group(2), "difficulty": int(m.group(3))}
    return None

def _pow_solve(nonce: str, difficulty: int) -> int:
    for proof in range(10_000_000):
        h = hashlib.sha256(f"{nonce}{proof}".encode()).hexdigest()
        if h.startswith("0" * difficulty):
            return proof
    raise RuntimeError("PoW exhausted")

def _waf_bypass(session, html: str, label: str) -> bool:
    import session as sess
    challenge = _parse_waf_challenge(html)
    if challenge:
        nonce = challenge["nonce"]
        token = challenge["token"]
        diff  = challenge["difficulty"]
        print(f"[waf] {label}: Variant A — solving PoW difficulty={diff} nonce={nonce[:8]}...")
        proof = _pow_solve(nonce, diff)
        print(f"[waf] {label}: proof={proof} — submitting to /ch/v")
        r = session.client.post(BASE + "/ch/v",
                                data={"nonce": nonce, "token": token, "proof": str(proof)},
                                headers={"Content-Type": "application/x-www-form-urlencoded"},
                                timeout=10)
        ok = r.status_code == 200 and "ok" in r.text
        print(f"[waf] {label}: /ch/v status={r.status_code}  ok={ok}  body={r.text[:80]}")
        return ok
    else:
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
            print(f"[waf] {label}: Variant B load note: {e}")
        time.sleep(14)
        try:
            session.browser_nav(AUTH_URL, timeout=45)
            print(f"[waf] {label}: re-navigated to AUTH_URL")
            return True
        except Exception as e:
            print(f"[waf] {label}: AUTH_URL re-nav failed: {e}")
            return False


# ── Login (SOAX, WAF bypass + CAPTCHA) ────────────────────────────────────────

def _login_with_soax(acct: dict, soax_pool, capsolver_keys, anticaptcha_keys,
                     twocaptcha_keys, capmonster_keys, proxy_search_limit=25):
    import session as sess
    import solver as sol

    username = acct["username"]
    password = acct["password"]

    print(f"\n[{username}] --- PROXY SCREEN ---")
    open_session = None
    used_proxy = None

    for attempt in range(1, proxy_search_limit + 1):
        proxy_url = soax_pool.advance()
        host = proxy_url.split("@")[-1] if "@" in proxy_url else proxy_url
        print(f"[{username}] proxy attempt {attempt}/{proxy_search_limit}: {host}")
        try:
            s = sess.get_session(proxy_url, headless=True)
        except Exception as e:
            print(f"[{username}]   get_session FAIL: {e}")
            continue
        try:
            current_url = s.browser_eval("() => location.href", timeout=10)
            page_html   = s.browser_eval("() => document.documentElement.innerHTML", timeout=10)
        except Exception as e:
            print(f"[{username}]   browser_eval FAIL: {e}")
            try: s.close()
            except Exception: pass
            continue
        if "/ch/" in (current_url or "") or _is_waf_challenge(page_html or ""):
            print(f"[{username}]   WAF challenge — skip")
            try: s.close()
            except Exception: pass
            continue
        try:
            exit_ip = s.browser_eval(
                "async () => { const r = await fetch('https://httpbin.org/ip'); const d = await r.json(); return d.origin; }",
                timeout=15)
        except Exception:
            exit_ip = host
        print(f"[{username}]   PASS  exit_ip={exit_ip}")
        open_session = s
        used_proxy = proxy_url
        break

    if open_session is None:
        print(f"[{username}] FAIL: no working proxy")
        return None

    print(f"\n[{username}] --- LOGIN ---")
    solver_key     = capsolver_keys[0]  if capsolver_keys   else ""
    twocaptcha_key = twocaptcha_keys[0] if twocaptcha_keys  else ""
    try:
        result = open_session.browser_login(
            username, password, lang="PT",
            solver_key=solver_key,
            twocaptcha_key=twocaptcha_key,
            anticaptcha_keys=anticaptcha_keys,
            twocaptcha_keys=twocaptcha_keys,
            capmonster_keys=capmonster_keys,
            skip_checkbox=True,
            min_score=50,
        )
        body   = (result.get("body") or "").strip()
        status = result.get("status", 0)
        orig_token = result.get("captcha_token", "")
        print(f"[{username}] login raw: status={status}  waf={_is_waf_challenge(body)}  resp={body[:200]}")
    except Exception as e:
        print(f"[{username}] login EXCEPTION: {e}")
        try: open_session.close()
        except Exception: pass
        return None

    # WAF on login POST → bypass + retry
    if _is_waf_challenge(body):
        print(f"[{username}] WAF on login POST — bypassing")
        fresh_result: list[str] = []
        def _solve():
            try:
                t = sol.race_all(capsolver_keys, anticaptcha_keys, twocaptcha_keys,
                                 capmonster_keys, "LOGIN_EVISA", proxy=None, min_score=50)
                fresh_result.append(t)
            except Exception: pass
        t = threading.Thread(target=_solve, daemon=True)
        t.start()
        bypassed = _waf_bypass(open_session, body, username)
        t.join(timeout=25)
        if not bypassed:
            print(f"[{username}] WAF bypass failed")
            try: open_session.close()
            except Exception: pass
            return None
        retry_token = fresh_result[0] if fresh_result else orig_token
        login_data = {"username": username, "password": password,
                      "language": "PT", "rgpd": "Y", "captchaResponse": retry_token}
        retry_r = open_session.browser_fetch(LOGIN_URL, data=login_data, timeout=90)
        body   = (retry_r.get("body") or "").strip()
        status = retry_r.get("status", 0)
        print(f"[{username}] retry login: status={status}  resp={body[:200]}")

    try:
        rtype = json.loads(body).get("type", "?")
    except Exception:
        rtype = "non-json"

    # type:error is transient — retry once with a fresh CAPTCHA token (bypass cookie still valid)
    if status == 200 and rtype == "error":
        print(f"[{username}] type:error — retrying with fresh token (transient)")
        import solver as sol
        try:
            retry_token = sol.race_all(capsolver_keys, anticaptcha_keys, twocaptcha_keys,
                                       capmonster_keys, "LOGIN_EVISA", proxy=None, min_score=50)
        except Exception as e:
            print(f"[{username}] retry token FAIL: {e}")
            retry_token = ""
        if retry_token:
            login_data2 = {"username": username, "password": password,
                           "language": "PT", "rgpd": "Y", "captchaResponse": retry_token}
            retry_r2 = open_session.browser_fetch(LOGIN_URL, data=login_data2, timeout=90)
            body   = (retry_r2.get("body") or "").strip()
            status = retry_r2.get("status", 0)
            print(f"[{username}] type:error retry: status={status}  resp={body[:200]}")
            try:
                rtype = json.loads(body).get("type", "?")
            except Exception:
                rtype = "non-json"

    if status == 200 and rtype in ("", "200", "success"):
        print(f"[{username}] LOGIN OK")
        return open_session
    else:
        print(f"[{username}] LOGIN FAILED  type={rtype!r}")
        try: open_session.close()
        except Exception: pass
        return None


# ── Apply workflow ─────────────────────────────────────────────────────────────

async def _apply(acct: dict, session, posto_id: str, nationality: str,
                 residence: str, capsolver_keys, anticaptcha_keys,
                 twocaptcha_keys, capmonster_keys):
    from batch_apply import apply_one
    executor = ThreadPoolExecutor(max_workers=4)
    result = await apply_one(
        acct, posto_id,
        slot_manager=None,
        capsolver_keys=capsolver_keys,
        anticaptcha_keys=anticaptcha_keys,
        twocaptcha_keys=twocaptcha_keys,
        capmonster_keys=capmonster_keys,
        executor=executor,
        nationality=nationality,
        residence=residence,
        client=session,
    )
    return result


# ── Per-account runner ─────────────────────────────────────────────────────────

def _run_one(acct: dict, soax_pool, capsolver_keys, anticaptcha_keys,
             twocaptcha_keys, capmonster_keys, posto_id: str,
             nationality: str, residence: str) -> str:
    username = acct["username"]
    print(f"\n{'='*60}")
    print(f"[test_apply] ACCOUNT: {username}")
    print(f"{'='*60}")

    session = _login_with_soax(acct, soax_pool, capsolver_keys, anticaptcha_keys,
                                twocaptcha_keys, capmonster_keys)
    if session is None:
        return "login_failed"

    print(f"\n[{username}] --- APPLY WORKFLOW ---")
    try:
        result = asyncio.run(_apply(acct, session, posto_id, nationality, residence,
                                    capsolver_keys, anticaptcha_keys,
                                    twocaptcha_keys, capmonster_keys))
    except Exception as e:
        print(f"[{username}] apply EXCEPTION: {e}")
        result = "error"
    finally:
        try: session.close()
        except Exception: pass

    print(f"\n[{username}] RESULT: {result}")
    return result


# ── Resume from saved checkpoint ─────────────────────────────────────────────

def _run_resume(accounts: list[dict], capsolver_keys, anticaptcha_keys,
                twocaptcha_keys, capmonster_keys) -> dict:
    """
    For each account: load saved schedule_jsp checkpoint, verify session alive,
    then jump directly to /slots. Confirms server honours form state without
    re-running questionnaire/Formulario/ScheduleController.
    """
    import session_store
    import session as sess
    from batch_apply import SLOTS_URL, SCHED_JSP
    import solver as sol

    results = {}
    for acct in accounts:
        username = acct["username"]
        print(f"\n{'='*60}")
        print(f"[resume] ACCOUNT: {username}")
        print(f"{'='*60}")

        meta = session_store.load_meta(username)
        if not meta:
            print(f"[resume] {username}: no saved session — skip")
            results[username] = "no_checkpoint"
            continue

        checkpoint = meta.get("checkpoint", "login")
        saved_at   = meta.get("saved_at", 0)
        age_min    = (time.time() - saved_at) / 60
        print(f"[resume] {username}: checkpoint={checkpoint}  age={age_min:.1f}min")

        if checkpoint != "schedule_jsp":
            print(f"[resume] {username}: checkpoint is '{checkpoint}', not 'schedule_jsp' — skip")
            results[username] = "wrong_checkpoint"
            continue

        loaded = session_store.load(username)
        if not loaded:
            print(f"[resume] {username}: load failed")
            results[username] = "load_failed"
            continue
        client, proxy = loaded

        # Log what cookies were restored
        restored_cookies = client.get_cookies(session_store.COOKIES_URL)
        print(f"[resume] {username}: restored cookies: {list(restored_cookies.keys())}")

        # Probe /VistosOnline/ and log raw response details
        try:
            r_probe = client.get(session_store.COOKIES_URL,
                                 headers={**sess.HEADERS_NAV, "Sec-Fetch-Site": "same-origin"},
                                 timeout=15, follow_redirects=False)
            print(f"[resume] {username}: probe status={r_probe.status_code}  url={r_probe.url}  "
                  f"body_start={r_probe.text[:120]!r}")
        except Exception as e:
            print(f"[resume] {username}: probe exception: {e}")

        alive = session_store.is_alive(client)
        print(f"[resume] {username}: is_alive={alive}  proxy={proxy}")
        if not alive:
            print(f"[resume] {username}: session expired — cannot resume")
            results[username] = "session_expired"
            continue

        posto_id  = meta.get("posto_id", "")
        posto_pdf = meta.get("posto_pdf", posto_id)
        sched_url = SCHED_JSP + "?posto_id=" + posto_id

        # Verify Schedule.jsp is still accessible directly
        try:
            r_sched = client.get(sched_url, headers=sess.HEADERS_NAV, timeout=15)
            print(f"[resume] {username}: GET Schedule.jsp → {r_sched.status_code}  url={r_sched.url}")
            if r_sched.status_code != 200 or "Schedule.jsp" not in str(r_sched.url):
                print(f"[resume] {username}: Schedule.jsp not accessible — session may lack form state")
                results[username] = "schedule_inaccessible"
                continue
        except Exception as e:
            print(f"[resume] {username}: Schedule.jsp GET failed: {e}")
            results[username] = "error"
            continue

        # Solve CAPTCHA and POST /slots
        print(f"[resume] {username}: solving SCHEDULE_EVISA captcha")
        try:
            token = sol.race_all(capsolver_keys, anticaptcha_keys, twocaptcha_keys,
                                 capmonster_keys, "SCHEDULE_EVISA", proxy, min_score=50)
        except Exception as e:
            print(f"[resume] {username}: captcha failed: {e}")
            results[username] = "captcha_failed"
            continue

        try:
            r_slots = client.post(SLOTS_URL, params={"posto_id": posto_id},
                                  data={"posto_id": posto_id, "captcha": token},
                                  headers={**sess.HEADERS_XHR, "Referer": sched_url},
                                  timeout=30)
            print(f"[resume] {username}: /slots status={r_slots.status_code}  body={r_slots.text[:300]}")
            try:
                slots_data = r_slots.json().get("data", {})
                result = "no_slot" if not slots_data else f"slots_found:{len(slots_data)}"
            except Exception:
                result = "non_json"
            results[username] = result
        except Exception as e:
            print(f"[resume] {username}: /slots POST failed: {e}")
            results[username] = "error"

    return results


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(description="Login + apply diagnostic (test mode)")
    parser.add_argument("--account",   default="", help="Specific username to run")
    parser.add_argument("--count",     type=int, default=1)
    parser.add_argument("--mode",      default="test")
    parser.add_argument("--posto",     default="", help="Override posto_id (e.g. 3059)")
    parser.add_argument("--residence", default="", help="Override residence country (e.g. FRA)")
    parser.add_argument("--skip-captcha-health", action="store_true", dest="skip_health")
    parser.add_argument("--resume", action="store_true",
                        help="Skip login/warmup; restore from saved schedule_jsp checkpoint and jump to /slots")
    args = parser.parse_args()

    env = _load_env()
    capsolver_keys   = _keys(env, "CAPSOLVER_KEYS")
    anticaptcha_keys = _keys(env, "ANTICAPTCHA_KEYS")
    twocaptcha_keys  = _keys(env, "TWOCAPTCHA_KEYS")
    capmonster_keys  = _keys(env, "CAPMONSTER_KEYS")

    from mode_config import get_mode_cfg
    cfg = get_mode_cfg(env, args.mode)
    acct_file  = Path(cfg["accounts_file"])
    posto_id   = args.posto     or cfg["posto_id"]
    nationality = cfg["nationality"]
    residence  = args.residence or cfg["residence"]

    print(f"[test_apply] mode={args.mode}  posto={posto_id}  nat={nationality}  res={residence}")
    print(f"[test_apply] accounts: {acct_file}")

    rows = list(csv.DictReader(open(acct_file, encoding="utf-8")))

    if args.account:
        accounts = [r for r in rows if r["username"] == args.account]
        if not accounts:
            print(f"ERROR: account {args.account!r} not found")
            sys.exit(1)
    else:
        eligible = [r for r in rows if r["status"] in ("verified", "active")
                    and not r.get("appointment_ref", "")]
        # Prefer accounts with no prior failure notes (non-JSON, err_attempts) first
        def _priority(r):
            notes = r.get("notes", "") or ""
            if not notes.strip():
                return 0
            if "non-JSON" in notes or "non_json" in notes or "err_attempts" in notes:
                return 2
            return 1
        eligible.sort(key=_priority)
        accounts = eligible[:args.count]

    if not accounts:
        print("ERROR: no eligible accounts found")
        sys.exit(1)

    print(f"[test_apply] running {len(accounts)} account(s): {[a['username'] for a in accounts]}")

    from proxy_pool import PersistentProxyPool
    soax_pool = PersistentProxyPool(_SOAX_FILE)
    print(f"[test_apply] SOAX pool: cursor={soax_pool.cursor}  used={soax_pool.used_count}/{soax_pool.total}\n")

    if args.resume:
        # Override account selection: pick the N accounts with the most recent
        # schedule_jsp checkpoints, regardless of CSV order.
        import session_store
        all_usernames = {r["username"]: r for r in rows}
        checkpointed = []
        sessions_dir = Path(__file__).parent / "sessions"
        if sessions_dir.exists():
            for p in sessions_dir.glob("*.json"):
                uname = p.stem
                if uname not in all_usernames:
                    continue
                try:
                    meta = json.loads(p.read_text(encoding="utf-8"))
                    if meta.get("checkpoint") == "schedule_jsp":
                        checkpointed.append((meta.get("saved_at", 0), uname))
                except Exception:
                    pass
        checkpointed.sort(reverse=True)  # most recent first
        resume_accounts = [all_usernames[u] for _, u in checkpointed[:args.count]]
        if not resume_accounts:
            print("ERROR: no schedule_jsp checkpoints found — run without --resume first")
            sys.exit(1)
        print(f"[test_apply] resume: using {len(resume_accounts)} checkpointed account(s): "
              f"{[a['username'] for a in resume_accounts]}")
        results = _run_resume(resume_accounts, capsolver_keys, anticaptcha_keys,
                              twocaptcha_keys, capmonster_keys)
    else:
        results = {}
        for acct in accounts:
            r = _run_one(acct, soax_pool, capsolver_keys, anticaptcha_keys,
                         twocaptcha_keys, capmonster_keys, posto_id, nationality, residence)
            results[acct["username"]] = r

    print(f"\n{'='*60}")
    print("[test_apply] SUMMARY")
    print(f"{'='*60}")
    for u, r in results.items():
        print(f"  {u:<20} {r}")
    from collections import Counter
    print(f"\n  Totals: {dict(Counter(results.values()))}")
    print(f"{'='*60}")


if __name__ == "__main__":
    main()
