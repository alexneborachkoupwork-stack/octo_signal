"""Quick probe: test RO webshare proxies against portal HOME + AUTH."""
import sys, time
sys.path.insert(0, '.')

from app.core.proxy_bridge import start_bridge
from playwright.sync_api import sync_playwright
from playwright_stealth import Stealth
from pathlib import Path

HOME_URL = "https://pedidodevistos.mne.gov.pt/VistosOnline/"
AUTH_URL = "https://pedidodevistos.mne.gov.pt/VistosOnline/Authentication.jsp"

proxy_file = Path("app/core/data/proxies_webshare_RO_http.txt")
lines = [l.strip() for l in proxy_file.read_text().splitlines() if l.strip()]

LIMIT = 10
results = []

for proxy_url in lines[:LIMIT]:
    bridge = None
    try:
        bridge = start_bridge(proxy_url)
        with sync_playwright() as pw:
            browser = pw.chromium.launch(channel='chrome', headless=True, args=['--no-sandbox'])
            ctx = browser.new_context(proxy={'server': bridge}, ignore_https_errors=True, locale='pt-PT')
            page = ctx.new_page()
            Stealth().apply_stealth_sync(page)

            # HOME
            try:
                page.goto(HOME_URL, timeout=18000, wait_until='domcontentloaded')
                page.wait_for_load_state('networkidle', timeout=6000)
            except Exception as e:
                results.append(f"FAIL_HOME {proxy_url} {e}")
                browser.close()
                continue

            home_body = page.content()
            home_ban = 'foi' in home_body and 'concretizar' in home_body
            home_dd = '/ch/bd.js' in home_body
            home_len = len(home_body)

            if home_ban:
                results.append(f"HOME_BAN len={home_len} {proxy_url}")
                browser.close()
                continue

            # AUTH
            try:
                page.goto(AUTH_URL, timeout=18000, wait_until='domcontentloaded')
                page.wait_for_load_state('networkidle', timeout=6000)
            except Exception as e:
                results.append(f"FAIL_AUTH {proxy_url} {e}")
                browser.close()
                continue

            auth_body = page.content()
            auth_ban = 'foi' in auth_body and 'concretizar' in auth_body
            auth_dd = '/ch/bd.js' in auth_body
            has_form = 'name="username"' in auth_body

            tag = "PASS" if has_form else ("AUTH_BAN" if auth_ban else ("DATADOME" if auth_dd else "UNKNOWN"))
            results.append(f"{tag} home_len={home_len} has_form={has_form} auth_ban={auth_ban} dd={auth_dd} {proxy_url}")
            browser.close()

    except Exception as outer:
        results.append(f"OUTER_ERR {proxy_url} {outer}")
    finally:
        # no explicit bridge cleanup needed
        pass
    time.sleep(1)

out = Path("ro_proxy_results.txt")
out.write_text('\n'.join(results), encoding='utf-8')
print('\n'.join(results))
print(f"\nDone. {len(results)}/{LIMIT} tested.")
