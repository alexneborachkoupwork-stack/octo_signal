"""
Quick scan to find proxies that currently tunnel to the portal without ERR_TUNNEL_CONNECTION_FAILED
or IP ban. Tests proxies in parallel (8 workers), stops at first 3 OK proxies.
"""
import sys, time, threading, argparse
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "app" / "core"))

from proxy_bridge import start_bridge
from playwright.sync_api import sync_playwright
from playwright_stealth import Stealth

HOME = "https://pedidodevistos.mne.gov.pt/VistosOnline/"
AUTH = "https://pedidodevistos.mne.gov.pt/VistosOnline/Authentication.jsp"
SS_DIR = ROOT / "app" / "core" / "screenshots"
SS_DIR.mkdir(parents=True, exist_ok=True)

_ap = argparse.ArgumentParser()
_ap.add_argument("--geo",   default="FR")
_ap.add_argument("--start", type=int, default=110)
_ap.add_argument("--end",   type=int, default=300)
_ap.add_argument("--want",  type=int, default=3)
_args = _ap.parse_args()

GEO = _args.geo

_stop = threading.Event()
OK_COUNT = 0
OK_LOCK = threading.Lock()
WANT = _args.want


def test_one(idx):
    global OK_COUNT
    if _stop.is_set():
        return idx, "SKIP"
    proxy_url = f"http://Mylist1234-{GEO}-{idx}:Saulo12345@p.webshare.io:80"
    try:
        bridge = start_bridge(proxy_url)
        with sync_playwright() as pw:
            browser = pw.chromium.launch(channel="chrome", headless=True, args=["--no-sandbox"])
            ctx = browser.new_context(proxy={"server": bridge}, ignore_https_errors=True,
                                       locale="pt-PT")
            page = ctx.new_page()
            Stealth().apply_stealth_sync(page)
            try:
                resp = page.goto(HOME, timeout=20000, wait_until="domcontentloaded")
                page.wait_for_load_state("networkidle", timeout=8000)
                body = page.content()
                has_ban = "Não foi" in body
                has_challenge = "/ch/bd.js" in body
                if has_ban:
                    return idx, f"HOME_BAN"
                if has_challenge:
                    return idx, "DD_CHALLENGE"
                # Navigate to AUTH
                resp2 = page.goto(AUTH, timeout=18000, wait_until="domcontentloaded")
                page.wait_for_load_state("networkidle", timeout=8000)
                body2 = page.content()
                title2 = page.title()
                has_ban2 = "Não foi" in body2
                has_form = 'name="username"' in body2
                if has_form:
                    with OK_LOCK:
                        OK_COUNT += 1
                        if OK_COUNT >= WANT:
                            _stop.set()
                    return idx, f"OK(form found)"
                if has_ban2:
                    ip_s = body2.find("IP: ")
                    ip = body2[ip_s+4:ip_s+20].split("<")[0].strip() if ip_s >= 0 else "?"
                    return idx, f"AUTH_BAN(ip={ip})"
                return idx, f"UNKNOWN(title={title2!r})"
            except Exception as e:
                return idx, f"NAV_ERR({type(e).__name__}: {str(e)[:60]})"
            finally:
                try: browser.close()
                except: pass
    except Exception as e:
        return idx, f"BRIDGE_ERR({e!s:.60})"


proxies_to_test = list(range(_args.start, _args.end + 1))
print(f"Testing {GEO}-{proxies_to_test[0]} to {GEO}-{proxies_to_test[-1]} ({len(proxies_to_test)} total), 8 parallel, want {WANT} OK\n")

ok = []
with ThreadPoolExecutor(max_workers=8) as ex:
    futs = {ex.submit(test_one, idx): idx for idx in proxies_to_test}
    for fut in as_completed(futs):
        idx = futs[fut]
        result_idx, result = fut.result()
        if result == "SKIP":
            continue
        sym = "PASS " if result.startswith("OK") else "BLOCK" if "BAN" in result else "OTHER"
        print(f"  {sym}  {GEO}-{result_idx:<5d}  {result}")
        sys.stdout.flush()
        if result.startswith("OK"):
            ok.append(result_idx)
        if _stop.is_set() and len(ok) >= WANT:
            break

print(f"\nFound {len(ok)} working proxies: {GEO}-{ok}")
