"""Probe: fetch Authentication.jsp via primp (has session cookies) and inspect JS."""
import sys, io, re
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

import session as sess
from proxy_pool import PersistentProxyPool
from pathlib import Path

pool = PersistentProxyPool(Path('data/proxies_isp.txt'))
pool.reset()
proxy = pool.advance()
print('Using proxy:', proxy.split('@')[-1] if '@' in proxy else proxy)

s = sess.get_session(proxy, headless=True)
print('Session ready')

AUTH_URL = 'https://pedidodevistos.mne.gov.pt/VistosOnline/Authentication.jsp'

try:
    # Use primp GET (has session cookies from WAF bypass)
    r = s.get(AUTH_URL, timeout=20)
    content = r.text
    print(f'Page size: {len(content)}  status: {r.status_code}')

    if '/ch/bd.js' in content:
        print('WARNING: got WAF challenge page instead of login page')
    else:
        # Print all form elements
        forms = re.findall(r'<form[^>]*>.*?</form>', content, re.DOTALL | re.IGNORECASE)
        for i, f in enumerate(forms):
            print(f'\nFORM {i}:', f[:600])

        # Print inline scripts containing login/captcha logic
        scripts = re.findall(r'<script[^>]*>(.*?)</script>', content, re.DOTALL | re.IGNORECASE)
        for sc in scripts:
            sc = sc.strip()
            if sc and any(k in sc.lower() for k in ('login', 'captcha', 'dologin', 'grecaptcha', 'getresponse')):
                print('\nINLINE SCRIPT:')
                print(sc[:2000])

        # Print all external script src attributes
        srcs = re.findall(r'<script[^>]+src=["\']([^"\']+)["\']', content, re.IGNORECASE)
        print('\nExternal scripts:', srcs)

        # Any hidden inputs anywhere on page
        hiddens = re.findall(r'<input[^>]+type=["\']hidden["\'][^>]*>', content, re.IGNORECASE)
        print('\nHidden inputs:', hiddens)
except Exception as e:
    print('ERROR:', e)
finally:
    s.close()
