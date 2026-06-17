"""
Manager — creates and supervises Worker coroutines for one booking event run.

Responsibilities:
  - Load accounts (scouts from test CSV, real from mode-specific CSV)
  - Create one Worker per account, assign role and shared resources
  - Dispatch proxies on demand (each worker calls provide_proxy())
  - Start all workers + HTTP signal server + lifecycle monitor concurrently
  - Count no_slot reports: when all real workers exhaust retries → reset signal bus
  - Kill workers that exceed max_lifetime (graceful stop, then hard cancel)
  - Print live status table every MONITOR_INTERVAL seconds

CLI:
  uv run python -m engine.manager \\
      --posto 5086 \\
      --scouts 3 \\
      --mode test \\
      --max-lifetime 43200 \\
      --login-concurrency 50 \\
      --apply-concurrency 30

Mode:
  test — real workers from data/test_accounts.csv, scouts also from test CSV (different rows)
  real — real workers from data/accounts.csv, scouts always from data/test_accounts.csv
"""

import argparse
import asyncio
import json
import logging
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

import engine  # noqa: F401 — triggers __init__.py sys.path setup

from engine.signals import SlotSignalBus
from engine.worker  import Worker

log = logging.getLogger("engine.manager")

MONITOR_INTERVAL = 30   # seconds between lifecycle checks
STOP_GRACE_SECS  = 60   # seconds before hard cancel after graceful stop request

_ROOT           = Path(__file__).parent.parent
_AUTO_API       = _ROOT / "auto_api"
_REAL_ACCT_FILE = _AUTO_API / "data" / "accounts.csv"
_TEST_ACCT_FILE = _AUTO_API / "data" / "test_accounts.csv"
_SOAX_PROXY_FILE = _AUTO_API / "data" / "proxies_soax.txt"
_ISP_PROXY_FILE  = _AUTO_API / "data" / "proxies_isp.txt"
_LOG_DIR        = _AUTO_API / "data" / "logs"


