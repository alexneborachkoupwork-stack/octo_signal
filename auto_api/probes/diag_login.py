"""
One-shot login diagnostic: opens a browser, attempts login for one account,
prints the FULL response body and redirect URL from the /login endpoint.
Run: uv run python auto_api/diag_login.py
Uses test accounts already attempted (doesn't burn fresh ones).
"""
import sys, os
sys.path.insert(0, os.path.dirname(__file__))

from session import get_session, BASE, AUTH_URL, HOME_URL

# Use an already-attempted account from offset 33 batch
USERNAME = "clacun7129"
PASSWORD_FILE = os.path.join(os.path.dirname(__file__), "data/test_accounts.csv")

def get_password(username):
    import csv
    with open(PASSWORD_FILE, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            if row.get("username") == username:
                return row.get("password", "")
    raise ValueError(f"Account {username} not found")

import json as _json

# Hardcode a specific ISP proxy for this diagnostic
def get_isp_proxy():
    isp_file = os.path.join(os.path.dirname(__file__), "data/proxies_isp.txt")
    with open(isp_file) as f:
        lines = [l.strip() for l in f if l.strip() and not l.startswith("#")]
    # Try proxies starting from index 10 to skip any burned ones at start
    return lines[10] if len(lines) > 10 else lines[0]

def main():
    password = get_password(USERNAME)
    proxy = get_isp_proxy()
    print(f"[diag] proxy={proxy}")
    print(f"[diag] username={USERNAME}")

    s = get_session(proxy)
    print("[diag] session ready")

    # Minimal CAPTCHA solve - just capsolver
    import solver
    from dotenv import dotenv_values
    env = dotenv_values(os.path.join(os.path.dirname(__file__), ".env"))
    capsolver_keys = [k.strip() for k in env.get("CAPSOLVER_KEYS","").split(",") if k.strip()]
    anticaptcha_keys = [k.strip() for k in env.get("ANTICAPTCHA_KEYS","").split(",") if k.strip()]

    print("[diag] solving CAPTCHA...")
    try:
        token = solver.race_all(capsolver_keys, anticaptcha_keys, [], [], "LOGIN_EVISA", proxy=None, min_score=50)
        print(f"[diag] token obtained len={len(token)}")
    except Exception as e:
        print(f"[diag] CAPTCHA solve failed: {e}")
        s.close()
        return

    # Inject token and post login, capturing FULL response
    tok_js = _json.dumps(token)
    uname_js = _json.dumps(USERNAME)
    passwd_js = _json.dumps(password)
    login_url_js = _json.dumps(BASE + "/VistosOnline/login")

    # Test new form.submit() approach
    result = s.browser_login(
        USERNAME, password,
        capsolver_keys=capsolver_keys,
        anticaptcha_keys=anticaptcha_keys,
        skip_checkbox=True,
        min_score=50,
        timeout=120,
    )
    print(f"\n[diag] === LOGIN RESPONSE ===")
    print(f"[diag] status = {result.get('status')}")
    print(f"[diag] body   = {result.get('body')}")
    print(f"[diag] ========================\n")

    s.close()

if __name__ == "__main__":
    main()
