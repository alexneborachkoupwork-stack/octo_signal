"""
Test different Webshare proxy URL formats to find what the user is using manually.
Some formats rotate IPs (fresh IP per connection), others give static IPs.
"""
import sys, time
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "app" / "core"))

from proxy_bridge import start_bridge
from playwright.sync_api import sync_playwright

HOME = "https://pedidodevistos.mne.gov.pt/VistosOnline/"
SS_DIR = ROOT / "app" / "core" / "screenshots"
SS_DIR.mkdir(parents=True, exist_ok=True)

# Different Webshare URL formats to try
# The -N suffix pins to a specific static IP.
# Without suffix, Webshare may give rotating residential IPs.
FORMATS = [
    # Rotating (no index) - same country prefix, no static IP
    ("rotating-RO",     "http://Mylist1234-RO:Saulo12345@p.webshare.io:80"),
    ("rotating-GB",     "http://Mylist1234-GB:Saulo12345@p.webshare.io:80"),
    ("rotating-US",     "http://Mylist1234-US:Saulo12345@p.webshare.io:80"),
    # No country (random geo)
    ("rotating-any",    "http://Mylist1234:Saulo12345@p.webshare.io:80"),
    # Webshare datacenter rotating endpoint (different product)
    ("dc-rotating",     "http://Mylist1234:Saulo12345@proxy.webshare.io:80"),
    ("dc-rotating-443", "http://Mylist1234:Saulo12345@proxy.webshare.io:443"),
]

for label, proxy_url in FORMATS:
    print(f"\n{'='*55}")
    print(f"[{label}]  {proxy_url}")
    try:
        bridge = start_bridge(proxy_url)
        print(f"  Bridge: {bridge}")
        with sync_playwright() as pw:
            browser = pw.chromium.launch(channel="chrome", headless=True,
                                          args=["--no-sandbox"])
            ctx = browser.new_context(proxy={"server": bridge},
                                      ignore_https_errors=True)
            page = ctx.new_page()
            try:
                resp = page.goto(HOME, timeout=20000, wait_until="domcontentloaded")
                time.sleep(1)
                body = page.content()
                title = page.title()
                has_err = "Não foi" in body or "could not be processed" in body.lower()
                has_form = 'name="username"' in body or 'type="password"' in body
                # Extract exit IP from error page if present
                ip_start = body.find("IP: ")
                exit_ip = body[ip_start+4:ip_start+20].split("<")[0].strip() if ip_start != -1 else "(not shown)"
                print(f"  Status: {resp.status if resp else 'N/A'}")
                print(f"  Title : {title!r}")
                print(f"  ExitIP: {exit_ip}")
                print(f"  Result: {'PASS - login form' if has_form else 'BLOCKED - error page' if has_err else 'UNKNOWN'}")
                ss = str(SS_DIR / f"diag_fmt_{label}.png")
                page.screenshot(path=ss)
                print(f"  Screenshot: {ss}")
            except Exception as e:
                print(f"  NAV ERROR: {e}")
            finally:
                browser.close()
    except Exception as e:
        print(f"  BRIDGE/LAUNCH ERROR: {e}")
