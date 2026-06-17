"""
Worker -- per-account asyncio coroutine with a complete state machine.

Role "real"  : login -> warmup -> await signal -> apply book -> PDF rename -> done
Role "scout" : login -> warmup -> poll slots forever -> fire signal when found

Key design properties:
- Self-contained: owns its client, proxy, state. Manager is only orchestration.
- Proxy via callback: calls proxy_requester() for each new proxy -- manager rotates the pool.
- Resumable: checks session_store on startup; skips login+warmup if session is alive.
- Multiple signal rounds: after no_slot, waits for signal bus reset then re-enters wait.
- Keepalive: primp GET Schedule.jsp every 4 min; 200=alive, 302=re-warm steps 2-6 (primp), dead=restore.
- Graceful stop: checks _stop_event at every loop boundary; saves session before exit.
- Critical-window login cap: if event_time set and < 2h remaining, caps retries at 5.
"""

import asyncio
import json
import logging
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from typing import Callable

import engine  # noqa: F401 -- triggers __init__.py sys.path setup

log = logging.getLogger("engine.worker")

LOGIN_MAX_ATTEMPTS   = 15
CRITICAL_LOGIN_MAX   = 5
CRITICAL_WINDOW_SECS = 2 * 3600   # < 2 h to event -> use CRITICAL_LOGIN_MAX
KEEPALIVE_INTERVAL   = 4 * 60     # primp GET Schedule.jsp every 4 min keeps both Vistos_sid and form state alive
DOWN_RETRY_SECS      = 5 * 60     # extra sleep when portal is "down"
POLL_INTERVAL        = 300        # seconds between slot polls (scout) -- portal kills session ~90s after /slots POST; 5 min interval keeps re-warm stable

LOGIN_URL = "https://pedidodevistos.mne.gov.pt/VistosOnline/login"

# PDF output dir relative to auto_api/
_AUTO_API_DIR = Path(__file__).parent.parent / "auto_api"
_PDF_DIR      = _AUTO_API_DIR / "data" / "pdfs"


