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
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from typing import Callable

import app  # noqa: F401 -- triggers __init__.py sys.path setup (adds app/core to sys.path)

log = logging.getLogger("app.worker")

CRITICAL_WINDOW_SECS = 2 * 3600   # < 2 h to event -> tighten limits
WARMUP_MAX_ATTEMPTS  = 3          # retries for generic (non-session_dead) warmup errors — same session/proxy, no new login cost
KEEPALIVE_INTERVAL   = 4 * 60     # primp GET Schedule.jsp every 4 min keeps both Vistos_sid and form state alive
DOWN_RETRY_SECS      = 5 * 60     # extra sleep when portal is "down"
POLL_INTERVAL        = 120        # seconds between slot polls (scout) -- portal kills session ~90s after /slots POST; 5 min interval keeps re-warm stable

# ── Login failure / retry model ────────────────────────────────────────────────
# Portal policy (confirmed 2026-07-05): ~2 accounts tolerated per IP, and an account
# gets ~2 login attempts per session (1 retry after a refresh) before the portal
# imposes a cooldown. Retrying the same account 10-15x (the old model) just burns
# proxies/CAPTCHA against a wall that was never going to open. The new model retries
# STICKY_RETRY_MAX times on one account (fresh proxy each time), then rotates to the
# next candidate account for the same identity (if any) — "sticky" vs "rotating"
# retry, per identity rather than per account.
STICKY_RETRY_MAX   = 2    # attempts on ONE account before rotating to the next candidate
SOFT_FAIL_CRITICAL = 1    # sticky limit in critical window (< CRITICAL_WINDOW_SECS to event)

# Patterns that identify HARD failures (server-side account rejection, unrecoverable).
# Matched case-insensitively against the full error/rejection string.
_HARD_PATTERNS = (
    "invalid credentials",
    "account suspended",
    "account blocked",
    "account is disabled",
    "conta suspensa",
    "conta bloqueada",
    "senha inválida",
    "utilizador bloqueado",
)

# Patterns that identify SOFT failures (proxy/network/infrastructure, recoverable by retry).
# A proxy connectivity failure should NOT permanently retire the account.
_SOFT_PATTERNS = (
    "net::err",              # catches all Playwright ERR_* network codes
    "err_tunnel",
    "err_connection",
    "err_timed_out",
    "err_name_not_resolved",
    "err_empty_response",
    "err_aborted",
    "unable to retrieve content because the page is navigating",
    "unblock_datadome timeout",
    "no token after all attempts",
    "datadome_challenge_on_post",
    "login_failed_auth_redirect",
    "too many attempts",
    "muitas tentativas",
    "non-json response",     # DataDome HTML challenge page returned instead of JSON
    "captcha",
    "recaptcha",
    # Note: broad "timeout" removed — "err_timed_out" and "etimedout" below cover
    # real connectivity timeouts without matching server-side gateway/SSL errors.
    "connection refused",
    "connection reset",
    "econnrefused",
    "etimedout",
    "socket hang up",
    "socket closed",
    "csrf_missing",          # page.content() race during get_session
)

# Subset of soft patterns that are specifically proxy connectivity faults.
# Only these warrant calling proxy_pool.report_failure() to cool that credential.
_PROXY_FAULT_PATTERNS = (
    "net::err",
    "err_tunnel",
    "err_connection",
    "err_timed_out",
    "err_name_not_resolved",
    "err_empty_response",
    "err_aborted",
    "connection refused",
    "connection reset",
    "econnrefused",
    "etimedout",
    "socket hang up",
    "socket closed",
)


def _classify_login_failure(msg: str) -> str:
    """Classify a login failure string as 'hard', 'soft', or 'unknown'."""
    ml = msg.lower()
    if any(p in ml for p in _HARD_PATTERNS):
        return "hard"
    if any(p in ml for p in _SOFT_PATTERNS):
        return "soft"
    return "unknown"


def _is_proxy_fault(msg: str) -> bool:
    """True when the failure is a proxy/network connectivity issue specifically.
    Used to decide whether to call proxy_pool.report_failure() — CAPTCHA and
    server-side rejections don't warrant cooling the proxy credential."""
    ml = msg.lower()
    return any(p in ml for p in _PROXY_FAULT_PATTERNS)

