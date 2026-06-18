"""Find doLogin() — get its source and also capture what the site actually sends."""
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
    # Check where doLogin is defined
    result = s.browser_eval("""
    (() => {
        return {
            doLoginType: typeof window.doLogin,
            doLoginSrc: typeof window.doLogin === 'function' ?
                        window.doLogin.toString().substring(0, 3000) : 'not_a_function',
        };
    })()
    """)
    print('doLogin type:', result.get('doLoginType'))
    print('doLogin source:')
    print(result.get('doLoginSrc', ''))
    print()

    # Also try to find it in ALL script elements on the page
    result2 = s.browser_eval("""
    (() => {
        const scripts = Array.from(document.querySelectorAll('script:not([src])'));
        const hits = [];
        for (const s of scripts) {
            const t = s.textContent;
            if (t.toLowerCase().includes('dologin') || t.toLowerCase().includes('do_login')) {
                hits.push(t.substring(0, 2000));
            }
        }
        return hits;
    })()
    """)
    print('Scripts containing doLogin:')
    for h in result2:
        print(h[:2000])
        print('---')

    # Check other JS files
    other_js_files = [
        '/VistosOnline/js/Company/jquery.validate.min.js',
        '/VistosOnline/js/Company/jquery.min.js',
    ]
    for js_url in other_js_files:
        try:
            content = s.browser_eval(f"""
            (async () => {{
                const r = await fetch('{js_url}');
                const t = await r.text();
                const idx = t.toLowerCase().indexOf('dologin');
                if (idx >= 0) {{
                    return t.substring(Math.max(0,idx-50), idx+500);
                }}
                return 'not_found';
            }})()
            """)
            if content != 'not_found':
                print(f'{js_url} contains doLogin:')
                print(content[:1000])
            else:
                print(f'{js_url}: doLogin not found')
        except Exception as e:
            print(f'{js_url}: error {e}')

finally:
    s.close()
print('Done.')
