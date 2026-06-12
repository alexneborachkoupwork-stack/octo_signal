"""
Headed login loop: tries accounts one at a time until one succeeds.
On failure: closes browser immediately and retries with next account+proxy.
On success: keeps browser open for 10 minutes for manual inspection.
"""
import csv
import json
import sys
import time
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)

sys.path.insert(0, str(Path(__file__).parent))

import session as sess
import solver as solvermod
from proxy_pool import PersistentProxyPool
from batch_login import (
    _load_dotenv, _waf_bypass, _is_waf_challenge,
    _csv_update, _now, _proxy_host, LOGIN_URL, _screen_proxy,
)

_ACCOUNT_FILE  = Path(__file__).parent / "data" / "accounts.csv"
_ISP_PROXY_FILE = Path(__file__).parent / "data" / "proxies_isp.txt"

_used_this_session: set[str] = set()


def _fresh_accounts() -> list[dict]:
    with open(_ACCOUNT_FILE, newline="", encoding="utf-8") as f:
        return [r for r in csv.DictReader(f)
                if r["status"].strip() == "verified"
                and r["username"] not in _used_this_session]


def main() -> None:
    env = _load_dotenv()
    def _keys(k): return [x.strip() for x in env.get(k, "").split(",") if x.strip()]
    capsolver_keys   = _keys("CAPSOLVER_KEYS")
    anticaptcha_keys = _keys("ANTICAPTCHA_KEYS")
    twocaptcha_keys  = _keys("TWOCAPTCHA_KEYS")
    capmonster_keys  = _keys("CAPMONSTER_KEYS")
    print(f"[loop] solvers: capsolver={len(capsolver_keys)} anticaptcha={len(anticaptcha_keys)} "
          f"2captcha={len(twocaptcha_keys)} capmonster={len(capmonster_keys)}")

    pool = PersistentProxyPool(_ISP_PROXY_FILE)

    attempt = 0
    while True:
        accounts = _fresh_accounts()
        if not accounts:
            print("[loop] No fresh verified accounts remaining — stopping")
            break

        acct    = accounts[0]
        uname   = acct["username"]
        passwd  = acct["password"]
        _used_this_session.add(uname)
        attempt += 1

        # Pick a fresh proxy that passes WAF pre-screen (no account claimed until screen passes)
        proxy = exit_ip = None
        for _ in range(len(pool._proxies)):
            candidate = pool.advance()
            if candidate is None:
                break
            if not _screen_proxy(candidate):
                continue
            ip = pool.verify_and_claim(candidate, uname)
            if ip:
                proxy, exit_ip = candidate, ip
                break

        if not proxy:
            print("[loop] Proxy pool exhausted — stopping")
            break

        print(f"\n[loop] attempt={attempt}  account={uname}  exit_ip={exit_ip}")

        # Open headed browser session
        try:
            s = sess.get_session(proxy, headless=False)
        except Exception as e:
            print(f"[loop] session init failed: {e}")
            _csv_update(uname, status="login_failed", notes="session_init_error")
            continue

        # Browser login (types credentials, clicks checkbox, handles CAPTCHA)
        solver_key    = capsolver_keys[0]  if capsolver_keys  else ""
        twocaptcha_key = twocaptcha_keys[0] if twocaptcha_keys else ""
        body = ""
        status = 0
        try:
            result = s.browser_login(
                uname, passwd, lang="PT",
                solver_key=solver_key,
                twocaptcha_key=twocaptcha_key,
                capsolver_keys=capsolver_keys,
                anticaptcha_keys=anticaptcha_keys,
                twocaptcha_keys=twocaptcha_keys,
                capmonster_keys=capmonster_keys,
                timeout=360,
            )
            body   = (result.get("body") or "").strip()
            status = result.get("status", 0)
        except Exception as e:
            print(f"[loop] browser_login error: {e}")
            try: s.close()
            except Exception: pass
            continue

        # WAF PoW bypass + retry
        if _is_waf_challenge(body):
            print(f"[loop] WAF challenge — solving PoW")
            if not _waf_bypass(s, body, uname):
                print(f"[loop] WAF bypass failed — next account")
                _csv_update(uname, status="login_failed", notes="waf_bypass_failed")
                try: s.close()
                except Exception: pass
                continue

            print(f"[loop] WAF bypassed — fresh CAPTCHA token (2captcha only for score=100)")
            retry_token = None
            if twocaptcha_keys:
                try:
                    retry_token = solvermod.solve("2captcha", twocaptcha_keys, "LOGIN_EVISA", proxy=None)
                    score = solvermod.score_token(retry_token)
                    print(f"[loop] 2captcha retry token  len={len(retry_token)}  score={score}")
                except Exception as e:
                    print(f"[loop] 2captcha retry failed: {e}")
            if not retry_token:
                try:
                    retry_token = solvermod.race_best(
                        capsolver_keys, anticaptcha_keys, twocaptcha_keys, capmonster_keys,
                        "LOGIN_EVISA", proxy=None, n_races=3, min_score=85,
                    )
                except Exception as e:
                    print(f"[loop] race_best fallback failed: {e}")
                    try: s.close()
                    except Exception: pass
                    continue

            login_data = {"username": uname, "password": passwd,
                          "language": "PT", "rgpd": "Y", "captchaResponse": retry_token}
            try:
                result = s.browser_fetch(LOGIN_URL, data=login_data, timeout=60)
                body   = (result.get("body") or "").strip()
                status = result.get("status", 0)
            except Exception as e:
                print(f"[loop] WAF retry fetch failed: {e}")
                try: s.close()
                except Exception: pass
                continue

        # Determine success
        success = False
        rtype   = ""
        if body:
            try:
                rtype = json.loads(body).get("type", "__missing__")
                if status == 200 and rtype in ("", "200", "success", "redirect"):
                    success = True
            except json.JSONDecodeError:
                pass
        elif status == 0:
            # body-read may have failed due to page redirect — check URL
            try:
                cur_url = s._page_url() if hasattr(s, "_page_url") else ""
            except Exception:
                cur_url = ""

        if not success:
            print(f"[loop] FAIL  status={status}  type={rtype!r}  body={body[:80]!r}")
            _csv_update(uname, status="login_failed", notes="login_loop attempt")
            try: s.close()
            except Exception: pass
            continue

        # ── SUCCESS ──────────────────────────────────────────────────────────
        print(f"[loop] LOGIN OK  account={uname}  exit_ip={exit_ip}")
        print(f"[loop] Browser open for 10 minutes — inspect the session then press Ctrl+C to exit")
        _csv_update(uname, status="active", last_login=_now(), proxy=_proxy_host(proxy))
        # Save session state so it can be restored later without re-logging in
        try:
            state_dir = Path(__file__).parent / "data" / "sessions"
            state_dir.mkdir(exist_ok=True)
            s.save_state(state_dir / f"{uname}.json")
        except Exception as se:
            print(f"[loop] WARNING: save_state failed: {se}")
        try:
            time.sleep(600)
        except KeyboardInterrupt:
            print("\n[loop] Ctrl+C received — closing browser")
        try: s.close()
        except Exception: pass
        break


if __name__ == "__main__":
    main()
