"""
Warmup orchestrator: log in N accounts concurrently, keep sessions alive,
detect slots, and fan-out batch_apply when slots appear.

Flow:
  1. Load active/verified accounts
  2. Login concurrently (SOAX proxy pool, rotating session IDs, up to 25 attempts)
  3. Save cookies to session_store
  4. Phase 2.5: warm all accounts through steps 2-6 (questionnaire→Schedule.jsp)
  5. Start keep-alive loop (probe every 18 min; re-warm or re-login if dead)
  6. Start SlotDetector
  7. On slot signal: fan-out apply_book() to all warmed sessions

Keep-alive distinguishes three probe outcomes:
  'alive'  — session healthy, nothing to do
  'dead'   — Vistos_sid expired; try proxy-swap restore first, then full re-login
  'down'   — portal unreachable (5xx/timeout); defer 5 min, do NOT burn CAPTCHA

12-hour deadline: all loops stop and the process exits cleanly after max_lifetime.

Usage:
  uv run python batch_warmup.py --count 50 --concurrency 20 --posto 5086
  uv run python batch_warmup.py --count 50 --concurrency 20 --posto 5086 --trigger-mode signal
    (signal = wait for human override at :8989/signal instead of auto-polling)
"""

import argparse
import asyncio
import logging
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

BASE      = "https://pedidodevistos.mne.gov.pt"
LOGIN_URL = BASE + "/VistosOnline/login"
AUTH_URL  = BASE + "/VistosOnline/Authentication.jsp"

_ENV_FILE          = Path(__file__).parent / ".env"
_ACCOUNT_FILE      = Path(__file__).parent / "data" / "accounts.csv"
_SOAX_PROXY_FILE   = Path(__file__).parent / "data" / "proxies_soax.txt"

KEEPALIVE_INTERVAL   = 18 * 60    # seconds between alive-probes
LOGIN_MAX_ATTEMPTS   = 25         # max proxy rotations per login attempt
MAX_SESSION_LIFETIME = 12 * 3600  # default 12-hour deadline (overridable)
DOWN_RETRY_INTERVAL  = 5 * 60     # seconds to defer re-probe when portal is "down"

_print_lock = threading.Lock()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-5s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("warmup")


def _log(acct: str, msg: str) -> None:
    with _print_lock:
        print(f"[{acct}] {msg}", flush=True)


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _load_dotenv() -> dict:
    env: dict[str, str] = {}
    if not _ENV_FILE.exists():
        return env
    for line in _ENV_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        env[k.strip()] = v.strip()
    return env


# ── Worker state ───────────────────────────────────────────────────────────────

class WorkerState:
    def __init__(self, acct: dict):
        self.acct        = acct
        self.username    = acct["username"]
        self.client      = None   # primp Client | None
        self.proxy       = None   # str | None
        self.state       = "idle" # idle|logging_in|logged_in|questionnaire|schedule|booking|done|failed|expired
        self.last_probe  = 0.0
        self.warmup_done = False  # True after steps 2-6 complete
        self.posto_pdf   = ""
        self.nat         = ""
        self.res         = ""
        self.started_at  = 0.0   # time.time() on FIRST successful login (never reset)
        self.down_streak = 0     # consecutive "down" probe count

    def __repr__(self):
        return f"WorkerState({self.username} state={self.state})"


# ── Coordinator ────────────────────────────────────────────────────────────────

