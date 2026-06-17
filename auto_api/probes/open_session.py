"""
Open a saved login session in a headed browser.

Usage:
    uv run python open_session.py <username>

Behaviour:
- Loads saved cookies from data/sessions/<username>.json
- Opens a visible browser window
- Checks if the session is still valid (not expired)
- If expired: auto re-logins using credentials from accounts.csv, saves fresh cookies
- If valid: runs a keep-alive ping every 10 minutes to prevent the 30-min idle timeout
- Press Ctrl+C to close
"""
import csv
import sys
import time
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)

sys.path.insert(0, str(Path(__file__).parent))

import session as sess
from batch_login import _load_dotenv, HOME_URL

_DATA         = Path(__file__).parent / "data"
PING_INTERVAL = 10 * 60   # ping every 10 min — server's idle timeout is ~30 min


def _is_expired(url: str) -> bool:
    return "Authentication.jsp" in url or "/ch/" in url


def _load_account(uname: str) -> str:
    """Return password for username from accounts.csv."""
    with open(_DATA / "accounts.csv", newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            if row["username"] == uname:
                return row["password"]
    return ""


def main() -> None:
    if len(sys.argv) < 2:
        print("Usage: uv run python open_session.py <username>")
        sys.exit(1)

    uname      = sys.argv[1]
    state_path = _DATA / "sessions" / f"{uname}.json"

    if not state_path.exists():
        print(f"[open] No saved session found for '{uname}'")
        print(f"[open] Expected: {state_path}")
        print(f"[open] Run login_loop.py first to create a session.")
        sys.exit(1)

    env = _load_dotenv()
    def _keys(k): return [x.strip() for x in env.get(k, "").split(",") if x.strip()]

    print(f"[open] Loading session for {uname} from {state_path}")
    s = sess.get_session_from_state(state_path, headless=False)

    url = s.browser_nav(HOME_URL, timeout=30)
    print(f"[open] Landed at: {url}")

    if _is_expired(url):
        print(f"[open] Session expired — re-logging in with saved credentials")
        passwd = _load_account(uname)
        if not passwd:
            print(f"[open] ERROR: Could not find password for {uname} in accounts.csv")
            s.close()
            sys.exit(1)

        try:
            result = s.browser_login(
                uname, passwd, lang="PT",
                capsolver_keys=_keys("CAPSOLVER_KEYS"),
                anticaptcha_keys=_keys("ANTICAPTCHA_KEYS"),
                twocaptcha_keys=_keys("TWOCAPTCHA_KEYS"),
                capmonster_keys=_keys("CAPMONSTER_KEYS"),
                timeout=360,
            )
            body = (result.get("body") or "")
            print(f"[open] Re-login result: {body[:120]}")

            import json
            try:
                rtype = json.loads(body).get("type", "?")
            except Exception:
                rtype = "non-json"

            if result.get("status") == 200 and rtype in ("", "200", "success"):
                print(f"[open] Re-login OK — saving fresh cookies")
                s.save_state(state_path)
            else:
                print(f"[open] Re-login FAILED (type={rtype!r}) — session may not work")
        except Exception as e:
            print(f"[open] Re-login error: {e}")
    else:
        print(f"[open] Session valid — keeping alive with pings every {PING_INTERVAL // 60} min")

    print(f"[open] Browser is open. Press Ctrl+C to close.")
    try:
        while True:
            time.sleep(PING_INTERVAL)
            try:
                url = s.browser_nav(HOME_URL, timeout=15)
                if _is_expired(url):
                    print(f"[open] Session expired during keep-alive — stopping")
                    break
                print(f"[open] Keep-alive ping OK  url={url}")
            except Exception as ping_err:
                print(f"[open] Keep-alive ping failed: {ping_err}")
    except KeyboardInterrupt:
        print("\n[open] Ctrl+C received — closing browser")

    s.close()


if __name__ == "__main__":
    main()
