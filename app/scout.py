"""
Standalone slot-detection (scout) service.

Runs scout accounts continuously, independent of any specific booking event's
lifecycle — intended for "run all day / all week" slot watching. Reuses the
exact same proven Worker(role="scout") state machine as app.cli's booking-event
workers (login -> warmup -> poll /slots forever, with keepalive + session
restore) — zero changes to that logic.

The scout's job is binary detection only: is anything available right now?
Not what, not which dates/periods. The notification (DB event_log row + optional
HTTP POST to --notify-url) carries no slot data — it's a pure wake-up trigger.
A real worker that wakes on this signal fetches its own fresh /slots data (with
its own fresh CAPTCHA token) inside apply_book(); that's the only way to get a
non-stale, CAPTCHA-gated answer anyway, so the scout relaying its own slot list
would just be a second, redundant, potentially-stale data path.

CLI:
  python -m app.scout --mode test --count 5 --posto 3059 \\
      --nationality CPV --residence FRA --proxy-file proxies_webshare.txt \\
      --notify-url http://localhost:8989/signal

  python -m app.scout --mode real --count 10 --posto 5084 \\
      --max-lifetime 604800   # 7 days
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import app  # noqa: F401 -- sys.path setup (adds app/core to sys.path)

from app.core.signals import SlotSignalBus
from app.worker import Worker

log = logging.getLogger("app.scout")

_ROOT     = Path(__file__).parent.parent
_CORE_DIR = _ROOT / "app" / "core"
_TEST_ACCT_FILE = _CORE_DIR / "data" / "test_accounts.csv"
_REAL_ACCT_FILE = _CORE_DIR / "data" / "accounts.csv"
_SOAX_PROXY_FILE = _CORE_DIR / "data" / "proxies_soax.txt"
_ISP_PROXY_FILE  = _CORE_DIR / "data" / "proxies_isp.txt"
_LOG_DIR  = _CORE_DIR / "data" / "logs"

MONITOR_INTERVAL = 30
DEFAULT_MAX_LIFETIME = 7 * 24 * 3600  # 7 days — scouts are meant to run unattended for a long time


class NotifyingSignalBus(SlotSignalBus):
    """Same SlotSignalBus contract Worker(role='scout') already calls .fire() on,
    plus: persist an event_log row and optionally POST to an external --notify-url
    so a separately-running booking process can be woken cross-process.

    Deliberately carries NO slot data anywhere in this path. The scout's job is
    binary detection only ("is anything available right now?") — not reporting
    which dates/periods. Each real worker fetches its own fresh /slots data (with
    its own fresh CAPTCHA token) inside apply_book() when it wakes; that's the only
    way to get a non-stale, CAPTCHA-gated answer anyway, so relaying the scout's
    own (possibly already-stale-by-the-time-it-arrives) slot list would just be a
    second, redundant data path."""

    def __init__(self, slot_manager, notify_url: str | None = None):
        super().__init__(slot_manager)
        self._notify_url = notify_url

    def fire(self) -> None:
        is_new_finding = not self._event.is_set()
        super().fire()
        if is_new_finding:
            threading.Thread(target=self._notify, daemon=True).start()

    def _notify(self) -> None:
        # DB log (best-effort — Phase A's DB exists but isn't the live path yet).
        # Records THAT a detection happened, not what was found.
        try:
            from app.db.session import get_session
            from app.db.models import EventLog
            s = get_session()
            try:
                s.add(EventLog(
                    level="info", state="slots_found",
                    message="scout detected slot availability",
                ))
                s.commit()
            finally:
                s.close()
        except Exception as e:
            print(f"[scout] db log failed (non-fatal): {e}")

        if self._notify_url:
            import urllib.request as _urllib
            import time as _time
            _notified = False
            for _attempt in range(3):
                try:
                    req = _urllib.Request(self._notify_url, data=b"", method="POST")
                    with _urllib.urlopen(req, timeout=10):
                        pass
                    print(f"[scout] notified {self._notify_url}")
                    _notified = True
                    break
                except Exception as e:
                    print(f"[scout] notify attempt {_attempt+1}/3 failed: {e}")
                    if _attempt < 2:
                        _time.sleep(2)
            if not _notified:
                print(f"[scout] notify failed after 3 attempts — slot signal may be lost")


def _load_dotenv(env_file: Path) -> dict[str, str]:
    env: dict[str, str] = {}
    if not env_file.exists():
        return env
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        k, v = k.strip(), v.strip()
        env[k] = v
        # Also push into os.environ (a real env var, if already set, wins) so
        # modules that check os.environ.get(...) directly — e.g. batch_apply.py's
        # EVIDENCE_SCREENSHOTS flag — actually see values set only in .env.
        os.environ.setdefault(k, v)
    return env


def _load_accounts(csv_file: Path, statuses=("verified", "active")) -> list[list[dict]]:
    """Returns identities (candidate-account lists), grouped by identity_id — see
    app.cli's _load_accounts docstring for the same grouping used by Worker."""
    from account_pool import AccountPool
    pool = AccountPool(csv_file)
    candidates = [a for a in pool.all() if a.get("status") in statuses]
    identities: dict[str, list[dict]] = {}
    for a in candidates:
        iid = a.get("identity_id") or a["username"]
        identities.setdefault(iid, []).append(a)
    return list(identities.values())


