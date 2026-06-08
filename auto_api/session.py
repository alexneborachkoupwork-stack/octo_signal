"""
HTTP session for the E-VISA portal.

Architecture (revised):
  1. PlaywrightSession launches a headless Chrome browser through the proxy,
     navigates to the target site, and runs stealth JS to bypass the
     FingerprintJS BotDetectorLib challenge (/ch/bd.js).
  2. Once the challenge is bypassed, cookies are extracted and injected into
     a lightweight primp.Client (same proxy) via explicit Cookie header.
     The BROWSER STAYS ALIVE to handle verification (which requires the same
     browser session/TLS fingerprint — primp alone hits the challenge again).
  3. All .get()/.post() calls use the primp client directly (fast, low overhead).
  4. Call verify_email(token_url) to use the browser for email verification.
     The browser is then closed automatically.

IP-binding note:
  Both Playwright and the primp client must use the same proxy URL (same
  sessionid) so they share the same exit IP.

Usage:
  s = get_session(proxy_url)   # blocks until challenge is bypassed (~15-25s)
  resp = s.post(URL, data={...}, headers={...})
  result = s.verify_email(token_url, insert_token_url, verify_url,
                          token_input, token_search, lang)
  s.close()
"""

import json as _json
import queue
import random
import threading
import time

IMPERSONATE = None   # None = primp built-in default; no "does not exist" warning

# ── Fingerprint pool ──────────────────────────────────────────────────────────
# (user_agent, sec_ch_ua, viewport_w, viewport_h, platform)
_FP_POOL = [
    (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36 Edg/131.0.0.0",
        '"Microsoft Edge";v="131", "Chromium";v="131", "Not_A Brand";v="24"',
        1280, 800, "Windows",
    ),
    (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        '"Google Chrome";v="131", "Chromium";v="131", "Not_A Brand";v="24"',
        1920, 1080, "Windows",
    ),
    (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
        '"Google Chrome";v="130", "Chromium";v="130", "Not_A Brand";v="99"',
        1366, 768, "Windows",
    ),
    (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        '"Google Chrome";v="131", "Chromium";v="131", "Not_A Brand";v="24"',
        1440, 900, "macOS",
    ),
    (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36",
        '"Google Chrome";v="129", "Chromium";v="129", "Not_A Brand";v="8"',
        1600, 900, "Windows",
    ),
    (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
        '"Google Chrome";v="130", "Chromium";v="130", "Not_A Brand";v="99"',
        1440, 900, "macOS",
    ),
]


def _pick_fingerprint() -> tuple:
    return random.choice(_FP_POOL)


def _get_proxy_timezone(proxy_url: str | None) -> str:
    """Detect the proxy exit-IP's timezone via ipwho.is."""
    if not proxy_url:
        return "America/New_York"
    try:
        import primp
        client = primp.Client(impersonate=IMPERSONATE, proxy=proxy_url,
                              follow_redirects=True, verify=False, timeout=8)
        r = client.get("https://ipwho.is/json/")
        data = r.json()
        tz = data.get("timezone", {})
        if isinstance(tz, dict):
            return tz.get("id", "America/New_York")
        return str(tz) or "America/New_York"
    except Exception:
        return "America/New_York"


BASE     = "https://pedidodevistos.mne.gov.pt"
AUTH_URL = BASE + "/VistosOnline/Authentication.jsp"
HOME_URL = BASE + "/VistosOnline/"

_HEADERS_BASE = {
    "User-Agent":         "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                          "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36 Edg/131.0.0.0",
    "Accept-Language":    "en-US,en;q=0.9",
    "Accept-Encoding":    "gzip, deflate, br",
    "Cache-Control":      "no-cache",
    "Pragma":             "no-cache",
    "sec-ch-ua":          '"Microsoft Edge";v="131", "Chromium";v="131", "Not_A Brand";v="24"',
    "sec-ch-ua-mobile":   "?0",
    "sec-ch-ua-platform": '"Windows"',
}

HEADERS_NAV = {
    **_HEADERS_BASE,
    "Accept":         "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
}