class Manager:
    def __init__(
        self,
        real_accounts:      list[dict],
        scout_accounts:     list[dict],
        proxy_pool,
        slot_manager,
        posto_id:           str,
        isp_pool=None,
        nationality:        str   = "CPV",
        residence:          str   = "",
        solver_keys:        dict  = {},
        max_lifetime:       float = 43200.0,
        max_slot_retries:   int   = 3,
        event_time:         float | None = None,
        login_concurrency:  int   = 50,
        apply_concurrency:  int   = 30,
        executor_threads:   int   = 120,
        signal_port:        int   = 8989,
        log_file:           Path  | None = None,
    ):
        self._real_accounts    = real_accounts
        self._scout_accounts   = scout_accounts
        self._proxy_pool       = proxy_pool
        self._isp_pool         = isp_pool
        self._slot_manager     = slot_manager
        self.posto_id          = posto_id
        self.nationality       = nationality
        self.residence         = residence
        self._solver_keys      = solver_keys
        self.max_lifetime      = max_lifetime
        self.max_slot_retries  = max_slot_retries
        self.event_time        = event_time
        self._signal_port      = signal_port
        self._log_file         = log_file

        self._executor  = ThreadPoolExecutor(max_workers=executor_threads,
                                             thread_name_prefix="eng-worker")
        self._login_sem = asyncio.Semaphore(login_concurrency)
        self._apply_sem = asyncio.Semaphore(apply_concurrency)
        self._signal_bus = SlotSignalBus(slot_manager)

        self._workers:       list[Worker]        = []
        self._workers_by_id: dict[str, Worker]   = {}
        self._tasks:         dict[str, asyncio.Task] = {}
        self._states:        dict[str, str]       = {}

        # No-slot fan-out tracking (reset each signal round)
        self._no_slot_round: set[str] = set()

        # Per-booking-event exit IP dedup (in-memory, not persisted)
        self._event_ips:     set[str]        = set()
        self._event_ip_lock: threading.Lock  = threading.Lock()

    # ── Proxy dispatch ─────────────────────────────────────────────────────────

    def provide_proxy(self) -> str:
        """Thread-safe proxy dispatch with per-event exit IP dedup."""
        return self._proxy_pool.advance_unique(self._event_ips, self._event_ip_lock)

    # ── Status callback (called by workers on every state transition) ──────────

    def _on_worker_status(self, worker_id: str, new_state: str, detail: dict) -> None:
        self._states[worker_id] = new_state
        self._log_event(worker_id, new_state, detail)

        if new_state == "no_slot_exhausted":
            self._no_slot_round.add(worker_id)
            active = self._active_real_count()
            log.info(
                f"[manager] no_slot {len(self._no_slot_round)}/{active} "
                f"real workers reported this round"
            )
            if active > 0 and len(self._no_slot_round) >= active:
                log.info("[manager] all real workers no_slot — resetting signal bus")
                self._no_slot_round.clear()
                self._event_ips.clear()
                self._signal_bus.reset()

    def _active_real_count(self) -> int:
        # Count ALL non-terminal real workers, including those still in login/warmup.
        #
        # Why: signal_bus.wait() returns immediately when the event is already set, so
        # workers that finish warmup AFTER a signal fires go directly into apply — they
        # never enter a blocking wait(). The bus must therefore stay set until every
        # worker (including still-warming ones) has had a chance to apply and report.
        # Excluding pre-warmed workers caused premature bus reset: W1 reported no_slot,
        # active=1, 1>=1 → reset — W2..WN finished warmup and found the bus cleared.
        #
        # Workers that fail (login_exhausted, warmup_error) go to terminal states and
        # are excluded, so a failed worker never blocks the reset.
        terminal = {"done", "failed", "expired", "stopped", "blocked", "crashed"}
        return sum(
            1 for w in self._workers
            if w.role == "real"
            and self._states.get(w.worker_id) not in terminal
            and w.worker_id in self._tasks
            and not self._tasks[w.worker_id].done()
        )

    def _log_event(self, worker_id: str, state: str, detail: dict) -> None:
        entry = {"ts": time.time(), "worker": worker_id, "state": state, **detail}
        if self._log_file:
            try:
                with open(self._log_file, "a", encoding="utf-8") as f:
                    f.write(json.dumps(entry) + "\n")
            except Exception:
                pass

    # ── Worker creation ────────────────────────────────────────────────────────

    def _create_workers(self) -> None:
        from engine.isp_proxy_pool import IspFirstRequester

        def _soax_advance() -> str:
            return self._proxy_pool.advance_unique(self._event_ips, self._event_ip_lock)

        common = dict(
            signal_bus      = self._signal_bus,
            slot_manager    = self._slot_manager,
            status_cb       = self._on_worker_status,
            executor        = self._executor,
            solver_keys     = self._solver_keys,
            posto_id        = self.posto_id,
            nationality     = self.nationality,
            residence       = self.residence,
            max_lifetime    = self.max_lifetime,
            max_slot_retries= self.max_slot_retries,
            event_time      = self.event_time,
            login_sem       = self._login_sem,
            apply_sem       = self._apply_sem,
            proxy_pool      = self._proxy_pool,
        )
        for acct in self._scout_accounts:
            # ISP-first for scouts too, SOAX fallback
            if self._isp_pool is not None:
                req = IspFirstRequester(self._isp_pool, _soax_advance)
            else:
                req = _soax_advance
            w = Worker(account=acct, role="scout", proxy_requester=req, **common)
            self._workers.append(w)
            self._workers_by_id[w.worker_id] = w
            self._states[w.worker_id] = "idle"

        for acct in self._real_accounts:
            # ISP-first, SOAX fallback. One requester per worker (not shared).
            if self._isp_pool is not None:
                req = IspFirstRequester(self._isp_pool, _soax_advance)
            else:
                req = _soax_advance
            w = Worker(account=acct, role="real", proxy_requester=req, **common)
            self._workers.append(w)
            self._workers_by_id[w.worker_id] = w
            self._states[w.worker_id] = "idle"

    # ── Main run ───────────────────────────────────────────────────────────────

    async def run(self) -> None:
        self._create_workers()
        n_scouts = len(self._scout_accounts)
        n_real   = len(self._real_accounts)
        log.info(
            f"[manager] starting: {n_real} real workers, {n_scouts} scouts, "
            f"posto={self.posto_id}  nationality={self.nationality}  residence={self.residence}"
        )

        for worker in self._workers:
            task = asyncio.create_task(
                worker.run(), name=f"worker-{worker.worker_id}")
            self._tasks[worker.worker_id] = task

        await asyncio.gather(
            self._signal_bus.serve_http(self._signal_port),
            self._lifecycle_monitor(),
            *self._tasks.values(),
            return_exceptions=True,
        )
        log.info("[manager] all coroutines finished")
        self._print_status_table(final=True)

    # ── Lifecycle monitor ──────────────────────────────────────────────────────

    async def _lifecycle_monitor(self) -> None:
        tick = 0
        while True:
            await asyncio.sleep(MONITOR_INTERVAL)
            tick += 1
            now = time.time()

            for wid, task in list(self._tasks.items()):
                worker = self._workers_by_id[wid]

                # Record crash
                if task.done() and not task.cancelled():
                    exc = task.exception()
                    if exc and self._states.get(wid) not in ("done", "failed", "expired", "stopped"):
                        log.error(f"[{wid}] task crashed: {exc}")
                        self._states[wid] = "crashed"
                    continue

                # Request graceful stop on lifetime exceeded
                if (
                    worker.started_at
                    and (now - worker.started_at) > self.max_lifetime
                    and self._states.get(wid) not in (
                        "expired", "stopped", "done", "failed", "crashed", "blocked"
                    )
                ):
                    log.warning(f"[{wid}] max_lifetime exceeded — requesting graceful stop")
                    worker._stop_event.set()
                    if worker._stop_requested_at == 0.0:
                        worker._stop_requested_at = now

                # Hard cancel if grace period expired
                if (
                    worker._stop_requested_at > 0
                    and (now - worker._stop_requested_at) > STOP_GRACE_SECS
                    and not task.done()
                ):
                    log.warning(f"[{wid}] grace period expired — cancelling task")
                    task.cancel()

            self._print_status_table()

            # Stop scouts when all real workers are in terminal states
            all_real_done = all(
                self._states.get(w.worker_id) in ("done", "failed", "expired", "stopped", "blocked", "crashed")
                or self._tasks[w.worker_id].done()
                for w in self._workers if w.role == "real"
            )
            if all_real_done and self._real_accounts:
                log.info("[manager] all real workers finished — stopping scouts and exiting")
                for w in self._workers:
                    if w.role == "scout" and not self._tasks[w.worker_id].done():
                        w._stop_event.set()
                return  # exit monitor; gather() will complete naturally

    # ── Status table ──────────────────────────────────────────────────────────

    def _print_status_table(self, final: bool = False) -> None:
        lines = ["\n" + "━" * 72]
        header = "FINAL STATUS" if final else datetime.now().strftime("%H:%M:%S")
        lines.append(f" {header}")
        lines.append(f" {'Worker':<24} {'Role':<7} {'State':<22} {'Proxy (session)'}")
        lines.append(" " + "─" * 70)
        for w in self._workers:
            proxy_label = ""
            if w.proxy:
                m = re.search(r"sessionid-([^-@:]+)", w.proxy)
                proxy_label = m.group(1)[:16] if m else (w.proxy.split("@")[-1] if "@" in w.proxy else w.proxy)[-20:]
            state = self._states.get(w.worker_id, "?")
            lines.append(f" {w.username:<24} {w.role:<7} {state:<22} {proxy_label}")
        lines.append("━" * 72)
        sm = self._slot_manager.stats()
        lines.append(
            f" Signal rounds: {self._signal_bus.fired_count} | "
            f"Pool: {sm['pool']}  Leased: {sm['leased']}  Taken: {sm['taken']} | "
            f"No-slot this round: {len(self._no_slot_round)}/{self._active_real_count()}"
        )
        print("\n".join(lines), flush=True)


