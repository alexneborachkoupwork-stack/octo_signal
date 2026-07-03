"""
Diagnostic: test primp-backed lang POST + quest steps after headed browser login.
This validates that primp with browser cookies bypasses Cloudflare WAF on API calls.

Usage:
  $env:CAPSOLVER_KEY="your-key"; uv run python probes/diag_primp_warmup.py
"""
import sys, pathlib, os, re
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "app" / "core"))

from session import get_session

CAPSOLVER_KEY = os.environ.get("CAPSOLVER_KEY", os.environ.get("CAPSOLVER_KEYS", "").split(",")[0].strip())
if not CAPSOLVER_KEY:
    # Load from .env directly
    env_file = pathlib.Path(__file__).resolve().parents[1] / "app" / "core" / ".env"
    for line in env_file.read_text().splitlines():
        if line.startswith("CAPSOLVER_KEYS="):
            CAPSOLVER_KEY = line.split("=", 1)[1].strip().split(",")[0].strip()
            break
if not CAPSOLVER_KEY:
    print("ERROR: set CAPSOLVER_KEY env var")
    sys.exit(1)

PROXY    = "socks5://Mylist1234-AL-52:Saulo12345@p.webshare.io:80"
USERNAME = "niltav5616"
PASSWORD = "Saulo12345"
BASE     = "https://pedidodevistos.mne.gov.pt"
QUEST_NEXT = BASE + "/VistosOnline/Questionario/QuestionarioNext"
QUEST_URL  = BASE + "/VistosOnline/Questionario"

print(f"[1] Starting HEADED session: {PROXY}")
sess = get_session(proxy=PROXY, headless=False)
print("[1] Session ready")

print("[2] Browser login...")
result = sess.browser_login(USERNAME, PASSWORD, capsolver_keys=[CAPSOLVER_KEY])
print(f"[2] Login result: {result}")
if not result.get("at_home"):
    print("[2] FAIL: at_home=False, aborting")
    sys.exit(1)

print("[3] Getting browser cookies...")
cookies = sess.get_context_cookies(BASE + "/VistosOnline/")
print(f"[3] Got {len(cookies)} cookies: {list(cookies.keys())}")

print("[4] Building primp client (SOCKS5 direct + impersonate=None + Chrome UA)...")
import primp
CHROME_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
             "AppleWebKit/537.36 (KHTML, like Gecko) "
             "Chrome/124.0.0.0 Safari/537.36")
print(f"[4] Using proxy: {sess.proxy}")
pc = primp.Client(impersonate=None, proxy=sess.proxy, verify=False, follow_redirects=True, timeout=30,
                  headers={"User-Agent": CHROME_UA, "Accept-Language": "pt-PT,pt;q=0.9,en-US;q=0.8,en;q=0.7"})
pc.set_cookies(BASE + "/VistosOnline/", cookies)
print("[4] primp client ready")

print("[5] lang POST via primp...")
lr = pc.post(BASE + "/VistosOnline/",
             headers={"Content-Type": "application/x-www-form-urlencoded"},
             data="lang=ENG&lang=PT")
print(f"[5] lang POST: status={lr.status_code}  len={len(lr.text)}  url={lr.url}")

print("[6] Quest step 1 via primp...")
qr = pc.get(f"{QUEST_NEXT}?lang=PT&nacionalidade=CPV&id_pergunta=21&resposta=1",
            headers={"Accept": "text/plain, */*; q=0.01", "X-Requested-With": "XMLHttpRequest", "Referer": QUEST_URL})
print(f"[6] Quest step 1: status={qr.status_code}  len={len(qr.text)}  snippet={qr.text[:80]}")