class Worker:
    def __init__(
        self,
        account:          dict,
        role:             str,                           # "real" | "scout"
        signal_bus,                                      # SlotSignalBus
        slot_manager,                                    # SlotManager
        proxy_requester:  Callable[[], str],             # manager.provide_proxy()
        status_cb:        Callable[[str, str, dict], None],  # (worker_id, state, detail)
        executor:         ThreadPoolExecutor,
        solver_keys:      dict,                          # {capsolver:[], anticaptcha:[], ...}
        posto_id:         str,
        nationality:      str   = "CPV",
        residence:        str   = "",
        max_lifetime:     float = 43200.0,
        max_slot_retries: int   = 3,
        event_time:       float | None = None,
        login_sem:        asyncio.Semaphore | None = None,
        apply_sem:        asyncio.Semaphore | None = None,
        proxy_pool        = None,
    ):
        self.account          = account
        self.username         = account["username"]
        self.worker_id        = self.username
        self.role             = role
        self._signal_bus      = signal_bus
        self._slot_manager    = slot_manager
        self._proxy_req       = proxy_requester
        self._status_cb       = status_cb
        self._executor        = executor
        self._solver_keys     = solver_keys
        self.posto_id         = posto_id
        self.nationality      = nationality
        self.residence        = residence
        self.max_lifetime     = max_lifetime
        self.max_slot_retries = max_slot_retries
        self.event_time       = event_time
        self._login_sem       = login_sem or asyncio.Semaphore(LOGIN_MAX_ATTEMPTS)
        self._apply_sem       = apply_sem or asyncio.Semaphore(1)
        self._proxy_pool      = proxy_pool

        # Runtime state
        self.state            = "idle"
        self.client           = None   # primp.Client (extracted from session after warmup)
        self._browser_sess    = None   # PlaywrightSession kept alive for browser_fetch on /slots
        self.proxy            = None   # current SOAX URL
        self.posto_pdf        = ""     # POST target extracted from Schedule.jsp
        self.sched_url        = ""     # Schedule.jsp URL (with posto_id) — keepalive target
        self.nat              = nationality
        self.res              = residence
        self.started_at       = 0.0    # UNIX time of first login (never reset)
        self.last_probe       = 0.0
        self.down_streak      = 0

        # Stop control
        self._stop_event         = asyncio.Event()
        self._stop_requested_at  = 0.0

    # -- Entry point -----------------------------------------------------------

    async def run(self) -> None:
        try:
            resumed = await self._try_resume()
            if not resumed:
                async with self._login_sem:
                    ok = await self._phase_login()
                if not ok:
                    self._report("failed", {"reason": "login_exhausted"})
                    return
                await self._phase_warmup()
                if self.state in ("failed", "expired", "stopped"):
                    return

            keepalive = asyncio.create_task(self._keepalive_coro(), name=f"ka-{self.username}")
            try:
                if self.role == "scout":
                    await self._phase_poll_slots()
                else:
                    await self._phase_await_and_apply()
            finally:
                keepalive.cancel()
                try:
                    await keepalive
                except asyncio.CancelledError:
                    pass
                if self._browser_sess is not None:
                    try:
                        self._browser_sess.close_browser(timeout=10)
                    except Exception:
                        pass
                    self._browser_sess = None

        except asyncio.CancelledError:
            self._save_if_alive()
            self._report("stopped", {"reason": "cancelled"})
            raise
        except Exception as e:
            log.error(f"[{self.username}] unhandled exception", exc_info=True)
            self._report("failed", {"error": str(e)})

    # -- Resume from checkpoint ------------------------------------------------

    async def _try_resume(self) -> bool:
        import session_store
        meta = session_store.load_meta(self.username)
        if not meta or meta.get("checkpoint") != "schedule_jsp":
            return False

        restored = await self._restore_cookies(meta)
        if restored:
            self._log("resumed from checkpoint (session alive)")
            self._report("warmed", {"resumed": True})
            return True
        self._log("checkpoint found but session dead -- proceeding to login")
        return False

    async def _restore_cookies(self, meta: dict | None = None) -> bool:
        """Try up to 5 proxy rotations using saved cookies. Returns True if alive."""
        import session_store
        import primp
        import session as sess

        loop = asyncio.get_event_loop()
        if meta is None:
            meta = session_store.load_meta(self.username)
        cookies = meta.get("cookies") if meta else None
        if not cookies:
            return False

        for attempt in range(5):
            new_proxy = self._proxy_req()
            try:
                client = primp.Client(
                    impersonate=sess.IMPERSONATE,
                    proxy=new_proxy,
                    follow_redirects=True,
                    verify=True,
                )
                client.set_cookies(session_store.COOKIES_URL, cookies)
                alive = await loop.run_in_executor(
                    self._executor, session_store.is_alive, client)
                if alive:
                    self.client    = client
                    self.proxy     = new_proxy
                    self.posto_pdf = meta.get("posto_pdf", "")
                    self.sched_url = (
                        f"https://pedidodevistos.mne.gov.pt/VistosOnline/Schedule.jsp"
                        f"?posto_id={meta.get('posto_id', self.posto_id)}")
                    self.nat       = meta.get("nat", self.nationality)
                    self.res       = meta.get("res", self.residence)
                    self.started_at = time.time()  # reset on resume so max_lifetime applies from now
                    self._log(f"session restored via proxy swap (attempt={attempt+1})")
                    return True
            except Exception as e:
                self._log(f"cookie restore attempt {attempt+1} failed: {e}", "debug")

        return False

    # -- Login -----------------------------------------------------------------

    async def _phase_login(self) -> bool:
        import session as sess
        import json as _json
        import session_store

        loop = asyncio.get_event_loop()
        self._report("logging_in", {})
        limit = self._login_limit()
        cap   = self._solver_keys

        for attempt in range(limit):
            if self._stop_event.is_set():
                return False

            login_proxy = self._proxy_req()
            s = None
            try:
                s = await loop.run_in_executor(self._executor, sess.get_session, login_proxy)

                _cap = cap
                _user = self.username
                _pwd  = self.account["password"]
                result = await loop.run_in_executor(
                    self._executor,
                    lambda: s.browser_login(
                        _user, _pwd,
                        capsolver_keys=_cap.get("capsolver", []),
                        anticaptcha_keys=_cap.get("anticaptcha", []),
                        twocaptcha_keys=_cap.get("twocaptcha", []),
                        capmonster_keys=_cap.get("capmonster", []),
                        skip_checkbox=True,
                        min_score=50,
                        timeout=300,
                    ),
                )
                body  = (result.get("body") or "").strip()
                http_status = result.get("status", 0)
                if not body or body.startswith("<"):
                    desc = f"html len={len(body)}" if body else "empty"
                    raise ValueError(f"login non-JSON response ({desc}) http={http_status}")
                resp  = _json.loads(body)
                rtype = resp.get("type", "")

                if rtype in ("", "200", "success"):
                    self.client = s
                    self.proxy  = login_proxy
                    s = None  # prevent finally from closing the session we're keeping
                    if self.started_at == 0.0:
                        self.started_at = time.time()
                    self._log(f"login OK (attempt={attempt+1}/{limit})")
                    session_store.save(self.username, self.client, self.proxy)
                    if self._proxy_pool is not None:
                        # Fire-and-forget: resolve and cache exit IP for this proxy.
                        # Populates ip_cache so advance_unique() can dedup faster next run.
                        self._executor.submit(self._proxy_pool.cache_ip, login_proxy)
                    self._report("logged_in", {"proxy": login_proxy, "attempt": attempt + 1})
                    return True

                _desc = resp.get("description") or resp.get("message") or ""
                self._log(f"login rejected type={rtype!r} desc={_desc!r} (attempt={attempt+1}/{limit})", "warning")

            except Exception as e:
                self._log(f"login attempt {attempt+1}/{limit} failed: {e}", "warning")

            finally:
                # Always close the browser unless we kept it (s set to None on success).
                if s is not None:
                    try:
                        s.close()
                    except Exception:
                        pass

        self._log(f"login failed after {limit} attempts", "error")
        return False

    # -- Warmup ----------------------------------------------------------------

    async def _phase_warmup(self) -> None:
        from batch_apply import apply_warmup
        import session_store

        self._report("warming_up", {})
        cap    = self._solver_keys
        result = await apply_warmup(
            self.account, self.posto_id,
            cap.get("capsolver",   []),
            cap.get("anticaptcha", []),
            cap.get("twocaptcha",  []),
            cap.get("capmonster",  []),
            self._executor,
            nationality=self.nationality,
            residence=self.residence,
            client=self.client,
            proxy=self.proxy,
        )
        if isinstance(result, dict):
            self.posto_pdf = result["posto_pdf"]
            self.nat       = result["nat"]
            self.res       = result["res"]
            self.sched_url = result.get("sched_url") or (
                f"https://pedidodevistos.mne.gov.pt/VistosOnline/Schedule.jsp?posto_id={self.posto_id}")
            self._log(f"warmup done: posto_pdf={self.posto_pdf}")
            # Keep browser alive for browser_fetch on /slots POST (DataDome passes only browser).
            # Extract primp client for keepalive GETs and session ops.
            if hasattr(self.client, "client"):
                self._browser_sess = self.client
                self.client = self.client.client  # raw primp
            self._log("warmup done — browser kept, primp extracted for keepalive")
            self._report("warmed", {"posto_pdf": self.posto_pdf})
        elif result == "session_dead":
            self._log("warmup: session dead -- running restore", "warning")
            self._close_browser_if_open()
            ok = await self._restore()
            if not ok:
                self._report("failed", {"reason": "warmup_session_dead_unrecoverable"})
        else:
            self._log(f"warmup error: {result}", "error")
            self._close_browser_if_open()
            self._report("failed", {"reason": f"warmup_{result}"})

    # -- Restore (session dead) -------------------------------------------------

    async def _restore(self) -> bool:
        """
        Attempt to recover a dead session.
        1. Cookie-swap (5 proxy rotations, no browser, no CAPTCHA).
        2. Full re-login (LOGIN_MAX_ATTEMPTS or CRITICAL_LOGIN_MAX).
        After recovery: re-run warmup to reach "warmed" state.
        Returns True if worker ends up in "warmed" state.
        """
        import session_store

        self._log("restore: trying cookie swap first")
        restored = await self._restore_cookies()
        if restored:
            self._log("restore: cookie swap succeeded")
            self._report("logged_in", {"restored": "cookie_swap"})
            await self._phase_warmup()
            return self.state == "warmed"

        self._log("restore: cookie swap failed -- full re-login")
        async with self._login_sem:
            ok = await self._phase_login()
        if ok:
            await self._phase_warmup()
            return self.state == "warmed"

        self._log("restore: re-login exhausted", "error")
        return False

    # -- Scout: slot polling loop -----------------------------------------------

    async def _phase_poll_slots(self) -> None:
        from batch_apply import poll_slots_once, SessionExpired

        self._report("polling_slots", {})

        while True:
            if self._stop_event.is_set():
                self._save_if_alive()
                self._report("stopped", {"reason": "stop_requested"})
                return
            if self._past_lifetime():
                self._report("expired", {"reason": "max_lifetime"})
                return

            cap = self._solver_keys
            try:
                slots = await poll_slots_once(
                    self.client, self.proxy, self.posto_id,
                    self.account, self.nat, self.res,
                    cap.get("capsolver",   []),
                    cap.get("anticaptcha", []),
                    cap.get("twocaptcha",  []),
                    cap.get("capmonster",  []),
                    self._executor,
                )
                if slots:
                    self._log(f"slots found: {len(slots)} date(s) -- signalling")
                    self._signal_bus.fire(slots)
            except SessionExpired as _se:
                self._log(f"session expired during polling -- restoring: {_se}", "warning")
                ok = await self._restore()
                if not ok:
                    self._report("failed", {"reason": "session_expired_unrecoverable"})
                    return
                self._report("polling_slots", {})
            except Exception as e:
                self._log(f"poll error: {e}", "warning")

            await asyncio.sleep(POLL_INTERVAL)

    # -- Real: await signal + apply --------------------------------------------

    async def _phase_await_and_apply(self) -> None:
        from batch_apply import apply_book

        self._report("awaiting_signal", {})

        while True:
            if self._stop_event.is_set():
                self._save_if_alive()
                self._report("stopped", {"reason": "stop_requested"})
                return
            if self._past_lifetime():
                self._report("expired", {"reason": "max_lifetime"})
                return

            # Block until signal fires (with per-worker deadline)
            remaining = self._remaining_lifetime()
            try:
                await asyncio.wait_for(self._signal_bus.wait(), timeout=max(1.0, remaining))
            except asyncio.TimeoutError:
                self._report("expired", {"reason": "signal_wait_timeout"})
                return

            # Signal received -- verify form state before booking
            self._report("applying", {})
            cap    = self._solver_keys
            result = "no_slot"

            form_ready = await self._ensure_form_state()
            if not form_ready:
                self._log("form state unrecoverable — skipping signal round", "warning")
                result = "form_unrecoverable"

            if form_ready:
                for attempt in range(self.max_slot_retries):
                    if self._stop_event.is_set():
                        break
                    try:
                        async with self._apply_sem:
                            result = await apply_book(
                                self.account, self.posto_id, self.posto_pdf,
                                self._slot_manager,
                                cap.get("capsolver",   []),
                                cap.get("anticaptcha", []),
                                cap.get("twocaptcha",  []),
                                cap.get("capmonster",  []),
                                self._executor,
                                client=self._browser_sess or self.client,
                                proxy=self.proxy,
                                nat=self.nat,
                                res=self.res,
                            )
                    except Exception as e:
                        self._log(f"apply_book exception (attempt={attempt+1}): {e}", "error")
                        result = "error"

                    if result == "applied":
                        self._rename_pdf()
                        self._report("done", {"result": "applied"})
                        return
                    elif result in ("no_slot", "captcha_failed"):
                        self._log(f"apply {result} (attempt={attempt+1}/{self.max_slot_retries})", "warning")
                        continue
                    elif result == "proxy_blocked":
                        self._log("apply proxy_blocked — proxy POST-dirty, triggering re-login", "warning")
                        break
                    else:  # "error" or unknown
                        self._log(f"apply error: {result}", "error")
                        break

            # All retries exhausted -- tell manager
            self._log(f"apply exhausted ({self.max_slot_retries} retries, last={result})", "warning")
            self._report("no_slot_exhausted", {"result": result})

            # Wait for the signal bus to be reset before re-entering wait()
            # (manager resets it after all real workers report no_slot_exhausted)
            self._log("waiting for signal reset...")
            while self._signal_bus._event.is_set():
                if self._stop_event.is_set():
                    self._save_if_alive()
                    self._report("stopped", {"reason": "stop_requested"})
                    return
                await asyncio.sleep(1.0)

            # If proxy was POST-dirty, re-login with fresh proxy before next signal round
            if result == "proxy_blocked":
                self._log("proxy_blocked: re-logging with next proxy")
                restored = await self._restore()
                if not restored:
                    self._report("failed", {"error": "proxy_blocked and restore failed"})
                    return

            # Signal cleared -- loop back to await next round
            self._report("awaiting_signal", {})

    # -- Keepalive -------------------------------------------------------------

    async def _keepalive_coro(self) -> None:
        """
        Primp GET Schedule.jsp every 4 min.
        - 200  → both Vistos_sid and form state alive (probe refreshes both TTLs)
        - 302  → check if session alive; if yes re-warm steps 2-6 (primp, no CAPTCHA, ~5-10s);
                 if session dead → full restore
        - 5xx/timeout → portal down, defer DOWN_RETRY_SECS
        """
        import session as sess
        import session_store
        from batch_apply import _run_steps_2_to_6

        loop = asyncio.get_event_loop()
        self._log("keepalive started (primp Schedule.jsp mode)")

        while True:
            await asyncio.sleep(KEEPALIVE_INTERVAL)

            if self._past_lifetime():
                self._log("keepalive: max_lifetime reached")
                self._report("expired", {"reason": "keepalive_lifetime"})
                return

            if self.client is None or self.state in (
                "idle", "logging_in", "warming_up", "done", "failed", "expired", "stopped"
            ):
                continue

            sched_url = self.sched_url or (
                f"https://pedidodevistos.mne.gov.pt/VistosOnline/Schedule.jsp?posto_id={self.posto_id}")

            try:
                r = await loop.run_in_executor(self._executor, lambda: self.client.get(
                    sched_url, headers=sess.HEADERS_NAV, timeout=15,
                    follow_redirects=False))
                status_code = r.status_code
            except Exception as e:
                self.down_streak += 1
                self._log(
                    f"keepalive: request failed (streak={self.down_streak}): {e}", "warning")
                await asyncio.sleep(DOWN_RETRY_SECS)
                continue

            self.last_probe = time.time()

            if status_code == 200:
                self.down_streak = 0
                self._log("keepalive: alive (200)")
            elif status_code >= 500:
                self.down_streak += 1
                self._log(
                    f"keepalive: portal down {status_code} (streak={self.down_streak}) "
                    f"-- deferring {DOWN_RETRY_SECS}s", "warning")
                await asyncio.sleep(DOWN_RETRY_SECS)

            else:
                # 302 (or other redirect) — distinguish form-expired vs session-dead
                self.down_streak = 0
                session_alive = await loop.run_in_executor(
                    self._executor, session_store.is_alive, self.client)

                if not session_alive:
                    self._log("keepalive: session dead -- restoring", "warning")
                    ok = await self._restore()
                    if not ok:
                        self._log("keepalive: restore failed -- worker marked failed", "error")
                        self._report("failed", {"reason": "keepalive_restore_failed"})
                        return
                else:
                    # Session alive but form state expired — re-warm via primp (no browser, no CAPTCHA)
                    self._log("keepalive: form state expired (302) — re-warming steps 2-6 (primp)")
                    try:
                        info = await _run_steps_2_to_6(
                            self.client, self.posto_id, self.account,
                            self.nat, self.res, self._executor)
                        if info.get("posto_pdf"):
                            self.posto_pdf = info["posto_pdf"]
                        if info.get("sched_url"):
                            self.sched_url = info["sched_url"]
                        self._log("keepalive: re-warm done — form state restored")
                    except Exception as e:
                        self._log(f"keepalive: re-warm failed: {e} — attempting full restore", "warning")
                        ok = await self._restore()
                        if not ok:
                            self._report("failed", {"reason": "keepalive_rewarm_failed"})
                            return

    # -- PDF rename ------------------------------------------------------------

    def _rename_pdf(self) -> None:
        src = _PDF_DIR / f"{self.username}.pdf"
        if not src.exists():
            self._log("PDF not found after apply -- skipping rename", "warning")
            return
        ts  = datetime.now().strftime("%Y%m%d_%H%M%S")
        dst = _PDF_DIR / f"{self.username}_{self.posto_id}_{ts}.pdf"
        try:
            src.rename(dst)
            self._log(f"PDF saved: {dst.name}")
        except Exception as e:
            self._log(f"PDF rename failed: {e}", "warning")

    # -- Helpers ---------------------------------------------------------------

    async def _ensure_form_state(self) -> bool:
        """
        Verify Schedule.jsp returns 200 immediately before apply_book.
        - 200  → ready, return True
        - 302 + session alive → re-warm steps 2-6 via primp, return True
        - 302 + session dead → full restore, return True/False
        - 5xx / timeout → portal down, return False
        Called from _phase_await_and_apply after signal fires, before booking.
        """
        import session as sess
        import session_store
        from batch_apply import _run_steps_2_to_6

        loop = asyncio.get_event_loop()
        sched_url = self.sched_url or (
            f"https://pedidodevistos.mne.gov.pt/VistosOnline/Schedule.jsp?posto_id={self.posto_id}")

        try:
            r = await loop.run_in_executor(self._executor, lambda: self.client.get(
                sched_url, headers=sess.HEADERS_NAV, timeout=15, follow_redirects=False))
        except Exception as e:
            self._log(f"form state check failed: {e}", "warning")
            return False

        if r.status_code == 200:
            self._log("form state OK (200) — proceeding to apply")
            return True

        if r.status_code >= 500:
            self._log(f"portal down ({r.status_code}) before apply", "warning")
            return False

        # 302 — distinguish form-expired vs session-dead
        session_alive = await loop.run_in_executor(
            self._executor, session_store.is_alive, self.client)

        if not session_alive:
            self._log("session dead before apply — restoring", "warning")
            return await self._restore()

        self._log("form state expired (302) before apply — re-warming steps 2-6")
        try:
            info = await _run_steps_2_to_6(
                self.client, self.posto_id, self.account,
                self.nat, self.res, self._executor)
            if info.get("posto_pdf"):
                self.posto_pdf = info["posto_pdf"]
            if info.get("sched_url"):
                self.sched_url = info["sched_url"]
            self._log("form state restored — proceeding to apply")
            return True
        except Exception as e:
            self._log(f"pre-apply re-warm failed: {e} — attempting full restore", "warning")
            return await self._restore()

    def _close_browser_if_open(self) -> None:
        """Close browser from self.client (pre-warmup) or self._browser_sess (post-warmup)."""
        sess = self._browser_sess or (
            self.client if (self.client is not None and hasattr(self.client, "close_browser"))
            else None)
        if sess is not None:
            primp_client = getattr(sess, "client", None)
            try:
                sess.close_browser(timeout=10)
            except Exception as _ce:
                self._log(f"close_browser error: {_ce}", "debug")
            if sess is self._browser_sess:
                self._browser_sess = None
            elif primp_client is not None:
                self.client = primp_client

    def _report(self, new_state: str, detail: dict | None = None) -> None:
        self.state = new_state
        self._status_cb(self.worker_id, new_state, detail or {})

    def _log(self, msg: str, level: str = "info") -> None:
        getattr(log, level)(f"[{self.username}] {msg}")

    def _login_limit(self) -> int:
        if self.event_time:
            remaining = self.event_time - time.time()
            if 0 < remaining <= CRITICAL_WINDOW_SECS:
                self._log(f"critical window ({remaining/3600:.1f}h left) -- login limit={CRITICAL_LOGIN_MAX}")
                return CRITICAL_LOGIN_MAX
        return LOGIN_MAX_ATTEMPTS

    def _remaining_lifetime(self) -> float:
        if self.started_at:
            return max(1.0, (self.started_at + self.max_lifetime) - time.time())
        return self.max_lifetime

    def _past_lifetime(self) -> bool:
        return bool(self.started_at and (time.time() - self.started_at) > self.max_lifetime)

    def _save_if_alive(self) -> None:
        import session_store
        if not self.client:
            return
        try:
            status = session_store.probe_session(self.client)
            if status == "alive":
                session_store.save(
                    self.username, self.client, self.proxy,
                    checkpoint="schedule_jsp",
                    posto_pdf=self.posto_pdf,
                    nat=self.nat,
                    res=self.res,
                )
                self._log("session saved on stop")
        except Exception as e:
            self._log(f"save_if_alive failed: {e}", "debug")