# ── CLI entry ──────────────────────────────────────────────────────────────────

def _load_dotenv(env_file: Path) -> dict[str, str]:
    env: dict[str, str] = {}
    if not env_file.exists():
        return env
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        env[k.strip()] = v.strip()
    return env


def _load_accounts(csv_file: Path, statuses: tuple[str, ...] = ("verified", "active", "login_failed")) -> list[dict]:
    from account_pool import AccountPool
    pool = AccountPool(csv_file)
    return [a for a in pool.all() if a.get("status") in statuses]


def _kill_stale_browser_procs() -> None:
    """Kill orphaned Playwright processes left by previous runs."""
    import subprocess
    killed = 0
    for name in ("chrome-headless-shell.exe",):  # node.exe also used by VS Code — do not kill
        try:
            result = subprocess.run(
                ["taskkill", "/F", "/IM", name],
                capture_output=True, text=True)
            if result.returncode == 0:
                killed += 1
        except Exception:
            pass
    if killed:
        import time as _t
        _t.sleep(1)  # brief pause so OS reclaims handles before Playwright relaunches


async def _run(args: argparse.Namespace, env: dict, ts: str) -> None:
    from proxy_pool  import PersistentProxyPool
    from slot_manager import SlotManager

    _kill_stale_browser_procs()

    _LOG_DIR.mkdir(parents=True, exist_ok=True)
    _PDF_DIR = _AUTO_API / "data" / "pdfs"
    _PDF_DIR.mkdir(parents=True, exist_ok=True)

    log_file = _LOG_DIR / f"manager_{ts}.jsonl"

    # Solver keys
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

    # Proxy pools
    _proxy_file = _AUTO_API / "data" / args.proxy_file if args.proxy_file else _SOAX_PROXY_FILE
    log.info(f"[proxy] pool file: {_proxy_file.name}")
    proxy_pool = PersistentProxyPool(_proxy_file)

    isp_pool = None
    if _ISP_PROXY_FILE.exists() and not args.no_isp:
        from engine.isp_proxy_pool import IspProxyPool
        isp_pool = IspProxyPool(str(_ISP_PROXY_FILE))
        log.info(f"[proxy] ISP pool loaded: {isp_pool.size_available}/{isp_pool.size_total} proxies")
    elif args.no_isp:
        log.info("[proxy] --no-isp: using SOAX only")
    else:
        log.warning(f"[proxy] ISP proxy file not found: {_ISP_PROXY_FILE} — real workers will use SOAX")

    # Slot manager
    slot_manager = SlotManager()

    # Load accounts
    scout_accounts_pool = _load_accounts(_TEST_ACCT_FILE)
    if args.account_offset:
        scout_accounts_pool = scout_accounts_pool[args.account_offset:]
    if not scout_accounts_pool:
        log.error(f"No verified/active accounts in {_TEST_ACCT_FILE} for scouts")
        sys.exit(1)
    scout_accounts = scout_accounts_pool[:args.scouts]

    if args.mode == "test":
        # Real workers: remaining rows from test CSV (not used as scouts)
        scout_usernames = {a["username"] for a in scout_accounts}
        real_accounts = [
            a for a in scout_accounts_pool
            if a["username"] not in scout_usernames
        ]
        nationality = env.get("TEST_NATIONALITY", "CPV")
        residence   = env.get("TEST_RESIDENCE",   nationality)
        posto_id    = args.posto or env.get("TEST_POSTO_ID", "5086")
    else:
        real_accounts = list(reversed(_load_accounts(_REAL_ACCT_FILE)))
        nationality   = args.nationality or env.get("NATIONALITY", "CPV")
        residence     = args.residence   or env.get("RESIDENCE",   nationality)
        posto_id      = args.posto or env.get("POSTO_ID", "5086")

    if args.count > 0:
        real_accounts = real_accounts[:args.count]

    log.info(
        f"[manager] mode={args.mode}  scouts={len(scout_accounts)}  "
        f"real={len(real_accounts)}  posto={posto_id}  "
        f"nationality={nationality}  residence={residence}"
    )
    if not real_accounts and args.scouts == 0:
        log.error("No accounts to run — check CSV files")
        sys.exit(1)

    manager = Manager(
        real_accounts     = real_accounts,
        scout_accounts    = scout_accounts,
        proxy_pool        = proxy_pool,
        isp_pool          = isp_pool,
        slot_manager      = slot_manager,
        posto_id          = posto_id,
        nationality       = nationality,
        residence         = residence,
        solver_keys       = solver_keys,
        max_lifetime      = float(args.max_lifetime),
        max_slot_retries  = args.max_slot_retries,
        event_time        = None,
        login_concurrency = args.login_concurrency,
        apply_concurrency = args.apply_concurrency,
        executor_threads  = args.executor_threads,
        signal_port       = args.signal_port,
        log_file          = log_file,
    )
    await manager.run()


