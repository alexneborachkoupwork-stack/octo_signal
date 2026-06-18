"""Get the full inline script containing doLogin from Authentication.jsp."""
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
    print(f'Trying {ip}...', flush=True)
    try:
        s = sess.get_session(proxy, headless=True)
        print(f'Session ready on {ip}')
        break
    except Exception as e:
        print(f'  FAIL: {e!s:.60}')
        s = None

if s is None:
    print('ERROR')
    sys.exit(1)

try:
    # Get full text of every inline script that mentions doLogin or login
    result = s.browser_eval("""
    (() => {
        const scripts = Array.from(document.querySelectorAll('script:not([src])'));
        const hits = [];
        for (const s of scripts) {
            const t = s.textContent;
            if (t.includes('doLogin') || t.includes('loginSubmit')) {
                hits.push(t);
            }
        }
        return hits.join('\\n\\n=====SCRIPT_SEP=====\\n\\n');
    })()
    """)
    print('Full login scripts:')
    print(result)
finally:
    s.close()
print('\nDone.')
