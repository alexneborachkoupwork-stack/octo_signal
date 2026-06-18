"""
SlotSignalBus — resettable asyncio.Event + HTTP override server.

Replaces the single-shot event in SlotDetector with a resettable design
that survives multiple signal rounds (all-no-slot → reset → scouts resume → next round).

The signal carries NO slot data. A scout's (or a human's) only job is to say
"something is available, go check" — each waking real worker does its own fresh
/slots POST (with its own fresh CAPTCHA token) inside apply_book() when it wakes,
since that's the only way to get a non-stale, CAPTCHA-gated answer anyway. Carrying
slot data through the signal would just be a second, redundant, potentially-stale
data path alongside the one that already has to exist.

HTTP server on :8989:
  POST /signal              body ignored — pure wake-up trigger
  POST /signal/reset
  GET  /status
  GET  /probe_slots         ?username=X&posto=3088
                            Probes /slots using the saved session checkpoint for
                            that account. No login, no browser, no worker impact.
                            Returns JSON with the raw server response.
"""

import asyncio
import logging

log = logging.getLogger("app.core.signals")


class SlotSignalBus:
    def __init__(self, slot_manager):
        self._slot_manager = slot_manager
        self._event        = asyncio.Event()
        self._fired_count  = 0

    # ── Worker-facing API ──────────────────────────────────────────────────────

    async def wait(self) -> None:
        """Block until the next slot signal (used by real workers)."""
        await self._event.wait()

    def fire(self) -> None:
        """
        Called by scout workers when poll_slots_once() finds availability (truthy
        result) — NOT given the slots themselves, just the fact that some exist.
        Wakes all waiting real workers. Idempotent: a second fire() while already
        set is a no-op besides the (suppressed) count.
        """
        if not self._event.is_set():
            self._fired_count += 1
            log.info(f"[signal] fired (round #{self._fired_count})")
        self._event.set()

    def reset(self) -> None:
        """
        Called by Manager after all real workers return no_slot.
        Clears the event so workers re-enter wait().
        Scout polling loops are unaffected — they keep running independently.
        """
        self._event.clear()
        log.info("[signal] reset — scouts continue, real workers re-enter wait")

    def fire_synthetic(self) -> None:
        """
        External signal (human via HTTP / Telegram / a separate app.scout process).
        Pure wake-up — wakes all warmed real workers regardless of scout state.
        Each worker fetches its own fresh /slots data when it wakes; this call
        carries no slot data because there is none to carry.
        """
        if not self._event.is_set():
            self._fired_count += 1
            log.info(f"[signal] synthetic signal fired (round #{self._fired_count})")
        self._event.set()

    @property
    def fired_count(self) -> int:
        return self._fired_count

    # ── HTTP override server ───────────────────────────────────────────────────

    async def serve_http(self, port: int = 8989) -> None:
        """
        Tiny aiohttp server on :port.
        POST /signal  body ignored — pure wake-up trigger
        GET  /status  returns JSON stats
        Runs forever until cancelled.
        """
        try:
            from aiohttp import web
        except ImportError:
            log.warning("[signal] aiohttp not installed — HTTP signal server disabled")
            await asyncio.sleep(3600 * 24)  # keep coroutine alive without exiting gather
            return

        async def handle_signal(request: web.Request) -> web.Response:
            self.fire_synthetic()
            return web.json_response({
                "ok": True,
                "fired_count": self._fired_count,
            })

        async def handle_status(request: web.Request) -> web.Response:
            return web.json_response({
                "slot_manager":  self._slot_manager.stats(),
                "fired_count":   self._fired_count,
                "event_set":     self._event.is_set(),
            })

        async def handle_reset(request: web.Request) -> web.Response:
            was_set = self._event.is_set()
            self.reset()
            log.info("[signal] manual reset via HTTP")
            return web.json_response({"ok": True, "was_set": was_set})

        async def handle_probe_slots(request: web.Request) -> web.Response:
            """
            Probe /slots for a saved session without touching the running worker.
            Loads checkpoint cookies, solves a fresh CAPTCHA, POSTs to /slots,
            returns the raw server response as JSON.

            Query params:
              username  — account username (must have a schedule_jsp checkpoint)
              posto     — posto_id to probe (default 3088)
              attempts  — how many proxy rotations to try (default 3)
            """
            import concurrent.futures
            import json as _json
            import re as _re
            import time as _time
            from pathlib import Path as _Path

            username = request.rel_url.query.get("username", "").strip()
            posto_id = request.rel_url.query.get("posto", "3088").strip()
            attempts = int(request.rel_url.query.get("attempts", "3"))

            if not username:
                return web.json_response({"error": "missing ?username="}, status=400)

            # ── Load checkpoint ────────────────────────────────────────────────
            try:
                import session_store
                meta = session_store.load_meta(username)
            except Exception as e:
                return web.json_response({"error": f"session_store error: {e}"}, status=500)

            if not meta:
                return web.json_response(
                    {"error": f"no saved session for '{username}' — worker must be warmed first"},
                    status=404)

            checkpoint = meta.get("checkpoint", "")
            cookies    = meta.get("cookies", {})
            proxy      = meta.get("proxy", "")

            if not cookies:
                return web.json_response(
                    {"error": f"checkpoint has no cookies (checkpoint={checkpoint!r})"}, status=422)

            # ── Load CAPTCHA keys from .env ────────────────────────────────────
            _env_path = _Path(__file__).parent / ".env"
            env: dict = {}
            if _env_path.exists():
                for _line in _env_path.read_text(encoding="utf-8").splitlines():
                    _line = _line.strip()
                    if not _line or _line.startswith("#") or "=" not in _line:
                        continue
                    _k, _, _v = _line.partition("=")
                    env[_k.strip()] = _v.strip()

            def _kl(k: str) -> list[str]:
                return [x.strip() for x in env.get(k, "").split(",") if x.strip()]

            capsolver_keys   = _kl("CAPSOLVER_KEYS")
            anticaptcha_keys = _kl("ANTICAPTCHA_KEYS")
            twocaptcha_keys  = _kl("TWOCAPTCHA_KEYS")
            capmonster_keys  = _kl("CAPMONSTER_KEYS")

            if not any([capsolver_keys, anticaptcha_keys, twocaptcha_keys, capmonster_keys]):
                return web.json_response({"error": "no CAPTCHA keys in .env"}, status=500)

            BASE_URL  = "https://pedidodevistos.mne.gov.pt"
            SLOTS_URL = BASE_URL + "/VistosOnline/slots"
            SCHED_REF = BASE_URL + f"/VistosOnline/Schedule.jsp?posto_id={posto_id}"

            results = []
            loop = asyncio.get_event_loop()

            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as _ex:

                def _probe_one(attempt_proxy: str, label: str) -> dict:
                    t0 = _time.time()
                    # Solve CAPTCHA
                    try:
                        import solver as sol
                        token = sol.race_all(
                            capsolver_keys, anticaptcha_keys, twocaptcha_keys, capmonster_keys,
                            "SCHEDULE_EVISA", attempt_proxy, min_score=50)
                        captcha_ms = int((_time.time() - t0) * 1000)
                    except Exception as ce:
                        return {"label": label, "proxy": attempt_proxy.split("@")[-1],
                                "error": f"captcha_failed: {ce}"}

                    # POST /slots
                    try:
                        import primp
                        import session as sess
                        c = primp.Client(proxy=attempt_proxy, impersonate="chrome_131",
                                         verify=True, follow_redirects=False)
                        c.set_cookies(session_store.COOKIES_URL, cookies)
                        r = c.post(SLOTS_URL, params={"posto_id": posto_id},
                                   data={"posto_id": posto_id, "captcha": token},
                                   headers={**sess.HEADERS_XHR, "Referer": SCHED_REF},
                                   timeout=30)
                        body = r.text
                        status = r.status_code
                        total_ms = int((_time.time() - t0) * 1000)
                    except Exception as re:
                        return {"label": label, "proxy": attempt_proxy.split("@")[-1],
                                "captcha_ms": captcha_ms,
                                "error": f"request_failed: {re}"}

                    # Classify
                    snip = body[:300]
                    if "body{margin:0;background:#fff}" in snip or "datadome" in snip.lower():
                        verdict = "DATADOME_BLOCK"
                    elif status == 302:
                        verdict = "REDIRECT_302 (session dead)"
                    elif status >= 400:
                        verdict = f"HTTP_{status}"
                    else:
                        # Try JSON parse — browser_form_post wraps in <pre>
                        raw_body = body
                        _pre = _re.search(r"<pre[^>]*>(.*?)</pre>", body, _re.DOTALL)
                        if _pre:
                            raw_body = _pre.group(1).strip()
                        try:
                            j = _json.loads(raw_body)
                            data = j.get("data", "MISSING")
                            if isinstance(data, dict) and data:
                                dates = sorted(data.keys())
                                verdict = f"SLOTS_FOUND: {len(dates)} date(s): {dates[:5]}"
                            elif isinstance(data, dict):
                                verdict = "EMPTY {data:{}}"
                            else:
                                verdict = f"UNEXPECTED_JSON: {str(data)[:80]}"
                        except Exception:
                            verdict = f"NON_JSON (len={len(body)})"

                    return {
                        "label":      label,
                        "proxy":      attempt_proxy.split("@")[-1],
                        "captcha_ms": captcha_ms,
                        "total_ms":   total_ms,
                        "status":     status,
                        "verdict":    verdict,
                        "body_head":  body[:500],
                    }

                for i in range(attempts):
                    # First attempt uses checkpoint's own proxy; subsequent ones rotate SOAX
                    if i == 0 and proxy:
                        attempt_proxy = proxy
                    else:
                        try:
                            from proxy_pool import PersistentProxyPool
                            _soax = _Path(__file__).parent / "data" / "proxies_soax.txt"
                            _pool = PersistentProxyPool(_soax)
                            attempt_proxy = _pool.advance()
                        except Exception:
                            attempt_proxy = proxy or ""
                    if not attempt_proxy:
                        results.append({"label": f"attempt_{i}", "error": "no proxy available"})
                        continue
                    r = await loop.run_in_executor(_ex, lambda p=attempt_proxy, l=f"attempt_{i}": _probe_one(p, l))
                    results.append(r)
                    # Stop early if we found real slots
                    if "SLOTS_FOUND" in r.get("verdict", ""):
                        break

            return web.json_response({
                "username":   username,
                "posto_id":   posto_id,
                "checkpoint": checkpoint,
                "results":    results,
            }, dumps=lambda x: _json.dumps(x, indent=2))

        app = web.Application()
        app.router.add_post("/signal",        handle_signal)
        app.router.add_post("/signal/reset",  handle_reset)
        app.router.add_get("/status",         handle_status)
        app.router.add_get("/probe_slots",    handle_probe_slots)

        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "0.0.0.0", port)
        await site.start()
        log.info(f"[signal] HTTP server on :{port}  POST /signal  GET /status")

        try:
            while True:
                await asyncio.sleep(3600)
        finally:
            await runner.cleanup()
