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
from pathlib import Path

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
    """Detect the proxy exit-IP's timezone via ipwho.is. Falls back to Europe/Lisbon
    (all configured proxies are Portuguese ISP/SOAX-PT)."""
    _DEFAULT = "Europe/Lisbon"
    if not proxy_url:
        return _DEFAULT
    for svc in ("https://ipwho.is/json/", "https://ipapi.co/json/"):
        try:
            import primp
            client = primp.Client(impersonate=IMPERSONATE, proxy=proxy_url,
                                  follow_redirects=True, verify=False, timeout=8)
            r = client.get(svc)
            data = r.json()
            tz = data.get("timezone", {})
            if isinstance(tz, dict):
                tz = tz.get("id", "")
            tz = str(tz).strip()
            if tz and "/" in tz:
                print(f"[session] tz lookup OK: {tz}")
                return tz
        except Exception as _e:
            pass
    print(f"[session] tz lookup failed — using {_DEFAULT}")
    return _DEFAULT


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
    import random as _r
    # Per-session random noise values (makes canvas/WebGL fingerprint unique)
    _noise = _r.randint(1, 9)
    _gl_vendor  = _r.choice(["Intel Inc.", "NVIDIA Corporation", "AMD"])
    _gl_renderer = _r.choice([
        "Intel Iris OpenGL Engine",
        "ANGLE (Intel, Intel(R) UHD Graphics Direct3D11 vs_5_0 ps_5_0)",
        "ANGLE (NVIDIA, NVIDIA GeForce GTX 1060 Direct3D11 vs_5_0 ps_5_0)",
    ])
    return f"""
// Hide automation signals
Object.defineProperty(navigator,'webdriver',{{get:()=>undefined}});
delete navigator.__proto__.webdriver;

// Fake chrome object (real Chrome has this)
window.chrome={{runtime:{{}},loadTimes:function(){{}},csi:function(){{}},app:{{}}}};

// Realistic plugin list
Object.defineProperty(navigator,'plugins',{{get:()=>{{
  const p=[
    {{name:'Chrome PDF Plugin',filename:'internal-pdf-viewer',description:'Portable Document Format'}},
    {{name:'Chrome PDF Viewer',filename:'mhjfbmdgcfjbbpaeojofohoefgiehjai',description:''}},
    {{name:'Native Client',filename:'internal-nacl-plugin',description:''}}
  ];
  p.__proto__=PluginArray.prototype;
  return p;
}}}});

// Languages
Object.defineProperty(navigator,'languages',{{get:()=>['pt-PT','pt','en-US','en']}});

// Permissions API patch
const _origQuery=window.navigator.permissions.query;
window.navigator.permissions.query=(p)=>p.name==='notifications'
  ?Promise.resolve({{state:'denied'}}):_origQuery(p);

// Hardware / screen
Object.defineProperty(navigator,'hardwareConcurrency',{{get:()=>4}});
Object.defineProperty(navigator,'deviceMemory',{{get:()=>8}});
Object.defineProperty(screen,'colorDepth',{{get:()=>24}});
Object.defineProperty(screen,'width',{{get:()=>{vp_w}}});
Object.defineProperty(screen,'height',{{get:()=>{vp_h}}});

// Canvas fingerprint randomization (unique per session — prevents fingerprint tracking)
(function(){{
  const _noise = {_noise};
  const _oTDU = HTMLCanvasElement.prototype.toDataURL;
  HTMLCanvasElement.prototype.toDataURL = function(t,...a){{
    const ctx = this.getContext('2d');
    if(ctx && (t==='image/png'||!t)){{
      const w=this.width||300,h=this.height||150;
      if(w>0&&h>0){{
        ctx.fillStyle='rgba(255,255,255,0.0'+_noise+')';
        ctx.fillRect(0,0,1,1);
      }}
    }}
    return _oTDU.apply(this,[t,...a]);
  }};
  const _oGetCtx = HTMLCanvasElement.prototype.getContext;
  HTMLCanvasElement.prototype.getContext = function(t,...a){{
    const ctx = _oGetCtx.apply(this,[t,...a]);
    if(ctx&&t==='2d'){{
      const _oFT=ctx.fillText.bind(ctx);
      ctx.fillText=function(tx,x,y,...rest){{
        ctx.save();
        ctx.shadowColor='rgba('+_noise+','+_noise+','+_noise+',0.01)';
        _oFT(tx,x,y,...rest);
        ctx.restore();
      }};
    }}
    return ctx;
  }};
}})();

// WebGL fingerprint (vendor/renderer)
(function(){{
  const _oGPA = WebGLRenderingContext.prototype.getParameter;
  WebGLRenderingContext.prototype.getParameter = function(p){{
    if(p===37445) return '{_gl_vendor}';
    if(p===37446) return '{_gl_renderer}';
    return _oGPA.call(this,p);
  }};
  if(window.WebGL2RenderingContext){{
    const _oGPA2 = WebGL2RenderingContext.prototype.getParameter;
    WebGL2RenderingContext.prototype.getParameter = function(p){{
      if(p===37445) return '{_gl_vendor}';
      if(p===37446) return '{_gl_renderer}';
      return _oGPA2.call(this,p);
    }};
  }}
}})();
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

    _CMD_VERIFY     = "verify"
    _CMD_FETCH      = "fetch"
    _CMD_LOGIN      = "login"
    _CMD_EVAL       = "eval"
    _CMD_NAV        = "nav"
    _CMD_SAVE_STATE = "save_state"
    _CMD_CLOSE      = "close"

    def __init__(self, proxy: str | None, inject_cookies: dict | None = None,
                 headless: bool = True, storage_state: str | None = None,
                 fp: tuple | None = None):
        self.proxy           = proxy          # original URL — used by primp + CapSolver
        self._inject_cookies = inject_cookies or {}   # pre-load cookies → skip WAF challenge
        self._headless       = headless
        self._storage_state  = storage_state  # Playwright storage_state path for session restore
        self._fp_override    = fp             # fixed fingerprint tuple for session restoration
        self._fp: tuple | None = None         # set by _worker once fingerprint is chosen
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

            if self._fp_override:
                ua, sec_ch_ua, vp_w, vp_h, platform = self._fp_override
                print(f"[session] fp restored  ua=...{ua[55:85]}  vp={vp_w}x{vp_h}")
            else:
                ua, sec_ch_ua, vp_w, vp_h, platform = _pick_fingerprint()
            self._fp = (ua, sec_ch_ua, vp_w, vp_h, platform)
            tz = _get_proxy_timezone(self.proxy)
            print(f"[session] fp ua=...{ua[55:85]}  tz={tz}  vp={vp_w}x{vp_h}")

            pw = sync_playwright().start()
            browser = pw.chromium.launch(
                headless=self._headless,
                args=["--disable-blink-features=AutomationControlled",
                      "--disable-dev-shm-usage", "--no-sandbox"],
                proxy=proxy_config,
            )
            ctx_kwargs = dict(
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
            if self._storage_state:
                ctx_kwargs["storage_state"] = self._storage_state
                print(f"[session] restoring session from {self._storage_state}")
            ctx = browser.new_context(**ctx_kwargs)
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

            # Apply playwright-stealth for comprehensive bot-detection evasion.
            # Patches 20+ signals: canvas getImageData noise, audio fingerprint,
            # getBoundingClientRect, Function.toString, webdriver, chrome runtime, etc.
            try:
                from playwright_stealth import Stealth
                Stealth().apply_stealth_sync(page)
                print("[session] playwright-stealth applied")
            except ImportError:
                print("[session] playwright-stealth not installed — using fallback script only")
            except Exception as _se:
                print(f"[session] playwright-stealth warning: {_se}")

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

                elif cmd.get("action") == self._CMD_EVAL:
                    try:
                        result = page.evaluate(cmd["js"])
                        self._res_q.put(("ok", result))
                    except Exception as exc:
                        self._res_q.put(("err", str(exc)))

                elif cmd.get("action") == self._CMD_NAV:
                    try:
                        page.goto(cmd["url"], timeout=cmd.get("nav_timeout", 30000))
                        page.wait_for_load_state("networkidle", timeout=15000)
                        self._res_q.put(("ok", page.url))
                    except Exception as exc:
                        try:
                            self._res_q.put(("ok", page.url))
                        except Exception:
                            self._res_q.put(("err", str(exc)))

                elif cmd.get("action") == self._CMD_SAVE_STATE:
                    try:
                        ctx.storage_state(path=cmd["path"])
                        # Write companion profile so the same fingerprint can be
                        # restored on next get_session_from_state() call.
                        if self._fp:
                            import json as _js
                            profile = {
                                "user_agent": self._fp[0],
                                "sec_ch_ua":  self._fp[1],
                                "viewport_w": self._fp[2],
                                "viewport_h": self._fp[3],
                                "platform":   self._fp[4],
                                "proxy":      self.proxy,
                                "saved_at":   time.time(),
                            }
                            p = Path(cmd["path"])
                            profile_path = p.parent / (p.stem + "_profile.json")
                            profile_path.write_text(_js.dumps(profile, indent=2), encoding="utf-8")
                        self._res_q.put(("ok", cmd["path"]))
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
        safe_body = body[:80].encode("utf-8", errors="replace").decode("utf-8", errors="replace")
        print(f"[session] browser_fetch {method} {url.split('?')[0].split('/')[-1]} "
              f"status={result.get('status')}  body={safe_body}")
        return {"status": result.get("status", 0), "body": body}

    def _solve_audio_challenge(self, page, twocaptcha_key: str) -> str:
        """
        When a reCAPTCHA image challenge is showing, switch to audio challenge,
        download the audio file, send to 2captcha audio endpoint, type the
        transcription, and submit. Returns the reCAPTCHA token on success or "".
        Token from real browser challenge interaction scores higher than injected tokens.
        """
        print(f"[session] audio_challenge: attempting")
        try:
            ch_frame = page.frame_locator(
                'iframe[title*="recaptcha challenge"], iframe[src*="bframe"]'
            ).first

            # Click audio button
            audio_btn = ch_frame.locator("#recaptcha-audio-button")
            if not audio_btn.is_visible(timeout=5000):
                print(f"[session] audio_challenge: audio button not visible")
                return ""
            audio_btn.click()
            page.wait_for_timeout(2000)

            # Check for "too many requests" error
            try:
                err_vis = ch_frame.locator(".rc-doscaptcha-body").is_visible(timeout=1000)
                if err_vis:
                    print(f"[session] audio_challenge: too-many-requests error")
                    return ""
            except Exception:
                pass

            # Get audio source URL
            audio_src = ""
            for sel in [
                "audio.rc-audiochallenge-audio-src",
                ".rc-audiochallenge-tdownload-link",
                "audio[src]",
            ]:
                try:
                    el = ch_frame.locator(sel).first
                    if el.count() > 0:
                        audio_src = el.get_attribute("src", timeout=3000) or \
                                    el.get_attribute("href", timeout=3000) or ""
                        if audio_src:
                            break
                except Exception:
                    pass

            if not audio_src:
                print(f"[session] audio_challenge: no audio source found")
                return ""
            print(f"[session] audio_challenge: audio_src={audio_src[:80]}")

            # Download audio via proxy
            import primp as _primp
            audio_client = _primp.Client(proxy=self.proxy, verify=False,
                                         follow_redirects=True, timeout=20)
            r = audio_client.get(audio_src)
            audio_bytes = r.content
            if not audio_bytes:
                print(f"[session] audio_challenge: empty audio response")
                return ""
            print(f"[session] audio_challenge: downloaded {len(audio_bytes)} bytes")

            # Submit to 2captcha audio solver
            if not twocaptcha_key:
                print(f"[session] audio_challenge: no 2captcha key — cannot solve audio")
                return ""
            import base64, requests as _req
            audio_b64 = base64.b64encode(audio_bytes).decode()
            submit_r = _req.post(
                "https://2captcha.com/in.php",
                data={"key": twocaptcha_key, "method": "audio",
                      "lang": "en", "body": audio_b64},
                timeout=30,
            )
            submit_text = submit_r.text.strip()
            if not submit_text.startswith("OK|"):
                print(f"[session] audio_challenge: submit error: {submit_text}")
                return ""
            task_id = submit_text.split("|", 1)[1]
            print(f"[session] audio_challenge: task_id={task_id}")

            # Poll for result
            transcription = ""
            for poll in range(30):
                time.sleep(5)
                poll_r = _req.get(
                    f"https://2captcha.com/res.php?key={twocaptcha_key}"
                    f"&action=get&id={task_id}",
                    timeout=15,
                )
                pt = poll_r.text.strip()
                if pt == "CAPCHA_NOT_READY":
                    continue
                if pt.startswith("OK|"):
                    transcription = pt.split("|", 1)[1].strip().lower()
                    print(f"[session] audio_challenge: transcription={transcription!r}")
                    break
                print(f"[session] audio_challenge: poll error: {pt}")
                return ""

            if not transcription:
                print(f"[session] audio_challenge: timed out waiting for transcription")
                return ""

            # Type transcription into audio input field and click verify
            audio_input = ch_frame.locator("#audio-response")
            audio_input.fill(transcription, timeout=5000)
            page.wait_for_timeout(500)
            verify_btn = ch_frame.locator("#recaptcha-verify-button")
            verify_btn.click(timeout=5000)
            page.wait_for_timeout(3000)

            # Check if token was generated (challenge solved)
            token = ""
            for _ in range(15):
                try:
                    token = page.evaluate("grecaptcha.enterprise.getResponse()") or ""
                    if token:
                        break
                except Exception:
                    pass
                page.wait_for_timeout(1000)

            if token:
                print(f"[session] audio_challenge: token obtained  len={len(token)}")
            else:
                print(f"[session] audio_challenge: no token after verify (wrong answer?)")
            return token

        except Exception as e:
            print(f"[session] audio_challenge: error: {e}")
            return ""

    def _do_browser_login(self, page, cmd: dict) -> dict:
        """
        Navigate fresh to AUTH_URL, type credentials to build reCAPTCHA behavioral
        signals, click the checkbox, then submit login via jQuery $.ajax.
        Falls back to an external CapSolver token if the image challenge appears.
        Returns {"status": int, "body": str}.
        """
        username          = cmd["username"]
        password          = cmd["password"]
        lang              = cmd.get("lang", "PT")
        solver_key        = cmd.get("solver_key", "")
        capsolver_keys    = cmd.get("capsolver_keys", [solver_key] if solver_key else [])
        anticaptcha_keys  = cmd.get("anticaptcha_keys", [])
        twocaptcha_keys   = cmd.get("twocaptcha_keys", [])
        capmonster_keys   = cmd.get("capmonster_keys", [])
        skip_checkbox     = cmd.get("skip_checkbox", False)
        min_score         = cmd.get("min_score", 80)

        # Navigate to auth page — reuse existing page if already there (avoids WAF retrigger),
        # otherwise go HOME_URL → AUTH_URL to match session init pattern.
        try:
            cur_url = page.url
        except Exception:
            cur_url = ""
        already_on_auth = AUTH_URL in cur_url or cur_url.endswith("/Authentication.jsp")
        # Check if form is clean and usable: username input present AND no challenge overlay
        form_ready = False
        if already_on_auth:
            try:
                form_ready = page.locator('input[name="username"]').count() > 0
            except Exception:
                pass
            if form_ready:
                try:
                    # Challenge iframe overlay blocks field clicks — must navigate fresh
                    ch_vis = page.locator(
                        'iframe[title*="recaptcha challenge"], iframe[src*="bframe"]'
                    ).is_visible(timeout=500)
                    if ch_vis:
                        form_ready = False
                        print(f"[session] browser_login: challenge overlay visible — navigating fresh")
                except Exception:
                    pass
        if form_ready:
            print(f"[session] browser_login: reusing existing AUTH_URL page  user={username}")
            time.sleep(random.uniform(0.5, 1.0))
        else:
            print(f"[session] browser_login: nav HOME_URL -> AUTH_URL  user={username}")
            page.goto(HOME_URL, timeout=30000)
            page.wait_for_load_state("networkidle", timeout=15000)
            page.goto(AUTH_URL, timeout=25000)
            page.wait_for_load_state("networkidle", timeout=15000)
            time.sleep(random.uniform(1.2, 2.0))

        # Debug: dump reCAPTCHA widget configuration to confirm action name
        try:
            rc_info = page.evaluate("""
            () => {
                const info = {};
                // data-action attributes on reCAPTCHA widget divs
                const divs = document.querySelectorAll('[data-sitekey],[data-action],[class*="recaptcha"]');
                info.widgets = Array.from(divs).map(el => ({
                    tag: el.tagName, id: el.id,
                    dataAction: el.getAttribute('data-action'),
                    dataSitekey: el.getAttribute('data-sitekey') ? el.getAttribute('data-sitekey').slice(0,12)+'...' : null,
                    dataCallback: el.getAttribute('data-callback'),
                }));
                // Look for grecaptcha render args in script content
                const scripts = Array.from(document.querySelectorAll('script:not([src])'));
                const hits = [];
                for (const s of scripts) {
                    const t = s.textContent;
                    const m = t.match(/action\\s*[:=]\\s*['"]([^'"]{1,40})['"]/g);
                    if (m) hits.push(...m);
                    const m2 = t.match(/grecaptcha[^;]{0,120}/g);
                    if (m2) hits.push(...m2.slice(0,5));
                }
                info.scriptHits = hits.slice(0, 20);
                return info;
            }
            """)
            print(f"[session] rcaptcha_info: {_json.dumps(rc_info)[:600]}")
        except Exception as e:
            print(f"[session] rcaptcha_info error: {e}")

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
                time.sleep(random.uniform(0.5, 2.0))
        except Exception:
            pass

        token = ""
        challenge_detected = False

        if skip_checkbox:
            # Skip checkbox click and 25s polling — go straight to external solver.
            # race_all() (single parallel race) is faster than race_best() (3 sequential
            # races) and produces the same token quality for LOGIN_EVISA.
            print(f"[session] browser_login: skip_checkbox=True — silent token injection")
        else:
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

        # External solver — race all available services (ProxyLess).
        # Audio challenge is intentionally skipped: clicking the audio button and
        # downloading .mp3 files through the proxy are known bot signals that degrade
        # the IP's reCAPTCHA trust score. External ProxyLess tokens work without it.
        if not token:
            any_keys = capsolver_keys or anticaptcha_keys or twocaptcha_keys or capmonster_keys
            if any_keys:
                import solver as _solver
                if skip_checkbox:
                    print(f"[session] race_all (min_score={min_score}, skip_checkbox mode)")
                    try:
                        token = _solver.race_all(
                            capsolver_keys, anticaptcha_keys, twocaptcha_keys, capmonster_keys,
                            "LOGIN_EVISA", proxy=None, min_score=min_score,
                        )
                        print(f"[session] race_all token obtained  len={len(token)}  score={_solver.score_token(token)}")
                    except Exception as e:
                        print(f"[session] race_all failed: {e}")
                else:
                    print(f"[session] no audio token — race_best x3 external solvers (proxyless)")
                    try:
                        token = _solver.race_best(
                            capsolver_keys, anticaptcha_keys, twocaptcha_keys, capmonster_keys,
                            "LOGIN_EVISA", proxy=None, n_races=3, min_score=85,
                        )
                        print(f"[session] race_best token obtained  len={len(token)}  score={_solver.score_token(token)}")
                    except Exception as e:
                        print(f"[session] race_all failed: {e}")

        # Last-resort: read injected textarea value
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

        tok_js = _json.dumps(token)

        # Set the reCAPTCHA textarea value (makes getResponse() return token)
        page.evaluate(f"""
        (() => {{
            const _tok = {tok_js};
            for (const sel of ['#g-recaptcha-response-1', '#g-recaptcha-response']) {{
                const ta = document.querySelector(sel);
                if (!ta) continue;
                const setter = Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, 'value')?.set;
                if (setter) setter.call(ta, _tok); else ta.value = _tok;
                ta.dispatchEvent(new Event('input',  {{bubbles: true}}));
                ta.dispatchEvent(new Event('change', {{bubbles: true}}));
            }}
        }})()
        """)
        print(f"[session] token set in textarea")

        # Override grecaptcha.enterprise.getResponse so doLogin() reads our token.
        # The internal reCAPTCHA widget state is separate from the textarea DOM value;
        # getResponse() reads widget state, not the textarea. Without this override
        # doLogin() sends an empty captchaResponse and the server returns type:error.
        page.evaluate(f"""
        (() => {{
            const _tok = {tok_js};
            const _patch = (obj) => {{
                if (!obj) return;
                const _orig = obj.getResponse;
                obj.getResponse = function(opt_widget_id) {{
                    const r = _orig ? _orig.call(this, opt_widget_id) : '';
                    return r || _tok;
                }};
            }};
            if (window.grecaptcha) {{
                _patch(window.grecaptcha);
                if (window.grecaptcha.enterprise) _patch(window.grecaptcha.enterprise);
            }}
        }})()
        """)
        print(f"[session] grecaptcha.getResponse patched to return injected token")

        # POST login credentials directly via browser fetch() — uses the browser's
        # session cookies and TLS fingerprint without any page interaction.
        uname_js   = _json.dumps(username)
        passwd_js  = _json.dumps(password)
        lang_js    = _json.dumps(lang)
        login_url  = BASE + "/VistosOnline/login"
        login_url_js = _json.dumps(login_url)

        login_fetch_js = f"""
        async () => {{
            const resp = await fetch({login_url_js}, {{
                method: "POST",
                headers: {{
                    "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
                    "X-Requested-With": "XMLHttpRequest",
                    "Accept": "*/*",
                }},
                body: new URLSearchParams({{
                    "username":        {uname_js},
                    "password":        {passwd_js},
                    "language":        {lang_js},
                    "rgpd":            "Y",
                    "captchaResponse": {tok_js},
                }}).toString(),
            }});
            const text = await resp.text();
            return {{ status: resp.status, body: text }};
        }}
        """
        try:
            result = page.evaluate(login_fetch_js)
        except Exception as e:
            print(f"[session] login fetch failed: {e}")
            return {"status": 0, "body": ""}

        status = result.get("status", 0)
        body   = result.get("body", "")
        safe_body = body[:80].encode("utf-8", errors="replace").decode("utf-8", errors="replace")
        print(f"[session] /login POST status={status}  body={safe_body!r}")

        # Confirm success: if server response looks like success, verify page navigated to HOME_URL
        parsed_type = None
        if body:
            try:
                parsed_type = _json.loads(body).get("type", "")
            except Exception:
                parsed_type = None  # HTML/WAF body — return as-is for WAF detection

        if status == 200 and parsed_type in ("", "200", "success"):
            page.wait_for_timeout(2000)
            cur_url = page.url
            at_home = cur_url == HOME_URL or cur_url.rstrip("/") == HOME_URL.rstrip("/")
            print(f"[session] post-login url check: {cur_url}  at_home={at_home}")
            if not at_home:
                print(f"[session] navigating to HOME_URL to confirm session")
                try:
                    page.goto(HOME_URL, timeout=20000)
                    page.wait_for_load_state("networkidle", timeout=10000)
                    landed  = page.url
                    content = page.content()
                    is_auth      = "Authentication.jsp" in landed or "/login" in landed
                    has_challenge = "/ch/bd.js" in content
                    if is_auth:
                        # Portal redirected back to login — session not established
                        print(f"[session] HOME_URL redirected to auth at {landed} — not authenticated")
                        body = '{"type":"error","description":"no_redirect"}'
                    elif has_challenge:
                        # HOME_URL got WAF-challenged — session IS established,
                        # WAF challenge on this IP is independent of login state
                        print(f"[session] HOME_URL has WAF challenge but session was established — success")
                        body = '{"type":"success","description":"nav_confirmed_waf"}'
                    else:
                        print(f"[session] HOME_URL confirmed at {landed} — success")
                        body = '{"type":"success","description":"nav_confirmed"}'
                except Exception as nav_err:
                    print(f"[session] HOME_URL nav failed: {nav_err}")
                    body = '{"type":"error","description":"no_redirect"}'

        return {"status": status, "body": body, "captcha_token": token}

    # ── Public interface ──────────────────────────────────────────────────────

    @property
    def client(self):
        """The underlying primp.Client — used by session_store and batch_apply."""
        return self._primp

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
                      solver_key: str = "", twocaptcha_key: str = "",
                      capsolver_keys: list | None = None,
                      anticaptcha_keys: list | None = None,
                      twocaptcha_keys: list | None = None,
                      capmonster_keys: list | None = None,
                      timeout: int = 300,
                      skip_checkbox: bool = False,
                      min_score: int = 80) -> dict:
        """
        Type credentials into the login form to build behavioral signals, click the
        reCAPTCHA checkbox, and submit. Races all solver services (ProxyLess) for token.
        skip_checkbox=True: skip click + 25s poll, go straight to race_all() injection.
        min_score: minimum reCAPTCHA score to accept (0=any, 80=default, 100=2captcha only).
        Returns {"status": int, "body": str}.
        """
        self._cmd_q.put({
            "action":          self._CMD_LOGIN,
            "username":        username,
            "password":        password,
            "lang":            lang,
            "solver_key":      solver_key,
            "twocaptcha_key":  twocaptcha_key,
            "capsolver_keys":  capsolver_keys or ([solver_key] if solver_key else []),
            "anticaptcha_keys": anticaptcha_keys or [],
            "twocaptcha_keys": twocaptcha_keys or ([twocaptcha_key] if twocaptcha_key else []),
            "capmonster_keys": capmonster_keys or [],
            "skip_checkbox":   skip_checkbox,
            "min_score":       min_score,
        })
        try:
            status, result = self._res_q.get(timeout=timeout)
        except queue.Empty:
            raise RuntimeError(f"browser_login timed out after {timeout}s")
        if status == "err":
            raise RuntimeError(f"browser_login failed: {result}")
        return result

    def browser_eval(self, js: str, timeout: int = 30):
        """Execute arbitrary JS in the live browser page and return the result."""
        self._cmd_q.put({"action": self._CMD_EVAL, "js": js})
        try:
            status, result = self._res_q.get(timeout=timeout)
        except queue.Empty:
            raise RuntimeError(f"browser_eval timed out after {timeout}s")
        if status == "err":
            raise RuntimeError(f"browser_eval failed: {result}")
        return result

    def browser_nav(self, url: str, timeout: int = 60) -> str:
        """Navigate the browser page to url and return the final URL after load."""
        self._cmd_q.put({"action": self._CMD_NAV, "url": url, "nav_timeout": timeout * 1000})
        try:
            status, result = self._res_q.get(timeout=timeout + 5)
        except queue.Empty:
            raise RuntimeError(f"browser_nav timed out after {timeout}s")
        if status == "err":
            raise RuntimeError(f"browser_nav failed: {result}")
        return result

    def save_state(self, path) -> None:
        """Export cookies + localStorage to a JSON file via Playwright storage_state."""
        self._cmd_q.put({"action": self._CMD_SAVE_STATE, "path": str(path)})
        try:
            status, result = self._res_q.get(timeout=30)
        except queue.Empty:
            raise RuntimeError("save_state timed out after 30s")
        if status == "err":
            raise RuntimeError(f"save_state failed: {result}")
        print(f"[session] session state saved → {result}")

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
                          proxy: str | None = None, timeout: int = 120,
                          cookies: str | None = None,
                          user_agent: str | None = None) -> str:
    """
    Call CapSolver to solve a reCAPTCHA v2 Enterprise checkbox in the background.
    Uses ReCaptchaV2EnterpriseTask (proxy-aware) or ProxyLess variant.
    Returns the gRecaptchaResponse token string, or raises on failure.

    cookies: browser cookie header string (e.g. "Vistos_sid=abc; cookiesession1=xyz").
             Passed to CapSolver so the token is generated inside the same server
             session — the Enterprise reCAPTCHA token will carry the session signals.
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
    if cookies:
        task["cookies"] = cookies
    if user_agent:
        task["userAgent"] = user_agent

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
                headless: bool = True,
                storage_state: str | None = None,
                fp: tuple | None = None) -> PlaywrightSession:
    """
    Launch a Chrome session. If inject_cookies is provided, the saved session
    cookies are pre-loaded before navigation — the WAF challenge is skipped.
    Pass headless=False to open a visible browser window (for manual inspection).
    Pass storage_state=path to restore a previously saved session (cookies + localStorage).
    Pass fp=(ua, sec_ch_ua, vp_w, vp_h, platform) to use a fixed fingerprint.
    """
    return PlaywrightSession(proxy, inject_cookies=inject_cookies,
                             headless=headless, storage_state=storage_state, fp=fp)


def get_session_from_state(state_path, headless: bool = False) -> PlaywrightSession:
    """
    Open a browser pre-loaded with a previously saved session state (from save_state()).
    No proxy needed — session cookies are embedded in the state file.
    headless=False by default so the user sees the browser window.
    Loads <state_stem>_profile.json alongside the state file to restore the original
    browser fingerprint (user-agent, viewport, platform) so the server sees the same
    fingerprint as during login.
    """
    state_path = Path(state_path)
    profile_path = state_path.parent / (state_path.stem + "_profile.json")
    fp = None
    proxy = None
    if profile_path.exists():
        try:
            prof = _json.loads(profile_path.read_text(encoding="utf-8"))
            fp    = (prof["user_agent"], prof["sec_ch_ua"],
                     prof["viewport_w"], prof["viewport_h"], prof["platform"])
            proxy = prof.get("proxy")
            print(f"[session] profile loaded from {profile_path.name}  ua=...{fp[0][55:85]}")
        except Exception as e:
            print(f"[session] profile load failed ({e}) — using random fingerprint")
    return PlaywrightSession(proxy=proxy, headless=headless,
                             storage_state=str(state_path), fp=fp)
