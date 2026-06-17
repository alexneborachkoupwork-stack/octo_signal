"""
Diagnostic: submit login with empty/invalid captchaResponse to identify what the server validates first.
- ReCaptchaError → CAPTCHA is checked; our tokens fail
- type:error     → something else fails before CAPTCHA
"""
import sys, io, json
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

import session as sess
from proxy_pool import PersistentProxyPool
from pathlib import Path

pool = PersistentProxyPool(Path('data/proxies_isp.txt'))
pool.reset()

# Find a working proxy
s = None
for attempt in range(15):
    proxy = pool.advance()
    ip = proxy.split('@')[-1] if '@' in proxy else proxy
    print(f'Trying proxy: {ip}')
    try:
        s = sess.get_session(proxy, headless=True)
        print(f'Session ready on {ip}')
        break
    except Exception as e:
        print(f'  FAIL: {e!s:.80}')
        s = None

if s is None:
    print('ERROR: no working proxy found')
    sys.exit(1)

LOGIN_URL = 'https://pedidodevistos.mne.gov.pt/VistosOnline/login'
user = 'erivar3335'
pw   = 'ljfWlF689cr6!#'

for label, captcha_val, password in [
    ('empty captchaResponse', '', pw),
    ('invalid token string',  'INVALID_TOKEN_XYZ', pw),
    ('valid token, wrong pw', 'INVALID_TOKEN_XYZ', 'WRONGPASSWORD999'),
]:
    print(f'\n--- TEST: {label} ---')
    try:
        r = s.browser_fetch(LOGIN_URL, data={
            'username': user, 'password': password,
            'language': 'PT', 'rgpd': 'Y',
            'captchaResponse': captcha_val,
        }, timeout=20)
        print('Response:', r.get('body', '')[:300])
    except Exception as e:
        print('ERROR:', e)

s.close()
print('\nDone.')