HEADERS_XHR = {
    **_HEADERS_BASE,
    "Accept":           "*/*",
    "Content-Type":     "application/x-www-form-urlencoded; charset=UTF-8",
    "X-Requested-With": "XMLHttpRequest",
    "Origin":           BASE,
    "Referer":          AUTH_URL,
    "Sec-Fetch-Dest":   "empty",
    "Sec-Fetch-Mode":   "cors",
    "Sec-Fetch-Site":   "same-origin",
}


def _make_stealth_script(vp_w: int, vp_h: int) -> str:
    return f"""
Object.defineProperty(navigator,'webdriver',{{get:()=>undefined}});
window.chrome={{runtime:{{}},loadTimes:function(){{}},csi:function(){{}},app:{{}}}};
Object.defineProperty(navigator,'plugins',{{get:()=>[
  {{name:'Chrome PDF Plugin'}},{{name:'Chrome PDF Viewer'}},{{name:'Native Client'}}
]}});
Object.defineProperty(navigator,'languages',{{get:()=>['en-US','en']}});
const _origQuery=window.navigator.permissions.query;
window.navigator.permissions.query=(p)=>p.name==='notifications'
  ?Promise.resolve({{state:'denied'}}):_origQuery(p);
Object.defineProperty(navigator,'hardwareConcurrency',{{get:()=>4}});
Object.defineProperty(screen,'colorDepth',{{get:()=>24}});
Object.defineProperty(screen,'width',{{get:()=>{vp_w}}});
Object.defineProperty(screen,'height',{{get:()=>{vp_h}}});
"""


# ── Fake response (matches primp.Response interface) ─────────────────────────

class _FakeResponse:
    def __init__(self, status_code: int, text: str, url: str):
        self.status_code = status_code
        self.text        = text
        self.url         = url
        self.cookies     = {}

    def json(self):
        return _json.loads(self.text)


# ── PlaywrightSession ─────────────────────────────────────────────────────────

