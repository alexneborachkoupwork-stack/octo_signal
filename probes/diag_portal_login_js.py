"""
Extract the portal's actual login JavaScript to find:
- What function is called on button click
- What fields are sent in the POST
- Any CSRF tokens or hidden fields
"""
import sys, time, json
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "app" / "core"))

from proxy_bridge import start_bridge
from playwright.sync_api import sync_playwright
from playwright_stealth import Stealth

HOME = "https://pedidodevistos.mne.gov.pt/VistosOnline/"
AUTH = "https://pedidodevistos.mne.gov.pt/VistosOnline/Authentication.jsp"

import argparse
ap = argparse.ArgumentParser()
ap.add_argument("--proxy", default="http://Mylist1234-FR-110:Saulo12345@p.webshare.io:80")
args = ap.parse_args()

bridge = start_bridge(args.proxy)
print(f"Proxy: {args.proxy}")
print(f"Bridge: {bridge}")

with sync_playwright() as pw:
    browser = pw.chromium.launch(channel="chrome", headless=True, args=["--no-sandbox"])
    ctx = browser.new_context(proxy={"server": bridge}, ignore_https_errors=True, locale="pt-PT")
    page = ctx.new_page()
    Stealth().apply_stealth_sync(page)

    print(f"\n[1] Navigate HOME -> AUTH")
    try:
        page.goto(HOME, timeout=25000, wait_until="domcontentloaded")
        page.wait_for_load_state("networkidle", timeout=12000)
        page.goto(AUTH, timeout=25000, wait_until="domcontentloaded")
        page.wait_for_load_state("networkidle", timeout=12000)
        time.sleep(2)
    except Exception as e:
        print(f"    NAV ERROR: {e}")
        browser.close()
        sys.exit(1)

    title = page.title()
    print(f"    Title: {title!r}")
    body_text = page.evaluate("() => document.body?.innerText?.slice(0,200) || ''")
    print(f"    Body: {body_text!r}")

    if "Não foi" in title or "foi" in body_text:
        print("    PORTAL IP BAN")
        browser.close()
        sys.exit(1)

    print(f"\n[2] Extract login-related JavaScript")
    js_info = page.evaluate("""
    () => {
        const inline = Array.from(document.querySelectorAll('script:not([src])')).map(s => s.textContent);
        const all = inline.join('\\n');

        // Find lines containing 'login', '/login', 'doLogin', 'ajax', etc.
        const lines = all.split('\\n');
        const loginLines = lines.filter(l =>
            l.match(/login|Login|captchaResponse|g-recaptcha|ajax|post.*json|\.post\(/i)
            && !l.includes('//') // skip comment lines
        ).slice(0, 50);

        // Find all function definitions
        const fns = (all.match(/function\\s+\\w+\\s*\\([^)]*\\)/g) || []).slice(0, 20);

        // Full doLogin or similar function
        const doLoginMatch = all.match(/(function\\s+(doLogin|loginForm|submitLogin)\\s*\\([^)]*\\)\\s*\\{[\\s\\S]{0,800}?\})/);

        // Form submit event listeners
        const onsubmitMatch = all.match(/addEventListener\\s*\\(\\s*['"]submit['"][\\s\\S]{0,300}?\)/g);

        // onclick handlers on buttons
        const btns = Array.from(document.querySelectorAll('button,input[type=submit],a[onclick]')).map(b => ({
            tag: b.tagName, id: b.id, cls: b.className.slice(0,40),
            onclick: b.getAttribute('onclick'), text: b.textContent.trim().slice(0,30)
        }));

        return {
            functionDefs: fns,
            doLoginFull: doLoginMatch ? doLoginMatch[0] : null,
            loginLines: loginLines,
            onsubmitListeners: onsubmitMatch || [],
            buttons: btns,
        };
    }
    """)

    print(f"\n  Function defs: {js_info['functionDefs']}")
    print(f"\n  Buttons: {js_info['buttons']}")
    print(f"\n  onsubmit listeners: {js_info['onsubmitListeners']}")
    if js_info['doLoginFull']:
        print(f"\n  doLogin() full:\n{js_info['doLoginFull']}")
    else:
        print(f"\n  doLogin NOT FOUND as inline script")
    print(f"\n  Login-related lines:")
    for line in js_info['loginLines']:
        print(f"    {line.strip()[:120]}")

    # Also check external scripts
    print(f"\n[3] External scripts linked")
    ext_scripts = page.evaluate("""
    () => Array.from(document.querySelectorAll('script[src]')).map(s => s.src)
    """)
    for s in ext_scripts:
        print(f"  {s}")

    # Network intercept: capture what happens when we click the submit btn
    print(f"\n[4] Form inspection")
    forms = page.evaluate("""
    () => {
        return Array.from(document.querySelectorAll('form')).map(f => ({
            id: f.id, action: f.action, method: f.method,
            inputs: Array.from(f.elements).map(e => ({name: e.name, type: e.type, id: e.id})).filter(e => e.name)
        }));
    }
    """)
    print(f"  Forms: {json.dumps(forms, indent=2)}")

    browser.close()
    print("\nDone.")