class Coordinator:
    def __init__(self):
        self._lock    = threading.Lock()
        self._workers: dict[str, WorkerState] = {}

    def register(self, ws: WorkerState) -> None:
        with self._lock:
            self._workers[ws.username] = ws

    def get(self, username: str) -> WorkerState | None:
        return self._workers.get(username)

    def all_workers(self) -> list[WorkerState]:
        with self._lock:
            return list(self._workers.values())

    def alive_count(self) -> int:
        with self._lock:
            return sum(1 for w in self._workers.values()
                       if w.state in ("logged_in", "questionnaire", "schedule"))

    def pick_detector_session(self) -> WorkerState | None:
        """Return healthiest session for slot polling (prefer warmed schedule sessions)."""
        with self._lock:
            candidates = [w for w in self._workers.values()
                          if w.state in ("schedule", "logged_in") and w.client and w.warmup_done]
            if not candidates:
                candidates = [w for w in self._workers.values()
                              if w.state in ("schedule", "logged_in") and w.client]
            if not candidates:
                return None
            candidates.sort(key=lambda w: (
                0 if w.warmup_done else 1,
                0 if w.state == "schedule" else 1,
                w.last_probe,
            ))
            return candidates[0]

    def mark_detector_failed(self, username: str) -> None:
        with self._lock:
            w = self._workers.get(username)
            if w:
                w.state = "failed"

    def warmup_stats(self) -> dict:
        with self._lock:
            counts: dict[str, int] = {}
            for w in self._workers.values():
                counts[w.state] = counts.get(w.state, 0) + 1
            return counts


# ── Login helper ───────────────────────────────────────────────────────────────

async def _login_worker(ws: WorkerState, proxy_pool,
                        capsolver_keys, anticaptcha_keys,
                        twocaptcha_keys, capmonster_keys,
                        lang: str, executor: ThreadPoolExecutor) -> bool:
    """
    Login with SOAX proxy rotation — up to LOGIN_MAX_ATTEMPTS different session IDs.
    Returns True on success and populates ws.client, ws.proxy.
    Sets ws.started_at on the FIRST successful login only.
    """
    import session as sess
    import solver as solvermod
    import json as _json

    loop = asyncio.get_event_loop()
    ws.state = "logging_in"
    username = ws.username
    password = ws.acct["password"]

    for attempt in range(LOGIN_MAX_ATTEMPTS):
        login_proxy = proxy_pool.advance()

        try:
            s = await loop.run_in_executor(executor, sess.get_session, login_proxy)
        except Exception as e:
            _log(username, f"get_session failed (attempt={attempt+1}): {e}")
            continue

        try:
            token = await loop.run_in_executor(
                executor, solvermod.race_all,
                capsolver_keys, anticaptcha_keys, twocaptcha_keys, capmonster_keys,
                "LOGIN_EVISA", login_proxy,
            )
        except Exception as e:
            _log(username, f"CAPTCHA failed (attempt={attempt+1}): {e}")
            continue

        try:
            payload = {"username": username, "password": password,
                       "language": lang, "rgpd": "Y", "captchaResponse": token}
            lr = await loop.run_in_executor(executor, lambda _s=s, _p=payload:
                _s.post(LOGIN_URL, data=_p, headers=sess.HEADERS_XHR, timeout=20))
            body = lr.text.strip()
            resp = _json.loads(body)
            rtype = resp.get("type", "")
        except Exception as e:
            _log(username, f"login POST error (attempt={attempt+1}): {e}")
            continue

        if rtype in ("", "200", "success"):
            ws.client = s
            ws.proxy  = login_proxy
            ws.state  = "logged_in"
            if ws.started_at == 0.0:
                ws.started_at = time.time()
            _log(username, f"login OK (attempt={attempt+1})")
            return True

        _log(username, f"login failed type={rtype!r} (attempt={attempt+1})")

    ws.state = "failed"
    _log(username, f"login failed after {LOGIN_MAX_ATTEMPTS} attempts")
    return False


# ── Session restore with proxy rotation ────────────────────────────────────────