LOGIN_URL = "https://pedidodevistos.mne.gov.pt/VistosOnline/login"

# PDF output dir relative to app/core/
_CORE_DIR = Path(__file__).parent / "core"
_PDF_DIR  = _CORE_DIR / "data" / "pdfs"


class Worker:
    def __init__(
        self,
        accounts:         list[dict],                    # candidate accounts for one identity (>=1)
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
        # Candidate accounts for this identity, in try-order. self.account is always
        # the currently-active one; _advance_candidate() switches to the next when
        # the current one exhausts its sticky retries (or fails unrecoverably later).
        self._candidates      = accounts
        self._acct_idx        = 0
        self.account          = accounts[0]
        self.username         = self.account["username"]
        self.identity_id      = self.account.get("identity_id") or self.username
        self.worker_id        = self.identity_id
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
        self._login_sem       = login_sem or asyncio.Semaphore(50)
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

        # Sticky-retry counter for the CURRENTLY active candidate account. Reset
        # to 0 by _advance_candidate() whenever we switch to a new account.
        self._sticky_attempts = 0

    # -- Identity: candidate account rotation -----------------------------------

    def _advance_candidate(self) -> bool:
        """Switch to the next untried candidate account for this identity.
        Returns False if no candidates remain (identity exhausted)."""
        self._acct_idx += 1
        if self._acct_idx >= len(self._candidates):
            return False
        self.account = self._candidates[self._acct_idx]
        self.username = self.account["username"]
        self._sticky_attempts = 0
        self._log(f"rotating to alternate account (candidate {self._acct_idx + 1}/{len(self._candidates)})")
        return True


    # -- Entry point -----------------------------------------------------------

    async def run(self) -> None:
        try:
            resumed = await self._try_resume()
            if not resumed:
                async with self._login_sem:
                    ok, reason = await self._phase_login()

                if not ok:
                    if reason == "stopped":
                        self._report("stopped", {"reason": "stop_requested"})
                        return
                    # "exhausted": every candidate account for this identity ran out of
                    # sticky retries (or got hard-rejected). Manager decides per-account
                    # CSV status from hard_failed_usernames vs. the full candidate list.
                    self._report("failed", {
                        "reason": "identity_login_exhausted",
                        "hard_failed_usernames": sorted(self._hard_failed_usernames),
                        "candidate_usernames": [a["username"] for a in self._candidates],
                    })
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

    async def _phase_login(self) -> tuple[bool, str]:
        """
        Sticky/rotating login retry, per identity (see STICKY_RETRY_MAX docstring above).

        Sticky: up to STICKY_RETRY_MAX attempts on the CURRENT candidate account, each
        with a freshly-rotated proxy. An explicit hard (server-side) rejection skips
        straight to rotation — retrying the same account against an explicit "wrong
        credentials"/"suspended" response would never succeed.
        Rotating: once the current account's sticky attempts are exhausted (or it gets
        a hard rejection), advance to the next candidate account for this identity.

        Returns (True, "ok") on success.
        Returns (False, "exhausted") once every candidate account has been tried.
        Returns (False, "stopped") if _stop_event fires mid-loop.

        self._hard_failed_usernames accumulates which specific candidate accounts got
        an explicit server-side rejection (vs. just running out of sticky attempts) —
        read by the caller to decide CSV status per account once the identity retires.
        """
        import session as sess
        import json as _json
        import session_store

        loop = asyncio.get_event_loop()
        self._report("logging_in", {})
        cap = self._solver_keys

        self._hard_failed_usernames: set[str] = set()
        attempt = 0   # monotonic across the whole identity (all candidates), for log messages

        while True:
            # Recompute sticky_limit each iteration so the critical window tightening
            # applies the moment event_time crosses the threshold.
            sticky_limit = SOFT_FAIL_CRITICAL if self._is_critical_window() else STICKY_RETRY_MAX

            if self._stop_event.is_set():
                return False, "stopped"

            if self._sticky_attempts >= sticky_limit:
                self._log(
                    f"{self.username} exhausted sticky retries "
                    f"({self._sticky_attempts}/{sticky_limit})", "warning")
                if not self._advance_candidate():
                    self._log("all candidate accounts exhausted — identity retiring", "error")
                    return False, "exhausted"
                continue  # retry loop with the newly-rotated-in account

            attempt += 1
            self._sticky_attempts += 1
            login_proxy = self._proxy_req()
            s = None

            # Pre-solve CAPTCHA in parallel with browser launch + page navigation.
            # By the time the browser finishes loading and filling the form (~20-40s),
            # the token is usually ready — no serial wait.
            _cap        = cap
            _user       = self.username
            _pwd        = self.account["password"]
            _pre_token: list[str] = [""]   # mutable; pre-solve thread writes here

            def _pre_solve() -> None:
                from app.core import solver as _pre_s
                try:
                    _pre_token[0] = _pre_s.race_all(
                        _cap.get("capsolver", []), _cap.get("anticaptcha", []),
                        _cap.get("twocaptcha", []), _cap.get("capmonster", []),
                        "LOGIN_EVISA", proxy=None, min_score=50,
                    )
                    self._log(f"pre-solve done  len={len(_pre_token[0])}")
                except Exception as _pe:
                    self._log(f"pre-solve failed: {_pe}", "warning")

            threading.Thread(target=_pre_solve, daemon=True,
                             name=f"presol-{self.username}").start()

            try:
                s = await loop.run_in_executor(self._executor, sess.get_session, login_proxy)

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
                        pre_token=_pre_token,
                        timeout=300,
                    ),
                )
                body        = (result.get("body") or "").strip()
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
                    self._log(
                        f"login OK  account={self.username}  "
                        f"(attempt={attempt}  sticky={self._sticky_attempts}/{sticky_limit}  "
                        f"candidate={self._acct_idx + 1}/{len(self._candidates)})")
                    session_store.save(self.username, self.client, self.proxy)
                    if self._proxy_pool is not None:
                        self._executor.submit(self._proxy_pool.cache_ip, login_proxy)
                    self._report("logged_in", {"proxy": login_proxy, "attempt": attempt})
                    return True, "ok"

                # Server responded but rejected — classify from type/description fields
                _desc    = resp.get("description") or resp.get("message") or ""
                fail_str = f"type={rtype} desc={_desc}"
                kind     = _classify_login_failure(fail_str)
                self._log(
                    f"login rejected {fail_str!r}  [{kind}]  account={self.username}  "
                    f"(attempt={attempt} sticky={self._sticky_attempts}/{sticky_limit})",
                    "warning",
                )
                if kind == "hard":
                    self._hard_failed_usernames.add(self.username)
                    self._log(f"hard rejection for {self.username} — rotating immediately", "warning")
                    if not self._advance_candidate():
                        return False, "exhausted"
                # soft/unknown: fall through to top of loop, sticky counter already
                # incremented above — will retry same account or rotate once the
                # sticky limit is reached, on the next iteration.

            except Exception as e:
                err_str = str(e)
                kind    = _classify_login_failure(err_str)
                self._log(
                    f"login attempt {attempt} failed [{kind}]: {err_str}  account={self.username}  "
                    f"(sticky={self._sticky_attempts}/{sticky_limit})",
                    "warning",
                )
                if kind == "hard":
                    self._hard_failed_usernames.add(self.username)
                    self._log(f"hard rejection for {self.username} — rotating immediately", "warning")
                    if not self._advance_candidate():
                        return False, "exhausted"
                elif _is_proxy_fault(err_str):
                    # Route failure to whichever pool actually owns this proxy.
                    # IspFirstRequester exposes report_failure(); plain callables do not.
                    if hasattr(self._proxy_req, "report_failure"):
                        self._proxy_req.report_failure(login_proxy)
                    elif self._proxy_pool is not None:
                        self._proxy_pool.report_failure(login_proxy)

            finally:
                if s is not None:
                    try:
                        s.close()
                    except Exception:
                        pass

    # -- Warmup ----------------------------------------------------------------

    async def _phase_warmup(self) -> None:
        from batch_apply import apply_warmup
        import session_store

        self._report("warming_up", {})
        cap = self._solver_keys

        # Retry generic warmup errors (e.g. csrf_missing from a Formulario response
        # quirk) a couple of times before giving up. We're already logged in at this
        # point — a warmup retry just re-walks Questionario/Formulario/ScheduleController
        # on the SAME session/proxy, no new proxy or CAPTCHA-spending login needed, so
        # it's cheap relative to a full re-login. Previously any non-"session_dead"
        # failure went straight to "failed" with zero retry — a real gap (a logged-in
        # worker that already paid the login cost was discarded over one transient
        # Formulario-response quirk).
        result = None
        for _wi in range(WARMUP_MAX_ATTEMPTS):
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
            if isinstance(result, dict) or result == "session_dead":
                break
            if _wi < WARMUP_MAX_ATTEMPTS - 1:
                self._log(f"warmup attempt {_wi+1}/{WARMUP_MAX_ATTEMPTS} failed: {result} -- retrying", "warning")

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
            # Exhausted WARMUP_MAX_ATTEMPTS on the SAME proxy/session every time (apply_warmup
            # is called with client=self.client, proxy=self.proxy unchanged across retries).
            # A generic failure that survives 3 identical retries is far more likely a blocked/
            # flagged proxy IP than a transient page-load race — the same class of problem the
            # session_dead branch above already recovers from via _restore() (fresh proxy).
            # Give this case the same chance instead of discarding a worker that already paid
            # the full login cost.
            self._log(f"warmup error: {result} -- exhausted {WARMUP_MAX_ATTEMPTS} attempts on same proxy, attempting restore", "warning")
            self._close_browser_if_open()
            ok = await self._restore()
            if not ok:
                self._report("failed", {"reason": f"warmup_{result}_unrecoverable"})

    # -- Restore (session dead) -------------------------------------------------

    async def _restore(self) -> bool:
        """
        Attempt to recover a dead session.
        1. Cookie-swap (5 proxy rotations, no browser, no CAPTCHA).
        2. Full re-login (LOGIN_MAX_ATTEMPTS or CRITICAL_LOGIN_MAX).
        After recovery: skip re-warmup entirely if the checkpoint already gave us a
        posto_pdf (i.e. we were previously warmed to Schedule.jsp) — same shortcut
        _try_resume() takes at worker startup, just reached from mid-run recovery
        instead. _ensure_form_state() re-verifies actual server-side readiness right
        before the next apply attempt regardless, so skipping the redundant re-warm
        here is safe: a stale/wrong posto_pdf gets caught and re-warmed there anyway.
        Returns True if worker ends up in "warmed" state.
        """
        import session_store

        self._log("restore: trying cookie swap first")
        restored = await self._restore_cookies()
        if restored:
            self._log("restore: cookie swap succeeded")
            self._report("logged_in", {"restored": "cookie_swap"})
            if self.posto_pdf:
                self._log(f"restore: checkpoint already warmed (posto_pdf={self.posto_pdf}) -- skipping re-warmup")
                self._report("warmed", {"resumed": "checkpoint_after_restore"})
                return True
            await self._phase_warmup()
            return self.state == "warmed"

        self._log("restore: cookie swap failed -- full re-login")
        async with self._login_sem:
            ok, reason = await self._phase_login()
        if ok:
            await self._phase_warmup()
            return self.state == "warmed"

        self._log(f"restore: re-login exhausted (reason={reason})", "error")
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
                    # Only the fact that something is available matters here — not
                    # the data itself. Each waking real worker fetches its own fresh
                    # /slots (with its own fresh CAPTCHA token) when it applies.
                    self._log(f"slots found: {len(slots)} date(s) -- signalling")
                    self._signal_bus.fire()
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
            # Connection-level failure (dead proxy, tunnel error) — not a portal-side
            # response we can classify by status code. Attempt a full restore (fresh
            # proxy) rather than giving up: a dead proxy here would otherwise poison
            # every subsequent signal round for the rest of the run (self.proxy is
            # only ever rotated via _restore(), and this path previously never called it).
            self._log(f"form state check failed: {e} — attempting restore", "warning")
            return await self._restore()

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

    def _is_critical_window(self) -> bool:
        """True when the booking event is < CRITICAL_WINDOW_SECS away.
        Tightens the soft-failure limit and disables the soft-pause retry loop
        so workers don't sleep through the booking window."""
        if self.event_time:
            remaining = self.event_time - time.time()
            if 0 < remaining <= CRITICAL_WINDOW_SECS:
                return True
        return False

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
