"""
Find a working FR proxy and extract the portal's reCAPTCHA action parameter.
Tests proxies in a wide spread (301-5000) until one reaches the auth page,
then extracts the reCAPTCHA configuration.
"""
import sys, time
from pathlib import Path

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

# Test a spread of high-index FR proxies that haven't been used
import argparse
ap = argparse.ArgumentParser()
ap.add_argument("--geo", default="GB")
ap.add_argument("--start", type=int, default=500)
ap.add_argument("--end", type=int, default=4000)
ap.add_argument("--step", type=int, default=100)
args = ap.parse_args()

indices = list(range(args.start, args.end + 1, args.step))
print(f"Testing {args.geo}-{args.start} to {args.geo}-{args.end} (step={args.step}): {len(indices)} proxies\n")

for idx in indices:
    proxy_url = f"http://Mylist1234-{args.geo}-{idx}:Saulo12345@p.webshare.io:80"
    print(f"[{args.geo}-{idx}] {proxy_url}")
    try:
        bridge = start_bridge(proxy_url)
        with sync_playwright() as pw:
            browser = pw.chromium.launch(channel="chrome", headless=True, args=["--no-sandbox"])
            ctx = browser.new_context(proxy={"server": bridge}, ignore_https_errors=True, locale="pt-PT")
            page = ctx.new_page()
            Stealth().apply_stealth_sync(page)
            try:
                page.goto(HOME, timeout=20000, wait_until="domcontentloaded")
                page.wait_for_load_state("networkidle", timeout=8000)
                body = page.content()
                if "Não foi" in body:
                    print(f"  HOME_BAN")
                    browser.close()
                    continue
                if "/ch/bd.js" in body:
                    print(f"  DD_CHALLENGE")
                    browser.close()
                    continue

                page.goto(AUTH, timeout=18000, wait_until="domcontentloaded")
                page.wait_for_load_state("networkidle", timeout=8000)
                time.sleep(2)
                body2 = page.content()
                title2 = page.title()

                if "Não foi" in body2:
                    print(f"  AUTH_BAN")
                    browser.close()
                    continue
                if 'name="username"' not in body2 and 'type="password"' not in body2:
                    print(f"  UNKNOWN title={title2!r}")
                    browser.close()
                    continue

                print(f"  OK! title={title2!r} - extracting reCAPTCHA config...")

                # Extract reCAPTCHA configuration
                rcaptcha_info = page.evaluate("""
                () => {
                    const inline = Array.from(document.querySelectorAll('script:not([src])')).map(s => s.textContent).join('\\n');

                    // Find grecaptcha.render or enterprise.render calls
                    const renderMatches = inline.match(/grecaptcha[^;]{0,200}render[^;]{0,300}/g) || [];

                    // Find data-action attributes
                    const widgetDivs = Array.from(document.querySelectorAll('[data-sitekey],[data-action],[class*="g-recaptcha"]'));
                    const widgetInfo = widgetDivs.map(el => ({
                        tag: el.tagName, id: el.id,
                        sitekey: el.getAttribute('data-sitekey'),
                        action: el.getAttribute('data-action'),
                        callback: el.getAttribute('data-callback'),
                    }));

                    // Find action strings in script content
                    const actionMatches = inline.match(/['"](action|action_name)['"]\s*[:=]\s*['"]([^'"]{1,50})['"]/g) || [];
                    const actionValues = inline.match(/action\s*:\s*['"]([^'"]{1,50})['"]/g) || [];

                    // Find sitekey
                    const sitekeyMatches = inline.match(/['"]\w{6}[A-Za-z0-9_-]{30,50}['"]/g) || [];

                    // Look for the captcha callback function body
                    const cbMatch = inline.match(/function\s+onCaptchaSuccess[^}]{0,500}/);

                    // reCAPTCHA Enterprise execute calls
                    const executeMatches = inline.match(/execute\s*\([^)]{0,200}\)/g) || [];

                    // Find all function names to find the login trigger
                    const fnNames = (inline.match(/function\s+(\w+)\s*\(/g) || []).map(m => m.match(/function\s+(\w+)/)[1]);

                    return {
                        renderCalls: renderMatches.slice(0, 5),
                        widgetInfo,
                        actionMatches: actionMatches.slice(0, 10),
                        actionValues: actionValues.slice(0, 10),
                        sitekeyMatches: sitekeyMatches.slice(0, 5),
                        captchaCallback: cbMatch ? cbMatch[0].slice(0, 400) : null,
                        executeCalls: executeMatches.slice(0, 5),
                        fnNames: fnNames.slice(0, 30),
                    };
                }
                """)

                print(f"\n  reCAPTCHA config:")
                print(f"    widgetInfo: {rcaptcha_info['widgetInfo']}")
                print(f"    renderCalls: {rcaptcha_info['renderCalls']}")
                print(f"    actionMatches: {rcaptcha_info['actionMatches']}")
                print(f"    actionValues: {rcaptcha_info['actionValues']}")
                print(f"    sitekeyMatches: {rcaptcha_info['sitekeyMatches']}")
                print(f"    executeCalls: {rcaptcha_info['executeCalls']}")
                print(f"    fnNames: {rcaptcha_info['fnNames']}")
                print(f"    captchaCallback: {rcaptcha_info['captchaCallback']}")

                # Also extract button/submit handlers
                submit_info = page.evaluate("""
                () => {
                    const btns = Array.from(document.querySelectorAll('button,input[type=submit],a[onclick]'));
                    return btns.map(b => ({tag: b.tagName, id: b.id, onclick: b.getAttribute('onclick'), text: b.textContent.trim().slice(0,40)}));
                }
                """)
                print(f"    buttons: {submit_info}")

                # Look at ALL inline scripts for login-related code
                full_scripts = page.evaluate("""
                () => {
                    const scripts = Array.from(document.querySelectorAll('script:not([src])')).map(s => s.textContent);
                    // Find script containing captchaResponse or doLogin
                    return scripts.filter(s => s.includes('captchaResponse') || s.includes('doLogin') || s.includes('loginForm'));
                }
                """)
                print(f"\n  Login scripts ({len(full_scripts)} found):")
                for i, sc in enumerate(full_scripts):
                    print(f"    Script {i} (len={len(sc)}):\n{sc[:600]}")

                # Screenshot
                ss = str(SS_DIR / f"auth_FR{idx}.png")
                page.screenshot(path=ss)
                print(f"\n  Screenshot: {ss}")

                browser.close()
                print(f"\nFound working proxy: FR-{idx}")
                sys.exit(0)

            except Exception as e:
                print(f"  NAV_ERR: {type(e).__name__}: {str(e)[:80]}")
                try: browser.close()
                except: pass
    except Exception as e:
        print(f"  BRIDGE_ERR: {e!s:.60}")

print("\nNo working proxy found in range.")