async def _try_restore_with_new_proxy(ws: WorkerState, proxy_pool,
                                       executor: ThreadPoolExecutor) -> bool:
    """
    Attempt to restore a dead session by reusing saved cookies on a fresh SOAX
    session ID (avoids a full browser re-login when the proxy was the issue).
    Returns True if the restored session is alive.
    """
    import session_store
    import session as sess

    loop = asyncio.get_event_loop()
    meta = session_store.load_meta(ws.username)
    cookies = meta.get("cookies")
    if not cookies:
        return False

    for attempt in range(5):
        new_proxy = proxy_pool.advance()
        try:
            import primp
            client = primp.Client(
                impersonate=sess.IMPERSONATE,
                proxy=new_proxy,
                follow_redirects=True,
                verify=True,
            )
            client.set_cookies(session_store.COOKIES_URL, cookies)
            alive = await loop.run_in_executor(executor, session_store.is_alive, client)
            if alive:
                ws.client = client
                ws.proxy  = new_proxy
                _log(ws.username, f"session restored with new proxy (attempt={attempt+1})")
                return True
        except Exception as e:
            _log(ws.username, f"restore attempt {attempt+1} failed: {e}")

    return False


# ── Re-warm helper ──────────────────────────────────────────────────────────────

async def _rewarm(ws: WorkerState, posto_id: str,
                  capsolver_keys, anticaptcha_keys,
                  twocaptcha_keys, capmonster_keys,
                  executor: ThreadPoolExecutor,
                  nationality: str = "CPV", residence: str = "") -> None:
    """Re-run steps 2-6 on an alive session after re-login or proxy restore."""
    import session_store
    from batch_apply import apply_warmup

    result = await apply_warmup(
        ws.acct, posto_id,
        capsolver_keys, anticaptcha_keys, twocaptcha_keys, capmonster_keys,
        executor, nationality=nationality, residence=residence,
        client=ws.client, proxy=ws.proxy,
    )
    if isinstance(result, dict):
        ws.posto_pdf   = result["posto_pdf"]
        ws.nat         = result["nat"]
        ws.res         = result["res"]
        ws.warmup_done = True
        ws.state       = "schedule"
        session_store.save(ws.username, ws.client, ws.proxy)
        log.info(f"[{ws.username}] re-warmed  posto_pdf={ws.posto_pdf}")
    else:
        ws.warmup_done = False
        ws.state       = "failed"
        log.warning(f"[{ws.username}] re-warm failed: {result}")


# ── Apply helper ───────────────────────────────────────────────────────────────

async def _apply_worker(ws: WorkerState, posto_id: str,
                        slot_manager, nationality: str,
                        capsolver_keys, anticaptcha_keys,
                        twocaptcha_keys, capmonster_keys,
                        executor: ThreadPoolExecutor) -> str:
    from batch_apply import apply_one
    ws.state = "questionnaire"
    result = await apply_one(
        ws.acct, posto_id, slot_manager,
        capsolver_keys, anticaptcha_keys, twocaptcha_keys, capmonster_keys,
        executor,
        nationality=nationality,
        client=ws.client,
        proxy=ws.proxy,
    )
    ws.state = "done" if result == "applied" else "failed"
    return result


# ── Keep-alive loop ────────────────────────────────────────────────────────────