async def _status_printer(workers: list[Worker], states: dict[str, str],
                           tasks: list[asyncio.Task], signal_task: asyncio.Task) -> None:
    _terminal = {"done", "failed", "expired", "stopped", "blocked", "crashed"}
    while True:
        await asyncio.sleep(MONITOR_INTERVAL)
        ts = time.strftime("%H:%M:%S")
        print(f"\n{'━'*60}\n {ts}  ({len(workers)} scouts)", flush=True)
        for w in workers:
            print(f" {w.worker_id:24s} {states.get(w.worker_id, w.state):20s} {w.proxy or ''}", flush=True)
        print("━" * 60, flush=True)

        # Exit once every scout has reached a terminal state — previously this loop
        # ran forever even after all scouts had failed (e.g. every account lost its
        # session and then exhausted re-login), printing the same dead status table
        # indefinitely with no recovery and no way to know the run was actually over
        # without manually killing the process. A scout run with nothing left to do
        # should end, not idle.
        all_terminal = all(
            states.get(w.worker_id) in _terminal or tasks[i].done()
            for i, w in enumerate(workers)
        )
        if all_terminal:
            log.info("[scout] all scouts terminal — exiting")
            signal_task.cancel()
            return


async def _run(args: argparse.Namespace, env: dict) -> None:
    from proxy_pool import PersistentProxyPool
    from slot_manager import SlotManager

    _LOG_DIR.mkdir(parents=True, exist_ok=True)

    def _keys(k: str) -> list[str]:
        return [x.strip() for x in env.get(k, "").split(",") if x.strip()]

    solver_keys = {
        "capsolver":   _keys("CAPSOLVER_KEYS"),
        "anticaptcha": _keys("ANTICAPTCHA_KEYS"),
        "twocaptcha":  _keys("TWOCAPTCHA_KEYS"),
        "capmonster":  _keys("CAPMONSTER_KEYS"),
    }
    if not any(solver_keys.values()):
        log.error("No CAPTCHA solver keys in .env — aborting")
        sys.exit(1)

    proxy_file = _CORE_DIR / "data" / args.proxy_file if args.proxy_file else _SOAX_PROXY_FILE
    log.info(f"[scout] proxy pool file: {proxy_file.name}" + (f"  offset={args.proxy_offset}" if args.proxy_offset else ""))
    proxy_pool = PersistentProxyPool(proxy_file, proxy_offset=args.proxy_offset)

    isp_pool = None
    IspFirstRequester = None
    if _ISP_PROXY_FILE.exists() and not args.no_isp:
        from app.core.isp_proxy_pool import IspProxyPool, IspFirstRequester
        isp_pool = IspProxyPool(str(_ISP_PROXY_FILE))
        log.info(f"[scout] ISP pool loaded: {isp_pool.size_available}/{isp_pool.size_total}")

    slot_manager = SlotManager()
    signal_bus = NotifyingSignalBus(slot_manager, notify_url=args.notify_url)

    acct_file = _TEST_ACCT_FILE if args.mode == "test" else _REAL_ACCT_FILE
    accounts = _load_accounts(acct_file)
    if args.account_offset:
        accounts = accounts[args.account_offset:]
    if args.count > 0:
        accounts = accounts[:args.count]
    if not accounts:
        log.error(f"No verified/active accounts in {acct_file}")
        sys.exit(1)

    nationality = args.nationality or env.get("TEST_NATIONALITY" if args.mode == "test" else "NATIONALITY", "CPV")
    residence   = args.residence   or env.get("TEST_RESIDENCE"   if args.mode == "test" else "RESIDENCE",   nationality)
    posto_id    = args.posto or env.get("TEST_POSTO_ID" if args.mode == "test" else "POSTO_ID", "5086")

    log.info(f"[scout] {len(accounts)} scout(s)  posto={posto_id}  nationality={nationality}  residence={residence}  notify_url={args.notify_url or '(none, DB-log only)'}")

    executor = ThreadPoolExecutor(max_workers=args.executor_threads, thread_name_prefix="scout")
    event_ips: set[str] = set()
    event_ip_lock = threading.Lock()

    def _soax_advance() -> str:
        return proxy_pool.advance_unique(event_ips, event_ip_lock)

    states: dict[str, str] = {}

    def _status_cb(worker_id: str, new_state: str, detail: dict) -> None:
        states[worker_id] = new_state

    workers: list[Worker] = []
    for candidates in accounts:
        req = IspFirstRequester(isp_pool, _soax_advance, soax_pool=proxy_pool) if isp_pool is not None else _soax_advance
        w = Worker(
            accounts=candidates, role="scout",
            signal_bus=signal_bus, slot_manager=slot_manager,
            proxy_requester=req, status_cb=_status_cb,
            executor=executor, solver_keys=solver_keys,
            posto_id=posto_id, nationality=nationality, residence=residence,
            max_lifetime=args.max_lifetime,
            login_sem=asyncio.Semaphore(args.login_concurrency),
            proxy_pool=proxy_pool,
        )
        workers.append(w)
        states[w.worker_id] = "idle"

    tasks = [asyncio.create_task(w.run(), name=f"scout-{w.worker_id}") for w in workers]
    # serve_http() runs forever by design — must be its own cancellable task, not a
    # bare coroutine in gather(), otherwise gather() never completes even after every
    # scout reaches a terminal state and _status_printer returns. See app.cli's
    # identical fix (run()/_lifecycle_monitor) made the same day for the full story.
    signal_task = asyncio.create_task(
        signal_bus.serve_http(args.signal_port), name="signal-http")
    await asyncio.gather(
        signal_task,
        _status_printer(workers, states, tasks, signal_task),
        *tasks,
        return_exceptions=True,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Standalone slot-detection scout service")
    parser.add_argument("--mode", choices=["test", "real"], default="test")
    parser.add_argument("--count", type=int, default=3)
    parser.add_argument("--account-offset", type=int, default=0)
    parser.add_argument("--posto", type=str, default=None)
    parser.add_argument("--nationality", type=str, default=None)
    parser.add_argument("--residence", type=str, default=None)
    parser.add_argument("--proxy-file", type=str, default=None)
    parser.add_argument("--proxy-offset", type=int, default=0,
                        help="Start the proxy cursor at this index — use to manually partition a "
                             "proxy file between this process and a concurrently-running app.cli "
                             "(or another app.scout) pointed at the SAME file, since two processes "
                             "do not coordinate cursors in real time (see proxy_pool.py docstring)")
    parser.add_argument("--no-isp", action="store_true")
    parser.add_argument("--login-concurrency", type=int, default=10)
    parser.add_argument("--executor-threads", type=int, default=40)
    parser.add_argument("--signal-port", type=int, default=8991, help="separate from app.cli's :8989 so both can run at once")
    parser.add_argument("--notify-url", type=str, default=None, help="POST {slots:[...]} here when slots are found, e.g. a running app.cli's :8989/signal")
    parser.add_argument("--max-lifetime", type=float, default=DEFAULT_MAX_LIFETIME)
    args = parser.parse_args()

    # Per-run log file (stdout + stderr + logging, all in one place) — mirrors app.cli's
    # setup exactly, so both services are equally recoverable if the terminal closes.
    ts = time.strftime("%Y%m%d_%H%M%S")
    _LOG_DIR.mkdir(parents=True, exist_ok=True)
    _run_log_path = _LOG_DIR / f"scout_{args.mode}_{ts}.log"

    class _Tee:
        def __init__(self, original, file_):
            self._orig = original
            self._file = file_
        def write(self, data):
            self._orig.write(data)
            self._file.write(data)
        def flush(self):
            self._orig.flush()
            self._file.flush()
        def __getattr__(self, name):
            return getattr(self._orig, name)

    _log_fh = open(_run_log_path, "w", encoding="utf-8", errors="replace")
    sys.stdout = _Tee(sys.stdout, _log_fh)
    sys.stderr = _Tee(sys.stderr, _log_fh)

    # logging.basicConfig() AFTER the Tee swap above so its StreamHandler binds to the
    # Tee object directly — one single file handle for the whole run (print + logging
    # both flow through it). Do NOT also add a separate logging.FileHandler(_run_log_path)
    # here — a second independent handle to the same path caused most logging-module
    # records to silently vanish from app.cli's equivalent log (found and fixed this
    # session — see memory: success-rate-fixes / the dual-file-handle bug).
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-5s %(name)s  %(message)s",
        datefmt="%H:%M:%S",
        force=True,
    )
    log.info(f"[scout] run log: {_run_log_path}")

    env = _load_dotenv(_CORE_DIR / ".env")
    asyncio.run(_run(args, env))


if __name__ == "__main__":
    main()