class PlaywrightSession:
    """
    Hybrid session:
    - Playwright browser bypasses the bot challenge and stays alive for verification.
    - primp.Client (with explicit Cookie header) handles all API calls (registration,
      login, apply steps) without the browser overhead of page.evaluate().
    - verify_email() uses the browser page to navigate to the token URL, which avoids
      the challenge page that primp alone would receive.

    Thread-safe: a dedicated worker thread owns Playwright.
    All primp calls are synchronous on the calling thread.
    verify_email() enqueues a job to the worker thread and waits for the result.
    close() enqueues a shutdown job and waits for the worker to exit.
    """

    _CMD_VERIFY = "verify"
    _CMD_FETCH  = "fetch"
    _CMD_LOGIN  = "login"
    _CMD_CLOSE  = "close"

    def __init__(self, proxy: str | None, inject_cookies: dict | None = None,
                 headless: bool = True):
        self.proxy           = proxy          # original URL — used by primp + CapSolver
        self._inject_cookies = inject_cookies or {}   # pre-load cookies → skip WAF challenge
        self._headless       = headless
        # Chromium cannot authenticate with SOCKS5 user:pass; route via a local
        # HTTP→SOCKS5 bridge. primp handles socks5:// natively and needs no bridge.
        if proxy and proxy.startswith('socks5://'):
            from proxy_bridge import start_bridge
            self._playwright_proxy = start_bridge(proxy)
        else:
            self._playwright_proxy = proxy
        self._primp   = None
        self._cookie_header: str = ""
        self._cmd_q:  queue.Queue = queue.Queue()
        self._res_q:  queue.Queue = queue.Queue()
        self._thread  = threading.Thread(target=self._worker, daemon=True)
        self._ready   = threading.Event()
        self._error: Exception | None = None
        self._thread.start()
        if not self._ready.wait(timeout=120):
            raise RuntimeError("PlaywrightSession init timed out (>120s)")
        if self._error:
            raise self._error

    # ── Worker thread (owns Playwright) ──────────────────────────────────────

    def _worker(self):
        from playwright.sync_api import sync_playwright
        browser = None
        pw = None
        page = None
        try:
            server, username, password = _parse_proxy(self._playwright_proxy)
            proxy_config = None
            if server:
                proxy_config = {"server": server}
                if username:
                    proxy_config["username"] = username
                if password:
                    proxy_config["password"] = password

            ua, sec_ch_ua, vp_w, vp_h, platform = _pick_fingerprint()
            tz = _get_proxy_timezone(self.proxy)
            print(f"[session] fp ua=...{ua[55:85]}  tz={tz}  vp={vp_w}x{vp_h}")

            pw = sync_playwright().start()
            browser = pw.chromium.launch(
                headless=self._headless,
                args=["--disable-blink-features=AutomationControlled",
                      "--disable-dev-shm-usage", "--no-sandbox"],
                proxy=proxy_config,
            )
            ctx = browser.new_context(
                user_agent=ua,
                viewport={"width": vp_w, "height": vp_h},
                screen={"width": vp_w, "height": vp_h},
                locale="en-US",
                timezone_id=tz,
                extra_http_headers={
                    "sec-ch-ua":          sec_ch_ua,
                    "sec-ch-ua-mobile":   "?0",
                    "sec-ch-ua-platform": f'"{platform}"',
                },
            )
            ctx.add_init_script(_make_stealth_script(vp_w, vp_h))

            # Pre-inject saved cookies so the server skips the WAF challenge
            # (used when reopening a browser for email verification after closing
            # the registration browser to free RAM during the email-poll wait).
            if self._inject_cookies:
                ctx.add_cookies([
                    {"name": k, "value": v, "domain": "pedidodevistos.mne.gov.pt",
                     "path": "/"}
                    for k, v in self._inject_cookies.items()
                ])
                print(f"[session] pre-injected {len(self._inject_cookies)} cookies (verify mode)")

            page = ctx.new_page()

            print(f"[session] nav -> {HOME_URL}")
            page.goto(HOME_URL, timeout=30000)
            page.wait_for_load_state("networkidle", timeout=15000)
            page.goto(AUTH_URL, timeout=25000)
            page.wait_for_load_state("networkidle", timeout=15000)
            time.sleep(2)

            content   = page.content()
            challenge = "/ch/bd.js" in content
            raw_cookies = ctx.cookies()
            cookie_dict = {c["name"]: c["value"] for c in raw_cookies}
            cookie_names = list(cookie_dict.keys())
            print(f"[session] challenge={challenge}  cookies={cookie_names}")

            if challenge:
                raise RuntimeError("Bot challenge not bypassed — proxy IP flagged")

            # ── Set up primp client with cookies ──────────────────────────────
            import primp
            primp_client = primp.Client(
                impersonate=IMPERSONATE,
                proxy=self.proxy,
                verify=False,
                follow_redirects=True,
                timeout=30,
            )
            # set_cookies() puts cookies in primp's jar (works for API endpoints).
            # The browser stays alive to handle verify_email() which requires the
            # same browser session (token URL has stricter fingerprint checks).
            if cookie_dict:
                try:
                    primp_client.set_cookies(AUTH_URL, cookie_dict)
                    print(f"[session] set_cookies: {cookie_names}")
                except Exception:
                    # Fallback: cookie header for all requests
                    self._cookie_header = "; ".join(f"{k}={v}" for k, v in cookie_dict.items())
                    print(f"[session] cookies -> header fallback: {cookie_names}")
            self._primp = primp_client
            print(f"[session] primp client ready  (browser alive for verify)")
            self._ready.set()

            # ── Event loop: wait for verify or close commands ─────────────────
            while True:
                cmd = self._cmd_q.get()
                if cmd is None or cmd.get("action") == self._CMD_CLOSE:
                    break

                if cmd.get("action") == self._CMD_VERIFY:
                    try:
                        result = self._do_browser_verify(page, ctx, cmd)
                        self._res_q.put(("ok", result))
                    except Exception as exc:
                        self._res_q.put(("err", str(exc)))

                elif cmd.get("action") == self._CMD_FETCH:
                    try:
                        result = self._do_browser_fetch(page, cmd)
                        self._res_q.put(("ok", result))
                    except Exception as exc:
                        self._res_q.put(("err", str(exc)))

                elif cmd.get("action") == self._CMD_LOGIN:
                    try:
                        result = self._do_browser_login(page, cmd)
                        self._res_q.put(("ok", result))
                    except Exception as exc:
                        self._res_q.put(("err", str(exc)))

        except Exception as exc:
            self._error = exc
            self._ready.set()
        finally:
            try:
                if browser:
                    browser.close()
            except Exception:
                pass
            try:
                if pw:
                    pw.stop()
            except Exception:
                pass
            print(f"[session] browser closed")

    def _do_browser_verify(self, page, ctx, cmd: dict) -> dict:
        """Navigate to the token URL and submit the verification form in the browser."""
        token_url      = cmd["token_url"]
        insert_jsp     = cmd["insert_jsp"]
        verify_url     = cmd["verify_url"]
        token_input    = cmd["token_input"]
        token_search   = cmd["token_search"]
        lang           = cmd["lang"]

        print(f"[session] browser verify: nav -> {token_url[:80]}")
        page.goto(token_url, timeout=25000)
        page.wait_for_load_state("networkidle", timeout=15000)
        content = page.content()
        has_challenge = "/ch/bd.js" in content
        if has_challenge:
            raise RuntimeError("token URL returned challenge — cookie expired?")

        # Submit the verification via fetch() from within the browser context
        js = f"""
        async () => {{
            const resp = await fetch({_json.dumps(verify_url)}, {{
                method: "POST",
                headers: {{
                    "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
                    "X-Requested-With": "XMLHttpRequest",
                    "Accept": "*/*",
                }},
                body: new URLSearchParams({{
                    "language": {_json.dumps(lang)},
                    "token": {_json.dumps(token_input)},
                    "tokenSearchParams": {_json.dumps(token_search)},
                }}).toString(),
            }});
            const text = await resp.text();
            return {{ status: resp.status, body: text }};
        }}
        """
        result = page.evaluate(js)
        print(f"[session] browser verify status={result.get('status')}  "
              f"body={result.get('body','')[:100]}")
        return result

    def _do_browser_fetch(self, page, cmd: dict) -> dict:
        """Execute a fetch() from the browser context. Useful for login and other
        endpoints that require the browser session/TLS fingerprint."""
        url     = cmd["url"]
        method  = cmd.get("method", "POST")
        data    = cmd.get("data", {})
        headers = cmd.get("headers", {})

        headers_js = _json.dumps(headers)
        body_js    = _json.dumps(data)
        method_js  = _json.dumps(method)
        url_js     = _json.dumps(url)

        js = f"""
        async () => {{
            const fields = {body_js};
            const resp = await fetch({url_js}, {{
                method: {method_js},
                headers: {{
                    "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
                    "X-Requested-With": "XMLHttpRequest",
                    "Accept": "*/*",
                    ...{headers_js},
                }},
                body: new URLSearchParams(fields).toString(),
            }});
            const text = await resp.text();
            return {{ status: resp.status, body: text }};
        }}
        """
        result = page.evaluate(js)
        body = result.get("body", "")
        print(f"[session] browser_fetch {method} {url.split('?')[0].split('/')[-1]} "
              f"status={result.get('status')}  body={body[:80]}")
        return {"status": result.get("status", 0), "body": body}

    def _do_browser_login(self, page, cmd: dict) -> dict:
        """
        Navigate fresh to AUTH_URL, type credentials to build reCAPTCHA behavioral
        signals, click the checkbox, then submit login via jQuery $.ajax.
        Falls back to an external CapSolver token if the image challenge appears.
        Returns {"status": int, "body": str}.
        """
        username   = cmd["username"]
        password   = cmd["password"]
        lang       = cmd.get("lang", "PT")
        solver_key = cmd.get("solver_key", "")

        # Fresh navigation — don't rely on stale page from session init
        print(f"[session] browser_login: nav -> AUTH_URL  user={username}")
        page.goto(AUTH_URL, timeout=25000)
        page.wait_for_load_state("networkidle", timeout=15000)
        time.sleep(random.uniform(1.2, 2.0))

        # Type credentials into form fields — keystrokes build reCAPTCHA Enterprise
        # behavioral score (same pattern that makes registration CAPTCHA pass).
        try:
            uf = page.locator(
                'input[name="username"], input#username, '
                'input[autocomplete="username"], input[type="text"]:visible'
            ).first
            uf.click(timeout=5000)
            time.sleep(random.uniform(0.4, 0.8))
            uf.type(username, delay=random.randint(70, 150))
            time.sleep(random.uniform(0.4, 0.9))
        except Exception as e:
            print(f"[session] username field not found: {e}")

        try:
            pf = page.locator(
                'input[name="password"], input#password, input[type="password"]:visible'
            ).first
            pf.click(timeout=5000)
            time.sleep(random.uniform(0.3, 0.7))
            pf.type(password, delay=random.randint(70, 150))
            time.sleep(random.uniform(0.8, 1.5))
        except Exception as e:
            print(f"[session] password field not found: {e}")

        # Move mouse toward reCAPTCHA before clicking
        try:
            box = page.locator('iframe[title*="reCAPTCHA"]').first.bounding_box(timeout=5000)
            if box:
                page.mouse.move(
                    box["x"] + box["width"] * 0.3,
                    box["y"] + box["height"] * 0.5,
                    steps=random.randint(10, 20),
                )
                time.sleep(random.uniform(0.3, 0.6))
        except Exception:
            pass

        # Click the reCAPTCHA checkbox
        print(f"[session] browser_login: clicking checkbox")
        clicked = False
        try:
            rc_frame = page.frame_locator('iframe[title*="reCAPTCHA"]').first
            rc_frame.locator("#recaptcha-anchor").click(timeout=10000)
            clicked = True
        except Exception as e:
            print(f"[session] checkbox click 1 failed: {e}")
        if not clicked:
            try:
                rc_frame = page.frame_locator('iframe[src*="recaptcha"]').first
                rc_frame.locator(".recaptcha-checkbox").click(timeout=8000)
                clicked = True
            except Exception as e2:
                print(f"[session] checkbox click 2 failed: {e2}")

        # Poll for auto-solve token (checkbox goes green without image challenge)
        token = ""
        challenge_detected = False
        for _ in range(25):
            time.sleep(1)
            try:
                token = page.evaluate("grecaptcha.enterprise.getResponse()") or ""
                if token:
                    break
            except Exception:
                pass
            try:
                vis = page.locator(
                    'iframe[title*="recaptcha challenge"], iframe[src*="bframe"]'
                ).is_visible(timeout=300)
                if vis:
                    print(f"[session] image challenge appeared")
                    challenge_detected = True
                    break
            except Exception:
                pass

        # Fallback: inject external CapSolver token when challenged or no auto-solve
        if not token and solver_key:
            print(f"[session] no auto-solve token — requesting external CapSolver token")
            try:
                token = _capsolver_token_sync(
                    solver_key,
                    sitekey="6LdOB9crAAAAADT4RFruc5sPmzLKIgvJVfL830d4",
                    page_url=AUTH_URL,
                    proxy=self.proxy,
                )
                if token:
                    # Inject into page so doLogin picks it up
                    tok_js = _json.dumps(token)
                    page.evaluate(
                        f"document.getElementById('g-recaptcha-response').value = {tok_js};"
                        f"window._injected_token = {tok_js};"
                    )
                    print(f"[session] external token injected  len={len(token)}")
            except Exception as e:
                print(f"[session] external CapSolver failed: {e}")

        # Last-resort: hidden input value
        if not token:
            try:
                token = page.evaluate("document.getElementById('g-recaptcha-response').value") or ""
            except Exception:
                pass

        print(f"[session] browser_login: token_len={len(token)}  challenge={challenge_detected}")
        if not token:
            raise RuntimeError(
                f"reCAPTCHA: no token after all attempts (challenge_detected={challenge_detected})"
            )

        # Intercept the raw /login response via Playwright before jQuery can transform it
        _login_raw: dict = {}

        def _on_response(resp):
            try:
                if "/VistosOnline/login" in resp.url and resp.request.method == "POST":
                    raw = resp.body()
                    ct  = resp.headers.get("content-type", "")
                    _login_raw["status"] = resp.status
                    _login_raw["body"]   = raw.decode("utf-8", errors="replace")
                    _login_raw["ct"]     = ct
                    print(f"[session] raw_login_resp: status={resp.status}  ct={ct!r}  body={_login_raw['body']!r}")
            except Exception as ex:
                print(f"[session] raw_login_resp capture error: {ex}")

        page.on("response", _on_response)

        # Submit via fetch() from the browser context
        username_js = _json.dumps(username)
        password_js = _json.dumps(password)
        lang_js     = _json.dumps(lang)
        token_js    = _json.dumps(token)

        js = f"""
        async () => {{
            const resp = await fetch('/VistosOnline/login', {{
                method: 'POST',
                headers: {{
                    'Content-Type': 'application/x-www-form-urlencoded; charset=UTF-8',
                    'X-Requested-With': 'XMLHttpRequest',
                    'Accept': '*/*',
                }},
                body: new URLSearchParams({{
                    username: {username_js},
                    password: {password_js},
                    language: {lang_js},
                    rgpd: 'Y',
                    captchaResponse: {token_js},
                }}).toString(),
            }});
            const text = await resp.text();
            const ct   = resp.headers.get('content-type') || '';
            return {{ status: resp.status, body: text, ct: ct }};
        }}
        """
        result = page.evaluate(js)
        page.remove_listener("response", _on_response)

        body = result.get("body", "")
        ct   = result.get("ct", "")
        print(f"[session] browser_login: status={result.get('status')}  ct={ct!r}  body={body!r}")
        return {"status": result.get("status", 0), "body": body}

    # ── Public interface ──────────────────────────────────────────────────────

    def _hdrs(self, extra: dict) -> dict:
        """Merge extra headers; inject Cookie header if we have one."""
        h = dict(extra)
        if self._cookie_header and "Cookie" not in h:
            h["Cookie"] = self._cookie_header
        return h

    def get(self, url: str, headers: dict | None = None,
            params: dict | None = None,
            timeout: int = 20, follow_redirects: bool = True, **_) -> _FakeResponse:
        if self._primp is None:
            raise RuntimeError("Session not ready")
        r = self._primp.get(url, headers=self._hdrs(headers or {}),
                            params=params, timeout=timeout)
        return _FakeResponse(r.status_code, r.text, str(r.url))

    def post(self, url: str, data: dict | None = None,
             headers: dict | None = None, params: dict | None = None,
             timeout: int = 30, follow_redirects: bool = True, **_) -> _FakeResponse:
        if self._primp is None:
            raise RuntimeError("Session not ready")
        r = self._primp.post(url, data=data, headers=self._hdrs(headers or {}),
                             params=params, timeout=timeout)
        return _FakeResponse(r.status_code, r.text, str(r.url))

    def browser_fetch(self, url: str, data: dict, method: str = "POST",
                      headers: dict | None = None, timeout: int = 30) -> dict:
        """
        Execute a fetch() from the live browser context.
        Useful for endpoints (login, apply) that require the browser TLS fingerprint.
        Returns {"status": int, "body": str}.
        """
        self._cmd_q.put({
            "action":  self._CMD_FETCH,
            "url":     url,
            "method":  method,
            "data":    data,
            "headers": headers or {},
        })
        try:
            status, result = self._res_q.get(timeout=timeout)
        except queue.Empty:
            raise RuntimeError(f"browser_fetch timed out after {timeout}s")
        if status == "err":
            raise RuntimeError(f"browser_fetch failed: {result}")
        return result

    def browser_login(self, username: str, password: str, lang: str = "PT",
                      solver_key: str = "", timeout: int = 240) -> dict:
        """
        Type credentials into the login form to build behavioral signals, click the
        reCAPTCHA checkbox, and submit. Falls back to an external CapSolver token if
        the image challenge appears or auto-solve fails.
        Returns {"status": int, "body": str}.
        """
        self._cmd_q.put({
            "action":     self._CMD_LOGIN,
            "username":   username,
            "password":   password,
            "lang":       lang,
            "solver_key": solver_key,
        })
        try:
            status, result = self._res_q.get(timeout=timeout)
        except queue.Empty:
            raise RuntimeError(f"browser_login timed out after {timeout}s")
        if status == "err":
            raise RuntimeError(f"browser_login failed: {result}")
        return result

    def verify_email(self, token_url: str, insert_jsp: str, verify_url: str,
                     token_input: str, token_search: str, lang: str,
                     timeout: int = 60) -> dict:
        """
        Use the live browser page to visit the token URL and submit verification.
        Returns {"status": int, "body": str} from the VerificarEmail POST.
        Blocks until done or timeout.
        """
        self._cmd_q.put({
            "action":       self._CMD_VERIFY,
            "token_url":    token_url,
            "insert_jsp":   insert_jsp,
            "verify_url":   verify_url,
            "token_input":  token_input,
            "token_search": token_search,
            "lang":         lang,
        })
        try:
            status, result = self._res_q.get(timeout=timeout)
        except queue.Empty:
            raise RuntimeError(f"verify_email timed out after {timeout}s")
        if status == "err":
            raise RuntimeError(f"verify_email failed: {result}")
        return result

    def get_cookies(self, url: str) -> dict:
        if self._primp is None:
            return {}
        try:
            return self._primp.get_cookies(url)
        except Exception:
            return {}

    def close(self):
        """Signal the browser worker to close and wait for it."""
        try:
            self._cmd_q.put({"action": self._CMD_CLOSE})
            self._thread.join(timeout=15)
        except Exception:
            pass