async def _keepalive_loop(coordinator: Coordinator,
                          proxy_pool,
                          capsolver_keys, anticaptcha_keys,
                          twocaptcha_keys, capmonster_keys,
                          lang: str,
                          executor: ThreadPoolExecutor,
                          posto_id: str = "",
                          nationality: str = "CPV",
                          residence: str = "",
                          max_lifetime: int = MAX_SESSION_LIFETIME,
                          deadline: float = 0.0) -> None:
    import session_store
    from session_store import probe_session

    loop = asyncio.get_event_loop()

    while True:
        # Stop keepalive when the global deadline has passed
        if deadline and time.time() > deadline:
            log.info("[keepalive] deadline reached — stopping keepalive loop")
            return

        await asyncio.sleep(KEEPALIVE_INTERVAL)
        now = loop.time()

        for ws in coordinator.all_workers():
            if ws.state not in ("logged_in", "questionnaire", "schedule"):
                continue
            if ws.client is None:
                continue
            if now - ws.last_probe < KEEPALIVE_INTERVAL * 0.8:
                continue

            status = await loop.run_in_executor(executor, probe_session, ws.client)
            ws.last_probe = now

            if status == "alive":
                ws.down_streak = 0
                continue

            elif status == "down":
                ws.down_streak += 1
                log.warning(f"[{ws.username}] portal unreachable (streak={ws.down_streak}) — deferring re-login")
                # Defer next probe to DOWN_RETRY_INTERVAL from now
                ws.last_probe = now - KEEPALIVE_INTERVAL + DOWN_RETRY_INTERVAL
                continue

            # status == "dead"
            ws.down_streak = 0

            # Enforce 12-hour deadline per worker
            if ws.started_at and (time.time() - ws.started_at) > max_lifetime:
                ws.state = "expired"
                log.info(f"[{ws.username}] past {max_lifetime//3600}h lifetime — marked expired")
                continue

            log.warning(f"[{ws.username}] session dead — attempting restore then re-login")
            ws.state       = "idle"
            ws.warmup_done = False
            ws.client      = None

            # 1. Try restore with rotated proxy (cheap: no browser, no CAPTCHA)
            restored = await _try_restore_with_new_proxy(ws, proxy_pool, executor)
            if restored:
                ws.state = "logged_in"
                session_store.save(ws.username, ws.client, ws.proxy)
                if posto_id:
                    await _rewarm(ws, posto_id,
                                  capsolver_keys, anticaptcha_keys, twocaptcha_keys, capmonster_keys,
                                  executor, nationality=nationality, residence=residence)
                continue

            # 2. Full re-login with proxy rotation
            ok = await _login_worker(
                ws, proxy_pool,
                capsolver_keys, anticaptcha_keys, twocaptcha_keys, capmonster_keys,
                lang, executor)
            if ok:
                session_store.save(ws.username, ws.client, ws.proxy)
                if posto_id:
                    await _rewarm(ws, posto_id,
                                  capsolver_keys, anticaptcha_keys, twocaptcha_keys, capmonster_keys,
                                  executor, nationality=nationality, residence=residence)


# ── Main ───────────────────────────────────────────────────────────────────────

