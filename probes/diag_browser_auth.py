"""
Navigate to the portal via real Chrome + Webshare proxy and screenshot every step.
Shows exactly what the browser sees.
"""
import sys, time
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "app" / "core"))

from proxy_bridge import start_bridge
from playwright.sync_api import sync_playwright

PROXY = "http://Mylist1234-RO-3:Saulo12345@p.webshare.io:80"
HOME  = "https://pedidodevistos.mne.gov.pt/VistosOnline/"
AUTH  = "https://pedidodevistos.mne.gov.pt/VistosOnline/Authentication.jsp"
SS_DIR = ROOT / "app" / "core" / "screenshots"
SS_DIR.mkdir(parents=True, exist_ok=True)

bridge = start_bridge(PROXY)
print(f"Bridge: {bridge}  →  {PROXY}")

with sync_playwright() as pw:
    browser = pw.chromium.launch(
        channel="chrome",
        headless=True,
        args=["--no-sandbox"],
    )
    ctx = browser.new_context(
        proxy={"server": bridge},
        user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36",
        ignore_https_errors=True,
    )
    page = ctx.new_page()
    from playwright_stealth import Stealth
    Stealth().apply_stealth_sync(page)

    # Step 1: HOME
    print(f"\n[1] Navigate to {HOME}")
    try:
        resp = page.goto(HOME, timeout=30000, wait_until="domcontentloaded")
        page.wait_for_load_state("networkidle", timeout=15000)
        print(f"    Status: {resp.status if resp else 'N/A'}")
        time.sleep(2)
        body1 = page.content()
        print(f"    Title : {page.title()!r}")
        print(f"    URL   : {page.url}")
        print(f"    DataDome challenge: {'/ch/bd.js' in body1}")
        print(f"    Body[400]: {body1[:400]!r}")
        ss1 = str(SS_DIR / "diag_home.png")
        page.screenshot(path=ss1)
        print(f"    Screenshot: {ss1}")
    except Exception as e:
        print(f"    ERROR: {e}")

    # Step 2: AUTH
    print(f"\n[2] Navigate to {AUTH}")
    try:
        resp2 = page.goto(AUTH, timeout=25000, wait_until="domcontentloaded")
        page.wait_for_load_state("networkidle", timeout=10000)
        print(f"    Status: {resp2.status if resp2 else 'N/A'}")
        time.sleep(1)
        body2 = page.content()
        print(f"    Title : {page.title()!r}")
        print(f"    URL   : {page.url}")
        has_form = 'name="username"' in body2 or 'type="password"' in body2
        has_err  = "Não foi" in body2
        print(f"    login_form: {has_form}  error_page: {has_err}")
        print(f"    Body[800]: {body2[:800]!r}")
        ss2 = str(SS_DIR / "diag_auth.png")
        page.screenshot(path=ss2)
        print(f"    Screenshot: {ss2}")
    except Exception as e:
        print(f"    ERROR: {e}")

    browser.close()
