"""
Probe: Get Authentication.jsp HTML via get_session() + browser_eval() to find
hidden fields, CSRF tokens, and login JS logic.
"""
import sys, io, re, json
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

import session as sess
from proxy_pool import PersistentProxyPool
from pathlib import Path

pool = PersistentProxyPool(Path('data/proxies_isp.txt'))
pool.reset()

# Find a working session
s = None
for _ in range(15):
    proxy = pool.advance()
    ip = proxy.split('@')[-1] if '@' in proxy else proxy
    print(f'Trying {ip}...')
    try:
        s = sess.get_session(proxy, headless=True)
        print(f'Session ready on {ip}')
        break
    except Exception as e:
        print(f'  FAIL: {e!s:.60}')
        s = None

if s is None:
    print('ERROR: no working proxy')
    sys.exit(1)

try:
    # Get the full page HTML from the current browser page (AUTH_URL from get_session)
    html = s.browser_eval('document.documentElement.outerHTML')
    print(f'Page HTML size: {len(html)}')
    print()

    if '/ch/bd.js' in html:
        print('WARNING: page is WAF challenge')
    else:
        # Print all forms
        forms = re.findall(r'<form[^>]*>.*?</form>', html, re.DOTALL | re.IGNORECASE)
        for i, f in enumerate(forms):
            print(f'=== FORM {i} ===')
            print(f[:800])
            print()

        # All hidden inputs
        hiddens = re.findall(r'<input[^>]+type=["\']hidden["\'][^>]*/?>',
                             html, re.IGNORECASE)
        print('=== HIDDEN INPUTS ===')
        for h in hiddens:
            print(' ', h)
        print()

        # All input fields (any type)
        all_inputs = re.findall(r'<input[^>]*/?>',
                                html, re.IGNORECASE)
        print('=== ALL INPUTS ===')
        for inp in all_inputs:
            if 'hidden' in inp.lower() or 'text' in inp.lower() or 'password' in inp.lower():
                print(' ', inp[:200])
        print()

        # Script tags with login logic
        scripts = re.findall(r'<script[^>]*>(.*?)</script>', html, re.DOTALL | re.IGNORECASE)
        for i, sc in enumerate(scripts):
            sc = sc.strip()
            if sc and any(k in sc.lower() for k in ('login', 'captcha', 'ajax', 'username',
                                                     'password', 'dologin', 'submit', 'rgpd')):
                print(f'=== SCRIPT {i} ===')
                print(sc[:3000])
                print()

        # External scripts
        srcs = re.findall(r'<script[^>]+src=["\']([^"\']+)["\']', html, re.IGNORECASE)
        print('=== EXTERNAL SCRIPTS ===')
        for s_url in srcs:
            print(' ', s_url)

    # Also read cookies from the browser
    cookies = s.browser_eval('document.cookie')
    print(f'\nBrowser cookies: {cookies}')

    # Try getting reCAPTCHA info
    try:
        rc_info = s.browser_eval('''
            (() => {
                try {
                    return {
                        response: grecaptcha && grecaptcha.enterprise ?
                                  grecaptcha.enterprise.getResponse() :
                                  (grecaptcha ? grecaptcha.getResponse() : "no_grecaptcha"),
                        sitekey: document.querySelector("[data-sitekey]") ?
                                 document.querySelector("[data-sitekey]").getAttribute("data-sitekey") : "not_found",
                    };
                } catch(e) { return {error: e.toString()}; }
            })()
        ''')
        print(f'\nreCAPTCHA info: {rc_info}')
    except Exception as e:
        print(f'\nreCAPTCHA probe: {e}')

finally:
    s.close()

print('\nDone.')