async def main_async(args: argparse.Namespace, env: dict,
                     capsolver_keys, anticaptcha_keys,
                     twocaptcha_keys, capmonster_keys) -> None:
    from account_pool import AccountPool
    import session_store
    from slot_manager import SlotManager
    from slot_detector import SlotDetector
    from batch_apply import apply_warmup
    from proxy_pool import PersistentProxyPool

    pool = AccountPool(_ACCOUNT_FILE)
    accounts = [a for a in pool.all() if a["status"] in ("verified", "active")]
    if args.count > 0:
        accounts = accounts[:args.count]
    if not accounts:
        print("[warmup] no verified/active accounts")
        return

    posto_id    = args.posto
    nationality = args.nationality
    residence   = getattr(args, "residence", "") or nationality
    max_lifetime = getattr(args, "max_lifetime", MAX_SESSION_LIFETIME)

    print(f"\n[warmup] {len(accounts)} accounts  posto={posto_id}"
          f"  nationality={nationality}  residence={residence}  concurrency={args.concurrency}"
          f"  max_lifetime={max_lifetime//3600}h")

    # SOAX proxy pool (rotates session IDs)
    proxy_pool   = PersistentProxyPool(_SOAX_PROXY_FILE)
    coordinator  = Coordinator()
    slot_manager = SlotManager()
    executor     = ThreadPoolExecutor(max_workers=args.concurrency + 8)
    sem          = asyncio.Semaphore(args.concurrency)

    workers = [WorkerState(acct) for acct in accounts]
    for ws in workers:
        coordinator.register(ws)

    # ── Phase 1: Login concurrently ──────────────────────────────────────────
    async def login_bounded(ws):
        async with sem:
            ok = await _login_worker(
                ws, proxy_pool,
                capsolver_keys, anticaptcha_keys, twocaptcha_keys, capmonster_keys,
                args.lang, executor)
            if ok:
                session_store.save(ws.username, ws.client, ws.proxy)
            return ok

    print(f"\n[warmup] Phase 1: logging in {len(workers)} accounts...")
    login_results = await asyncio.gather(*[login_bounded(ws) for ws in workers],
                                         return_exceptions=True)
    ok_count = sum(1 for r in login_results if r is True)
    print(f"[warmup] login done: {ok_count}/{len(workers)} succeeded")

    if ok_count == 0:
        print("[warmup] all logins failed — check credentials / CAPTCHA / connectivity")
        return

    # ── Phase 2.5: Warmup (steps 2-6) for all logged-in workers ─────────────
    logged_in_workers = [ws for ws in workers if ws.state == "logged_in"]

    async def warmup_bounded(ws):
        async with sem:
            result = await apply_warmup(
                ws.acct, posto_id,
                capsolver_keys, anticaptcha_keys, twocaptcha_keys, capmonster_keys,
                executor, nationality=nationality, residence=residence,
                client=ws.client, proxy=ws.proxy,
            )
            if isinstance(result, dict):
                ws.posto_pdf   = result["posto_pdf"]
                ws.nat         = result["nat"]
                ws.res         = result["res"]
                ws.warmup_done = True
                ws.state       = "schedule"
                _log(ws.username, f"warmup done: posto_pdf={ws.posto_pdf}")
            else:
                ws.state = "failed"
                _log(ws.username, f"warmup failed: {result}")

    print(f"\n[warmup] Phase 2.5: warming up {len(logged_in_workers)} accounts...")
    await asyncio.gather(*[warmup_bounded(ws) for ws in logged_in_workers],
                         return_exceptions=True)
    warm_count = sum(1 for ws in workers if ws.warmup_done)
    print(f"[warmup] warmup done: {warm_count}/{ok_count} accounts at Schedule.jsp")

    if warm_count == 0:
        print("[warmup] no accounts warmed — cannot poll slots")
        return

    # ── Phase 2: Start keep-alive ────────────────────────────────────────────
    deadline = time.time() + max_lifetime
    asyncio.create_task(_keepalive_loop(
        coordinator, proxy_pool,
        capsolver_keys, anticaptcha_keys, twocaptcha_keys, capmonster_keys,
        args.lang, executor,
        posto_id=posto_id, nationality=nationality, residence=residence,
        max_lifetime=max_lifetime, deadline=deadline))

    # ── Phase 3: Start slot detector ─────────────────────────────────────────
    detector = SlotDetector(
        slot_manager, coordinator,
        capsolver_keys=capsolver_keys,
        anticaptcha_keys=anticaptcha_keys,
        twocaptcha_keys=twocaptcha_keys,
        capmonster_keys=capmonster_keys,
        executor=executor,
    )

    if args.trigger_mode == "signal":
        print(f"\n[warmup] Waiting for human signal on :8989/signal  (POST JSON)")
        print(f"  Example: curl -X POST http://localhost:8989/signal"
              f' -H "Content-Type: application/json"'
              f' -d \'{{"posto_id":"{posto_id}","date":"2026-07-01","period_id":"1"}}\'')
        asyncio.create_task(detector._override_server())
    else:
        await detector.start(posto_id)

    # ── Phase 4: Wait for slots (with deadline) ───────────────────────────────
    remaining = max(0.0, deadline - time.time())
    print(f"\n[warmup] Waiting for slot signal (trigger-mode={args.trigger_mode}  "
          f"deadline={max_lifetime//3600}h)...")
    try:
        await asyncio.wait_for(detector.wait(), timeout=remaining)
    except asyncio.TimeoutError:
        print(f"\n[warmup] {max_lifetime//3600}h deadline reached — no slots found, exiting")
        detector.stop()
        return

    print(f"\n[warmup] SLOTS DETECTED  pool={slot_manager.stats()}")

    # ── Phase 5: Fan-out apply ────────────────────────────────────────────────
    alive_workers = [ws for ws in workers if ws.state in ("logged_in", "questionnaire", "schedule")]
    print(f"[warmup] Fan-out: applying with {len(alive_workers)} alive sessions concurrently")

    apply_sem = asyncio.Semaphore(args.apply_concurrency or args.concurrency)

    async def apply_bounded(ws):
        async with apply_sem:
            return await _apply_worker(
                ws, posto_id, slot_manager, nationality,
                capsolver_keys, anticaptcha_keys, twocaptcha_keys, capmonster_keys,
                executor)

    results = await asyncio.gather(*[apply_bounded(ws) for ws in alive_workers],
                                   return_exceptions=True)

    counts: dict[str, int] = {}
    for r in results:
        k = str(r) if isinstance(r, Exception) else r
        counts[k] = counts.get(k, 0) + 1

    print("\n" + "="*50)
    print("[warmup] APPLY SUMMARY")
    for k, v in sorted(counts.items()):
        print(f"  {k:<16}: {v}")
    print(f"  slot pool:     {slot_manager.stats()}")
    print("="*50)