def main() -> None:
    # Force UTF-8 unbuffered stdout so session.py print() calls appear in real-time.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-5s %(name)s  %(message)s",
        datefmt="%H:%M:%S",
    )

    env = _load_dotenv(_AUTO_API / ".env")

    parser = argparse.ArgumentParser(
        description="Engine manager — login workers, keep sessions alive, apply when slots appear")

    parser.add_argument("--mode",               choices=("test", "real"), default="test",
                        help="test: accounts from test_accounts.csv; real: from accounts.csv")
    parser.add_argument("--posto",              default="",
                        help="Consular post ID (overrides .env POSTO_ID / TEST_POSTO_ID)")
    parser.add_argument("--nationality",        default="",
                        help="ISO nationality code (real mode, overrides .env NATIONALITY)")
    parser.add_argument("--residence",          default="",
                        help="Country of residence ISO code (real mode, overrides .env RESIDENCE)")
    parser.add_argument("--scouts",             type=int, default=3,
                        help="Number of scout workers (fake accounts, from test_accounts.csv)")
    parser.add_argument("--count",              type=int, default=0,
                        help="Max real worker accounts (0 = all available)")
    parser.add_argument("--account-offset",    type=int, default=0, dest="account_offset",
                        help="Skip first N accounts from pool (use fresh accounts after burned ones)")
    parser.add_argument("--max-lifetime",       type=int, default=43200, dest="max_lifetime",
                        help="Worker session max lifetime in seconds (default: 43200 = 12 h)")
    parser.add_argument("--max-slot-retries",   type=int, default=3, dest="max_slot_retries",
                        help="Per-signal apply retries before returning no_slot (default: 3)")
    parser.add_argument("--login-concurrency",  type=int, default=50, dest="login_concurrency",
                        help="Max simultaneous browser logins (default: 50)")
    parser.add_argument("--apply-concurrency",  type=int, default=30, dest="apply_concurrency",
                        help="Max simultaneous apply_book calls (default: 30)")
    parser.add_argument("--executor-threads",   type=int, default=120, dest="executor_threads",
                        help="ThreadPoolExecutor size (default: 120)")
    parser.add_argument("--signal-port",        type=int, default=8989, dest="signal_port",
                        help="HTTP signal server port (default: 8989)")
    parser.add_argument("--no-isp",             action="store_true", dest="no_isp",
                        help="Skip ISP proxy pool even if proxies_isp.txt exists; use SOAX only")
    parser.add_argument("--proxy-file",          type=str, default=None, dest="proxy_file",
                        help="Override SOAX proxy file (e.g. proxies_webshare.txt)")

    args = parser.parse_args()

    # --- Per-run log file (stdout + stderr + logging, all in one place) ----------
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    _LOG_DIR.mkdir(parents=True, exist_ok=True)
    _run_log_path = _LOG_DIR / f"run_{args.mode}_{ts}.log"

    class _Tee:
        """Forwards writes to both the original stream and a file."""
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

    # Also send logging records to the same file.
    _file_handler = logging.FileHandler(_run_log_path, encoding="utf-8")
    _file_handler.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)-5s %(name)s  %(message)s", datefmt="%H:%M:%S"))
    logging.getLogger().addHandler(_file_handler)

    log.info(f"[manager] run log: {_run_log_path}")
    # ---------------------------------------------------------------------------

    asyncio.run(_run(args, env, ts=ts))


if __name__ == "__main__":
    main()
