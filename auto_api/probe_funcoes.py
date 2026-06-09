"""Read funcoes.js from within the browser session to see what doLogin() sends."""
import sys, io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

import session as sess
from proxy_pool import PersistentProxyPool
from pathlib import Path

pool = PersistentProxyPool(Path('data/proxies_isp.txt'))
pool.reset()

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
    # Fetch funcoes.js from the browser context (same-origin fetch)
    result = s.browser_eval("""
    (async () => {
        const resp = await fetch('/VistosOnline/js/funcoes.js');
        return await resp.text();
    })()
    """)
    print('funcoes.js size:', len(result))
    print()
    # Find doLogin function
    idx = result.lower().find('dologin')
    if idx >= 0:
        # print surrounding 3000 chars
        start = max(0, idx - 100)
        end = min(len(result), idx + 3000)
        print('doLogin context:')
        print(result[start:end])
    else:
        print('doLogin not found in funcoes.js!')
        print('First 2000 chars:')
        print(result[:2000])
finally:
    s.close()
print('\nDone.')