def main() -> None:
    env = _load_dotenv()

    parser = argparse.ArgumentParser(description="Warmup orchestrator")
    parser.add_argument("--count",            type=int, default=0,
                        help="Max accounts (0=all verified/active)")
    parser.add_argument("--concurrency",      type=int, default=20,
                        help="Max concurrent login workers")
    parser.add_argument("--apply-concurrency", type=int, default=0, dest="apply_concurrency",
                        help="Max concurrent apply workers (default: same as --concurrency)")
    parser.add_argument("--posto",            default=env.get("POSTO_ID", "5086"),
                        help="Consular post ID")
    parser.add_argument("--lang",             default=env.get("SITE_LANGUAGE", "PT"))
    parser.add_argument("--nationality",      default=env.get("NATIONALITY", "CPV"),
                        help="ISO 3166-1 alpha-3 nationality (default: NATIONALITY from .env)")
    parser.add_argument("--residence",        default=env.get("RESIDENCE", ""),
                        help="Country of residence ISO code (default: same as --nationality)")
    parser.add_argument("--scout-file",       default="", dest="scout_file",
                        help="CSV of fake accounts to use as scouts (optional)")
    parser.add_argument("--scout-count",      type=int, default=30, dest="scout_count",
                        help="Max scout accounts from --scout-file (default: 30)")
    parser.add_argument("--trigger-mode",     choices=("auto", "signal"), default="auto",
                        dest="trigger_mode",
                        help="auto=poll every 30s; signal=wait for POST :8989/signal")
    parser.add_argument("--max-lifetime",     type=int, default=MAX_SESSION_LIFETIME,
                        dest="max_lifetime",
                        help="Max session lifetime in seconds (default: 43200 = 12h)")
    args = parser.parse_args()

    def _keys(k: str) -> list[str]:
        return [x.strip() for x in env.get(k, "").split(",") if x.strip()]

    capsolver_keys   = _keys("CAPSOLVER_KEYS")
    anticaptcha_keys = _keys("ANTICAPTCHA_KEYS")
    twocaptcha_keys  = _keys("TWOCAPTCHA_KEYS")
    capmonster_keys  = _keys("CAPMONSTER_KEYS")

    if not any([capsolver_keys, anticaptcha_keys, twocaptcha_keys, capmonster_keys]):
        print("ERROR: no CAPTCHA solver keys in .env")
        sys.exit(1)

    asyncio.run(main_async(args, env, capsolver_keys, anticaptcha_keys, twocaptcha_keys, capmonster_keys))


if __name__ == "__main__":
    main()
