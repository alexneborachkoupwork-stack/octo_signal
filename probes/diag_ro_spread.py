"""
Test evenly-spaced RO proxies (indices 1..4000, step 40 = 100 samples)
and also GB/ES/IT with same spread.
Identifies if higher-index proxies are unblocked.
"""
import sys, time
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "app" / "core"))

from proxy_bridge import start_bridge
from playwright.sync_api import sync_playwright
from playwright_stealth import Stealth
from concurrent.futures import ThreadPoolExecutor, as_completed
import threading

HOME = "https://pedidodevistos.mne.gov.pt/VistosOnline/"
AUTH = "https://pedidodevistos.mne.gov.pt/VistosOnline/Authentication.jsp"
SS_DIR = ROOT / "app" / "core" / "screenshots"
SS_DIR.mkdir(parents=True, exist_ok=True)

_stop = threading.Event()

def test_one(label, proxy_url):
    if _stop.is_set():
        return label, proxy_url, "SKIP"
    try:
        bridge = start_bridge(proxy_url)
        with sync_playwright() as pw:
            browser = pw.chromium.launch(channel="chrome", headless=True,
                                          args=["--no-sandbox"])
            ctx = browser.new_context(proxy={"server": bridge},
                                      user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36",
                                      ignore_https_errors=True)
            page = ctx.new_page()
            Stealth().apply_stealth_sync(page)
            try:
                resp = page.goto(HOME, timeout=25000, wait_until="domcontentloaded")
                try:
                    page.wait_for_load_state("networkidle", timeout=10000)
                except Exception:
                    pass
                time.sleep(0.5)
                body = page.content()
                status = resp.status if resp else 0
                ip_start = body.find("IP: ")
                exit_ip = body[ip_start+4:ip_start+20].split("<")[0].strip() if ip_start != -1 else ""
                has_err  = "Não foi" in body or "could not be processed" in body.lower()
                has_form = 'name="username"' in body or 'type="password"' in body
                if has_form:
                    return label, proxy_url, f"OK(form on home)"
                if has_err:
                    return label, proxy_url, f"BLOCKED(ip={exit_ip})"
                # Home loaded (200/Inicio) — navigate to AUTH to check login form
                title = page.title()
                if status == 200 and "foi possível" not in title:
                    try:
                        page.goto(AUTH, timeout=20000, wait_until="domcontentloaded")
                        try:
                            page.wait_for_load_state("networkidle", timeout=8000)
                        except Exception:
                            pass
                        body2 = page.content()
                        has_form2 = 'name="username"' in body2 or 'type="password"' in body2
                        has_err2  = "Não foi" in body2
                        ip2 = body2[body2.find("IP: ")+4:body2.find("IP: ")+20].split("<")[0].strip() if "IP: " in body2 else ""
                        if has_form2:
                            return label, proxy_url, f"OK(login form found)"
                        if has_err2:
                            return label, proxy_url, f"BLOCKED_AUTH(ip={ip2})"
                        return label, proxy_url, f"HOME_OK_AUTH_UNKNOWN(auth_title={page.title()!r})"
                    except Exception as e2:
                        return label, proxy_url, f"HOME_OK_AUTH_ERR({e2!s:.60})"
                return label, proxy_url, f"UNKNOWN(status={status} title={title!r})"
            except Exception as e:
                return label, proxy_url, f"TIMEOUT/ERR({type(e).__name__}: {str(e)[:60]})"
            finally:
                browser.close()
    except Exception as e:
        return label, proxy_url, f"BRIDGE_ERR({e})"


# Build test list: spread across full range of each geo
GEOS = {
    "RO": 4000,
    "GB": 4000,
    "ES": 4000,
    "IT": 4000,
}
STEP = 80   # every 80th index → 50 samples per geo × 4 geos = 200 total
PWD = "Saulo12345"
PREFIX = "Mylist1234"

tasks = []
for geo, max_idx in GEOS.items():
    for idx in range(1, max_idx + 1, STEP):
        user = f"{PREFIX}-{geo}-{idx}"
        url  = f"http://{user}:{PWD}@p.webshare.io:80"
        tasks.append((f"{geo}-{idx}", url))

print(f"Testing {len(tasks)} proxies spread across RO/GB/ES/IT (indices 1..max, step={STEP})")
print("Workers: 8\n")

ok = []
with ThreadPoolExecutor(max_workers=8) as ex:
    futs = {ex.submit(test_one, lbl, url): (lbl, url) for lbl, url in tasks}
    for fut in as_completed(futs):
        lbl, url, result = fut.result()
        if result == "SKIP":
            continue
        if result.startswith("OK"):
            ok.append(url)
            print(f"  PASS     {lbl:12s}  {result}")
            _stop.set()
        elif result.startswith("BLOCKED"):
            print(f"  BLOCKED  {lbl:12s}  {result}")
        else:
            print(f"  OTHER    {lbl:12s}  {result}")
        sys.stdout.flush()

print(f"\nDone. OK={len(ok)}")