# ── External CAPTCHA solver (synchronous, for browser fallback) ───────────────

def _capsolver_token_sync(api_key: str, sitekey: str, page_url: str,
                          proxy: str | None = None, timeout: int = 120) -> str:
    """
    Call CapSolver to solve a reCAPTCHA v2 Enterprise checkbox in the background.
    Uses ReCaptchaV2EnterpriseTask (proxy-aware) or ProxyLess variant.
    Returns the gRecaptchaResponse token string, or raises on failure.
    """
    import urllib.request as _req
    import json as _j

    task: dict = {
        "type":       "ReCaptchaV2EnterpriseTask",
        "websiteURL": page_url,
        "websiteKey": sitekey,
    }
    if proxy:
        try:
            from urllib.parse import urlparse, unquote
            p = urlparse(proxy)
            scheme = (p.scheme or "http").lower()
            task["proxyType"]    = "socks5" if "socks" in scheme else "http"
            task["proxyAddress"] = p.hostname or ""
            task["proxyPort"]    = p.port or 80
            if p.username:
                task["proxyLogin"]    = p.username
                task["proxyPassword"] = unquote(p.password or "")
        except Exception:
            pass

    def _post(url: str, body: dict) -> dict:
        data = _j.dumps(body).encode()
        r = _req.Request(url, data=data, headers={"Content-Type": "application/json"})
        return _j.loads(_req.urlopen(r, timeout=15).read())

    result = _post("https://api.capsolver.com/createTask",
                   {"clientKey": api_key, "task": task})
    task_id = result.get("taskId")
    if not task_id:
        raise RuntimeError(f"CapSolver createTask failed: {result}")

    get_body = {"clientKey": api_key, "taskId": task_id}
    deadline = time.time() + timeout
    while time.time() < deadline:
        time.sleep(4)
        res = _post("https://api.capsolver.com/getTaskResult", get_body)
        if res.get("status") == "ready":
            return res["solution"]["gRecaptchaResponse"]
        if res.get("status") == "failed":
            raise RuntimeError(f"CapSolver task failed: {res}")
    raise RuntimeError("CapSolver: timed out waiting for result")


# ── Proxy URL parser ──────────────────────────────────────────────────────────

def _parse_proxy(proxy_url: str | None) -> tuple[str | None, str | None, str | None]:
    """Split proxy URL into (server, username, password) for Playwright."""
    if not proxy_url:
        return None, None, None
    try:
        from urllib.parse import urlparse, unquote
        p = urlparse(proxy_url)
        server = f"{p.scheme or 'http'}://{p.hostname}:{p.port or 80}"
        return server, unquote(p.username or ""), unquote(p.password or "")
    except Exception:
        return proxy_url, None, None


# ── Public entry point ────────────────────────────────────────────────────────

def get_session(proxy: str | None = None,
                inject_cookies: dict | None = None,
                headless: bool = True) -> PlaywrightSession:
    """
    Launch a Chrome session. If inject_cookies is provided, the saved session
    cookies are pre-loaded before navigation — the WAF challenge is skipped.
    Pass headless=False to open a visible browser window (for manual inspection).
    """
    return PlaywrightSession(proxy, inject_cookies=inject_cookies, headless=headless)
