"""
Warmup orchestrator: log in N accounts concurrently, keep sessions alive,
detect slots, and fan-out batch_apply when slots appear.

Flow:
  1. Load active/verified accounts
  2. Login concurrently (no-proxy + ProxyLess CAPTCHA for login)
  3. Save cookies to session_store
  4. Start keep-alive loop (probe every 18 min, re-login if dead)
  5. Start SlotDetector
  6. On slot signal: fan-out apply_one() to all alive sessions

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
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

BASE      = "https://pedidodevistos.mne.gov.pt"
LOGIN_URL = BASE + "/VistosOnline/login"
AUTH_URL  = BASE + "/VistosOnline/Authentication.jsp"

_ENV_FILE     = Path(__file__).parent / ".env"
_ACCOUNT_FILE = Path(__file__).parent / "data" / "accounts.csv"

KEEPALIVE_INTERVAL = 18 * 60   # seconds between alive-probes
RE_LOGIN_MAX_RETRIES = 3

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
        self.acct       = acct
        self.username   = acct["username"]
        self.client     = None   # primp Client | None
        self.proxy      = None   # str | None
        self.state      = "idle" # idle|logging_in|logged_in|questionnaire|schedule|booking|done|failed
        self.last_probe = 0.0

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
        """Return healthiest session for slot polling (prefer schedule state)."""
        with self._lock:
            candidates = [w for w in self._workers.values()
                          if w.state in ("schedule", "logged_in") and w.client]
            if not candidates:
                return None
            # Prefer schedule > logged_in, then pick oldest last_probe (least recently polled)
            candidates.sort(key=lambda w: (
                0 if w.state == "schedule" else 1,
                w.last_probe
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

async def _login_worker(ws: WorkerState, login_proxy: str | None,
                        capsolver_keys, anticaptcha_keys,
                        twocaptcha_keys, capmonster_keys,
                        lang: str, executor: ThreadPoolExecutor) -> bool:
    """Login and populate ws.client.  Returns True on success."""
    import session as sess
    import solver as solvermod
    import json as _json

    loop = asyncio.get_event_loop()
    ws.state = "logging_in"
    username = ws.username
    password = ws.acct["password"]

    for attempt in range(RE_LOGIN_MAX_RETRIES):
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
            _log(username, f"login POST error: {e}")
            continue

        if rtype in ("", "200"):
            ws.client = s
            ws.proxy  = login_proxy
            ws.state  = "logged_in"
            _log(username, "login OK")
            return True

        _log(username, f"login failed type={rtype!r} (attempt={attempt+1})")

    ws.state = "failed"
    return False


# ── Apply helper ───────────────────────────────────────────────────────────────

async def _apply_worker(ws: WorkerState, posto_id: str, posto_id_pdf: str,
                        slot_manager, nationality: str,
                        capsolver_keys, anticaptcha_keys,
                        twocaptcha_keys, capmonster_keys,
                        executor: ThreadPoolExecutor) -> str:
    from batch_apply import apply_one
    ws.state = "questionnaire"
    result = await apply_one(
        ws.acct, posto_id, posto_id_pdf, slot_manager,
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
                          login_proxy: str | None,
                          capsolver_keys, anticaptcha_keys,
                          twocaptcha_keys, capmonster_keys,
                          lang: str,
                          executor: ThreadPoolExecutor) -> None:
    import time
    import session_store

    while True:
        await asyncio.sleep(KEEPALIVE_INTERVAL)
        now = asyncio.get_event_loop().time()
        for ws in coordinator.all_workers():
            if ws.state not in ("logged_in", "questionnaire", "schedule"):
                continue
            if ws.client is None:
                continue
            if now - ws.last_probe < KEEPALIVE_INTERVAL * 0.8:
                continue
            alive = await asyncio.get_event_loop().run_in_executor(
                executor, session_store.is_alive, ws.client)
            ws.last_probe = asyncio.get_event_loop().time()
            if not alive:
                log.warning(f"[{ws.username}] session dead — re-logging in")
                ws.state  = "idle"
                ws.client = None
                ok = await _login_worker(
                    ws, login_proxy,
                    capsolver_keys, anticaptcha_keys, twocaptcha_keys, capmonster_keys,
                    lang, executor)
                if ok:
                    session_store.save(ws.username, ws.client, ws.proxy)


# ── Main ───────────────────────────────────────────────────────────────────────

async def main_async(args: argparse.Namespace, env: dict,
                     capsolver_keys, anticaptcha_keys,
                     twocaptcha_keys, capmonster_keys) -> None:
    from account_pool import AccountPool
    import session_store
    from slot_manager import SlotManager
    from slot_detector import SlotDetector

    pool = AccountPool(_ACCOUNT_FILE)
    accounts = [a for a in pool.all() if a["status"] in ("verified", "active")]
    if args.count > 0:
        accounts = accounts[:args.count]
    if not accounts:
        print("[warmup] no verified/active accounts")
        return

    posto_id     = args.posto
    posto_id_pdf = env.get("POSTO_ID_PDF", str(int(posto_id) - 2))
    nationality  = args.nationality

    print(f"\n[warmup] {len(accounts)} accounts  posto={posto_id}  nationality={nationality}  concurrency={args.concurrency}")
    print(f"[warmup] login mode: proxy required (datacenter IP blocked by target)")

    coordinator  = Coordinator()
    slot_manager = SlotManager()
    executor     = ThreadPoolExecutor(max_workers=args.concurrency + 8)
    sem          = asyncio.Semaphore(args.concurrency)

    # Register all workers
    workers = [WorkerState(acct) for acct in accounts]
    for ws in workers:
        coordinator.register(ws)

    # ── Phase 1: Login concurrently ──────────────────────────────────────────
    # proxy is required — datacenter IP is blocked by the target server
    login_proxy = env.get("PROXY") or None

    async def login_bounded(ws):
        async with sem:
            ok = await _login_worker(
                ws, login_proxy,
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

    # ── Phase 2: Start keep-alive ────────────────────────────────────────────
    asyncio.create_task(_keepalive_loop(
        coordinator, login_proxy,
        capsolver_keys, anticaptcha_keys, twocaptcha_keys, capmonster_keys,
        args.lang, executor))

    # ── Phase 3: Start slot detector ─────────────────────────────────────────
    detector = SlotDetector(slot_manager, coordinator)

    if args.trigger_mode == "signal":
        print(f"\n[warmup] Waiting for human signal on :8989/signal  (POST JSON)")
        print(f"  Example: curl -X POST http://localhost:8989/signal"
              f' -H "Content-Type: application/json"'
              f' -d \'{{"posto_id":"{posto_id}","date":"2026-07-01","period_id":"1"}}\'')
        # Start server only (no polling)
        asyncio.create_task(detector._override_server())
    else:
        await detector.start(posto_id)

    # ── Phase 4: Wait for slots ───────────────────────────────────────────────
    print(f"\n[warmup] Waiting for slot signal (trigger-mode={args.trigger_mode})...")
    await detector.wait()
    print(f"\n[warmup] SLOTS DETECTED  pool={slot_manager.stats()}")

    # ── Phase 5: Fan-out apply ────────────────────────────────────────────────
    alive_workers = [ws for ws in workers if ws.state in ("logged_in", "questionnaire", "schedule")]
    print(f"[warmup] Fan-out: applying with {len(alive_workers)} alive sessions concurrently")

    apply_sem = asyncio.Semaphore(args.apply_concurrency or args.concurrency)

    async def apply_bounded(ws):
        async with apply_sem:
            return await _apply_worker(
                ws, posto_id, posto_id_pdf, slot_manager, nationality,
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
    parser.add_argument("--trigger-mode",     choices=("auto", "signal"), default="auto",
                        dest="trigger_mode",
                        help="auto=poll every 30s; signal=wait for POST :8989/signal")
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
