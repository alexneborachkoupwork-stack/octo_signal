"""
Diagnostic: attempt login HEADED (headless=False) with Webshare proxy.
If at_home=True  → headless mode was the detection signal, headed works
If at_home=False → something else leaks (timing, CDP, TLS)
"""
import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from app.core.session import get_session
import os

CAPSOLVER_KEY = os.environ.get("CAPSOLVER_KEY", "")
if not CAPSOLVER_KEY:
    print("ERROR: set CAPSOLVER_KEY env var")
    sys.exit(1)

# Use first AL proxy (cursor=0 → AL-1)
PROXY = "socks5://Mylist1234-AL-1:Saulo12345@p.webshare.io:80"
USERNAME = "niltav5616"
PASSWORD = "Saulo12345"

print(f"[diag] Starting HEADED session with Webshare proxy: {PROXY}")
try:
    sess = get_session(proxy=PROXY, headless=False)
    print("[diag] Session warmed up OK")
    result = sess.browser_login(USERNAME, PASSWORD, capsolver_keys=[CAPSOLVER_KEY])
    print(f"[diag] Login result: {result}")
except Exception as e:
    print(f"[diag] Error: {e}")
    import traceback; traceback.print_exc()
