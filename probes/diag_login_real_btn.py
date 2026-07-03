"""
Test: click the portal's REAL login submit button (not synthetic form injection).
This exercises whatever doLogin() JavaScript the portal uses, including any
hidden CSRF tokens or session nonces that our synthetic form might be missing.

Also captures what happens when we submit with the real button click.
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
SS_DIR = ROOT / "app" / "core" / "screenshots"
SS_DIR.mkdir(parents=True, exist_ok=True)

# Use a proxy that previously reached the auth page successfully
PROXY = "http://Mylist1234-FR-108:Saulo12345@p.webshare.io:80"
USERNAME = "dulrodri852906"
PASSWORD = "zm70Vi53kZki!#"

# Read capsolver key from .env
import os
_env_path = ROOT / "app" / "core" / ".env"
CAPSOLVER_KEY = ""
if _env_path.exists():
    for _line in _env_path.read_text(encoding="utf-8").splitlines():
        _line = _line.strip()
        if _line.startswith("CAPSOLVER_KEYS="):
            CAPSOLVER_KEY = _line.split("=", 1)[1].split(",")[0].strip()
            break
CAPSOLVER_KEY = CAPSOLVER_KEY or os.environ.get("CAPSOLVER_KEY", "")

print(f"Bridge setup for {PROXY}")
bridge = start_bridge(PROXY)
print(f"Bridge: {bridge}")

with sync_playwright() as pw:
    browser = pw.chromium.launch(channel="chrome", headless=True, args=["--no-sandbox"])
    ctx = browser.new_context(
        proxy={"server": bridge},
        user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        locale="pt-PT",
        ignore_https_errors=True,
    )
    page = ctx.new_page()
    Stealth().apply_stealth_sync(page)

    # 1. Navigate HOME then AUTH
    print(f"\n[1] Navigate HOME -> AUTH")
    page.goto(HOME, timeout=30000, wait_until="domcontentloaded")
    page.wait_for_load_state("networkidle", timeout=15000)
    page.goto(AUTH, timeout=25000, wait_until="domcontentloaded")
    page.wait_for_load_state("networkidle", timeout=15000)
    time.sleep(2)

    title = page.title()
    print(f"    Title: {title!r}")
    if "Não foi" in title or "could not" in title.lower():
        print("    PORTAL IP BAN - this proxy is blocked")
        browser.close()
        sys.exit(1)

    # 2. Capture the login page JS to find doLogin() or submit handler
    print(f"\n[2] Examine page scripts for login function")
    login_fn = page.evaluate("""
    () => {
        // Look for the doLogin function or form submit handler
        const scriptTexts = Array.from(document.querySelectorAll('script:not([src])')).map(s => s.textContent);
        const loginFnMatch = scriptTexts.join('\n').match(/function\\s+(doLogin|loginSubmit|submitLogin|onSubmit)\\s*\\([^)]*\\)\\s*\\{[^}]{0,500}/);
        const jqueryAjax = scriptTexts.join('\n').match(/\\.ajax\\s*\\(\\s*\\{[^}]{0,300}/g);
        const captchaCallback = scriptTexts.join('\n').match(/function\\s+onCaptchaSuccess[^}]{0,300}/);
        return {
            loginFn: loginFnMatch ? loginFnMatch[0] : null,
            jqueryAjax: jqueryAjax ? jqueryAjax.slice(0, 3) : [],
            captchaCallback: captchaCallback ? captchaCallback[0] : null,
            allFunctions: scriptTexts.join('\n').match(/function\\s+\\w+\\s*\\([^)]*\\)/g)?.slice(0, 30) || [],
        };
    }
    """)
    print(f"    allFunctions: {login_fn['allFunctions']}")
    print(f"    loginFn: {login_fn['loginFn']}")
    print(f"    captchaCallback: {login_fn['captchaCallback']}")
    if login_fn['jqueryAjax']:
        for i, aj in enumerate(login_fn['jqueryAjax']):
            print(f"    $.ajax[{i}]: {aj[:200]!r}")

    # 3. Check submit button
    submit_info = page.evaluate("""
    () => {
        const btns = Array.from(document.querySelectorAll('button, input[type=submit], a[onclick*=Login], a[onclick*=login]'));
        return btns.map(b => ({tag: b.tagName, type: b.type, id: b.id, class: b.className, onclick: b.getAttribute('onclick'), text: b.textContent.trim().slice(0,50)}));
    }
    """)
    print(f"\n[3] Submit buttons: {submit_info}")

    # 4. Fill credentials
    print(f"\n[4] Fill credentials")
    try:
        uf = page.locator('input[name="username"]').first
        uf.fill(USERNAME)
        time.sleep(0.5)
    except Exception as e:
        print(f"    username fill error: {e}")

    try:
        pf = page.locator('input[type="password"]').first
        pf.fill(PASSWORD)
        time.sleep(0.5)
    except Exception as e:
        print(f"    password fill error: {e}")

    # 5. Get a real CAPTCHA token via capsolver
    token = ""
    if CAPSOLVER_KEY:
        print(f"\n[5] Getting CAPTCHA token (capsolver)")
        try:
            import solver as _solver
            token = _solver.race_all([CAPSOLVER_KEY], [], [], [], "LOGIN_EVISA", proxy=None, min_score=50)
            print(f"    token len={len(token)}")
        except Exception as e:
            print(f"    solver error: {e}")
    else:
        print(f"\n[5] No CAPSOLVER_KEY - skipping real token (will try with empty)")

    # 6. Set token in textarea
    if token:
        tok_js = json.dumps(token)
        page.evaluate(f"""
        (() => {{
            const _tok = {tok_js};
            for (const sel of ['#g-recaptcha-response-1', '#g-recaptcha-response']) {{
                const ta = document.querySelector(sel);
                if (!ta) continue;
                const setter = Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, 'value')?.set;
                if (setter) setter.call(ta, _tok); else ta.value = _tok;
                ta.dispatchEvent(new Event('input', {{bubbles: true}}));
                ta.dispatchEvent(new Event('change', {{bubbles: true}}));
            }}
            // Patch grecaptcha
            const _patch = (obj) => {{
                if (!obj) return;
                const _orig = obj.getResponse;
                obj.getResponse = function(id) {{ return (_orig ? _orig.call(this, id) : '') || _tok; }};
            }};
            if (window.grecaptcha) {{
                _patch(window.grecaptcha);
                if (window.grecaptcha.enterprise) _patch(window.grecaptcha.enterprise);
            }}
        }})()
        """)
        # Call onCaptchaSuccess
        page.evaluate(f"""
        (() => {{
            const _tok = {tok_js};
            if (typeof window.onCaptchaSuccess === 'function') window.onCaptchaSuccess(_tok);
        }})()
        """)
        time.sleep(1.5)

    # 7. Try clicking the real submit button
    print(f"\n[7] Clicking real submit button")
    screenshot_before = str(SS_DIR / "diag_before_submit.png")
    page.screenshot(path=screenshot_before)
    print(f"    Screenshot before: {screenshot_before}")

    clicked = False
    try:
        # Try finding a clickable login button
        btn_selectors = [
            'button[type=submit]',
            'input[type=submit]',
            'a[onclick*="ogin"]',
            'button:has-text("Iniciar")',
            'button:has-text("Login")',
            '[onclick*="doLogin"]',
            '[onclick*="submitLogin"]',
        ]
        for sel in btn_selectors:
            try:
                btn = page.locator(sel).first
                if btn.count() > 0 and btn.is_visible(timeout=1000):
                    print(f"    Clicking: {sel}")
                    with page.expect_navigation(wait_until="networkidle", timeout=30000):
                        btn.click()
                    clicked = True
                    break
            except Exception as be:
                print(f"    {sel}: {be!s:.60}")
    except Exception as e:
        print(f"    click error: {e}")

    if not clicked:
        print(f"    No button clicked, trying JS doLogin()")
        try:
            with page.expect_navigation(wait_until="networkidle", timeout=30000):
                page.evaluate("() => { if(typeof doLogin==='function') doLogin(); else if(typeof window.doLogin==='function') window.doLogin(); }")
            clicked = True
        except Exception as e:
            print(f"    doLogin() error: {e}")

    time.sleep(2)
    final_url = page.url
    html = page.content() or ""
    title2 = page.title()
    print(f"\n[8] After submit:")
    print(f"    URL: {final_url}")
    print(f"    Title: {title2!r}")
    print(f"    HTML length: {len(html)}")
    print(f"    at_home: {final_url.rstrip('/').endswith('/VistosOnline')}")
    print(f"    at_auth: 'Authentication.jsp' in {final_url!r}")
    print(f"    at_login: final_url.rstrip('/').endswith('/VistosOnline/login')")

    # Try to extract any error message
    err_msg = page.evaluate("""
    () => {
        const sels = ['.alert', '.error', '.errorMessage', '#errorMessage', '.text-danger',
                      '.msg-error', '[class*="error"]', '[id*="error"]', '.captchaError',
                      'td.text-center', 'p.text-center', '.message', '#message'];
        for (const s of sels) {
            const el = document.querySelector(s);
            if (el && el.textContent.trim()) return {sel: s, text: el.textContent.trim().slice(0, 300)};
        }
        // Check body text for Portuguese error keywords
        const body = document.body?.innerText || '';
        const errIdx = body.search(/erro|invalid|Inválido|incorret|incorrectly|falhou|falha/i);
        if (errIdx >= 0) return {sel: 'body', text: body.slice(Math.max(0,errIdx-50), errIdx+200)};
        return null;
    }
    """)
    print(f"    error_msg: {err_msg}")

    # Also show body text snippet
    body_text = page.evaluate("() => document.body?.innerText?.slice(0, 800) || ''")
    print(f"    body_text[:800]: {body_text!r}")

    screenshot_after = str(SS_DIR / "diag_after_submit.png")
    page.screenshot(path=screenshot_after)
    print(f"    Screenshot after: {screenshot_after}")

    browser.close()
    print("\nDone.")
