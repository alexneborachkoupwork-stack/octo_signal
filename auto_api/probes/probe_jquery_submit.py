"""
Test: use jQuery $.ajax directly from within the browser session.
Compares jQuery AJAX vs our fetch() to find what the server actually expects.
"""
import sys, io, time, json
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

import session as sess
import solver as solvermod
from proxy_pool import PersistentProxyPool
from pathlib import Path

pool = PersistentProxyPool(Path('data/proxies_isp.txt'))
pool.reset()

USERNAME = 'erivar3335'
PASSWORD = 'ljfWlF689cr6!#'

env = {}
for ef in [Path('.env'), Path('auto_api/.env'), Path('../.env')]:
    if ef.exists():
        for line in ef.read_text(encoding='utf-8').splitlines():
            line = line.strip()
            if line and '=' in line and not line.startswith('#'):
                k, _, v = line.partition('=')
                env[k.strip()] = v.strip()
        break

capsolver_keys = [x.strip() for x in env.get('CAPSOLVER_KEYS', '').split(',') if x.strip()]
if not capsolver_keys:
    print('ERROR: no CAPSOLVER_KEYS')
    sys.exit(1)

s = None
proxy = None
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
    print(f'Getting CapSolver token...')
    token = solvermod.solve('capsolver', capsolver_keys, 'LOGIN_EVISA', proxy)
    print(f'Token len={len(token)}')

    # Store data in window variables (avoids any escaping issues in eval)
    s.browser_eval('window._ld = ' + json.dumps({
        'username': USERNAME,
        'password': PASSWORD,
        'token': token,
    }))

    # Test 1: jQuery $.ajax (same as doLogin)
    print('\n=== TEST 1: jQuery $.ajax ===')
    r1 = s.browser_eval("""
    (function() {
        return new Promise(function(resolve) {
            $.ajax({
                url: '/VistosOnline/login',
                data: {
                    username: window._ld.username,
                    password: window._ld.password,
                    language: 'PT',
                    rgpd: 'Y',
                    captchaResponse: window._ld.token
                },
                type: 'POST',
                success: function(data) {
                    resolve({method: 'jquery', ok: true, data: data});
                },
                error: function(xhr, status, err) {
                    resolve({method: 'jquery', ok: false, status: xhr.status,
                              body: xhr.responseText, err: err});
                }
            });
        });
    })()
    """, timeout=30)
    print('jQuery result:', r1)

    # Test 2: fetch() (what we currently use)
    print('\n=== TEST 2: fetch() ===')
    r2 = s.browser_eval("""
    (function() {
        return (async function() {
            const body = new URLSearchParams({
                username: window._ld.username,
                password: window._ld.password,
                language: 'PT',
                rgpd: 'Y',
                captchaResponse: window._ld.token
            }).toString();
            const resp = await fetch('/VistosOnline/login', {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/x-www-form-urlencoded; charset=UTF-8',
                    'X-Requested-With': 'XMLHttpRequest',
                    'Accept': '*/*'
                },
                body: body
            });
            const text = await resp.text();
            return {method: 'fetch', status: resp.status, body: text};
        })();
    })()
    """, timeout=30)
    print('fetch() result:', r2)

finally:
    s.close()
print('Done.')
