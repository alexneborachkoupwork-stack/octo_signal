"""
Booking Event Service — creates and supervises real-account Worker coroutines for
one booking event run. Booking-only: no scouting, no slot polling responsibility.
Slot detection lives entirely in the separate, standalone `app.scout` service —
this process only warms real workers, keeps sessions alive, and applies the
moment a signal arrives on its HTTP /signal endpoint (fired either manually, or
by a separately-running `app.scout --notify-url http://<this-host>:<signal-port>/signal`).

Responsibilities:
  - Load real accounts from the mode-specific CSV
  - Create one Worker(role="real") per account
  - Dispatch proxies on demand (each worker calls provide_proxy())
  - Start all workers + HTTP signal server + lifecycle monitor concurrently
  - Count no_slot reports: when all workers exhaust retries → reset signal bus
  - Kill workers that exceed max_lifetime (graceful stop, then hard cancel)
  - Print live status table every MONITOR_INTERVAL seconds

CLI:
  uv run python -m app.cli \\
      --posto 5086 \\
      --mode test \\
      --max-lifetime 43200 \\
      --login-concurrency 50 \\
      --apply-concurrency 30

Mode:
  test — real workers from data/test_accounts.csv
  real — real workers from data/accounts.csv

Pair with the scout service (separate process), pointed at the same target and
at this process's /signal endpoint:
  python -m app.scout --mode test --posto 5086 --notify-url http://localhost:8989/signal
"""

import argparse
import asyncio
import json
import logging
import os
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

import app  # noqa: F401 — triggers __init__.py sys.path setup (adds app/core to sys.path)

from app.core.signals import SlotSignalBus
from app.worker        import Worker

log = logging.getLogger("app.cli")

MONITOR_INTERVAL = 30   # seconds between lifecycle checks
STOP_GRACE_SECS  = 60   # seconds before hard cancel after graceful stop request

_ROOT           = Path(__file__).parent.parent
_CORE_DIR       = _ROOT / "app" / "core"
_REAL_ACCT_FILE = _CORE_DIR / "data" / "accounts.csv"
_TEST_ACCT_FILE = _CORE_DIR / "data" / "test_accounts.csv"
_WEBSHARE_PROXY_FILE = _CORE_DIR / "data" / "proxies_webshare.txt"
_SOAX_PROXY_FILE     = _CORE_DIR / "data" / "proxies_soax.txt"
_ISP_PROXY_FILE      = _CORE_DIR / "data" / "proxies_isp.txt"
_LOG_DIR        = _CORE_DIR / "data" / "logs"


class Manager:
    def __init__(
        self,
        real_accounts:      list[dict],
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
        acct_file:          Path  | None = None,
        max_rehab_rounds:   int   = 5,
    ):
        self._real_accounts    = real_accounts
        self._acct_file        = acct_file
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

        self._executor  = ThreadPoolExecutor(max_workers=executor_threads,
                                             thread_name_prefix="eng-worker")
        # Open the JSONL event log once so every _log_event() call is a simple write,
        # not a repeated open/close cycle (which was the previous behavior).
        self._log_fh = open(log_file, "a", encoding="utf-8") if log_file else None
        self._login_sem = asyncio.Semaphore(login_concurrency)
        self._apply_sem = asyncio.Semaphore(apply_concurrency)
        self._signal_bus = SlotSignalBus(slot_manager)
        self._signal_task: asyncio.Task | None = None  # set in run(); serve_http() runs forever, must be cancellable

        self._workers:       list[Worker]        = []
        self._workers_by_id: dict[str, Worker]   = {}
        self._tasks:         dict[str, asyncio.Task] = {}
        self._states:        dict[str, str]       = {}

        # No-slot fan-out tracking (reset each signal round)
        self._no_slot_round: set[str] = set()

        # Rehabilitation: soft/unknown failed workers are re-queued here and respawned
        self._rehab_queue:    list[dict]     = []
        self._rehab_attempts: dict[str, int] = {}
        self._max_rehab_rounds               = max_rehab_rounds

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

        if new_state == "failed" and self._acct_file:
            reason = detail.get("reason", "")
            if reason == "login_hard_exhausted":
                # Hard failure = explicit server-side account rejection (wrong credentials,
                # account suspended, etc.) — mark login_failed so future runs skip this account.
                # Soft/unknown/global exhaustion is proxy attrition, NOT account failure;
                # those reasons leave the account as "verified" so it's retried next run
                # with a healthier proxy pool.
                try:
                    from account_pool import AccountPool
                    AccountPool(self._acct_file).mark_login_failed(
                        worker_id,
                        notes=f"hard_exhausted at {datetime.now().isoformat(timespec='seconds')}",
                    )
                except Exception as _ue:
                    log.warning(f"[manager] could not persist login_failed for {worker_id}: {_ue}")
            elif reason in ("login_soft_exhausted", "login_unknown_exhausted",
                            "login_global_exhausted"):
                # Proxy attrition — account is fine. Queue for rehab (fresh proxy next wave).
                attempts = self._rehab_attempts.get(worker_id, 0)
                if attempts < self._max_rehab_rounds:
                    acct = self._account_by_username.get(worker_id)
                    if acct:
                        self._rehab_queue.append(acct)
                        self._rehab_attempts[worker_id] = attempts + 1
                        log.info(
                            f"[manager] {worker_id} queued for rehab "
                            f"(round {attempts + 1}/{self._max_rehab_rounds}, reason={reason})"
                        )
                else:
                    log.warning(
                        f"[manager] {worker_id} exhausted {self._max_rehab_rounds} rehab "
                        f"rounds ({reason}) — retiring permanently"
                    )

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
        # Count ALL non-terminal workers, including those still in login/warmup.
        # (Service B only ever has role="real" workers — scouting lives entirely in
        # the separate app.scout service — so no role filter is needed here.)
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
            if self._states.get(w.worker_id) not in terminal
            and w.worker_id in self._tasks
            and not self._tasks[w.worker_id].done()
        )

    def _log_event(self, worker_id: str, state: str, detail: dict) -> None:
        if self._log_fh is None:
            return
        entry = {"ts": time.time(), "worker": worker_id, "state": state, **detail}
        try:
            self._log_fh.write(json.dumps(entry) + "\n")
            self._log_fh.flush()
        except Exception:
            pass

    # ── Worker creation ────────────────────────────────────────────────────────

    def _create_workers(self) -> None:
        self._account_by_username = {a["username"]: a for a in self._real_accounts}

        def _proxy_req() -> str:
            return self._proxy_pool.advance_unique(self._event_ips, self._event_ip_lock)
        self._proxy_requester = _proxy_req

        self._worker_common = dict(
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
        for acct in self._real_accounts:
            # Login proxy = Webshare (the main pool). ISP is not used for login —
            # static ISP IPs are blocked by DataDome on the login POST.
            w = Worker(account=acct, role="real", proxy_requester=self._proxy_requester,
                       **self._worker_common)
            self._workers.append(w)
            self._workers_by_id[w.worker_id] = w
            self._states[w.worker_id] = "idle"

    # ── Rehabilitation ─────────────────────────────────────────────────────────

    def _spawn_rehab_wave(self) -> None:
        """
        Respawn workers for accounts that failed due to proxy attrition (soft/unknown
        exhaustion). Each gets a fresh proxy from the pool. Hard-failed accounts are
        never queued here — they stay retired.
        """
        accounts = list(self._rehab_queue)
        self._rehab_queue.clear()
        log.info(f"[manager] rehab wave: respawning {len(accounts)} workers")
        for acct in accounts:
            username = acct["username"]
            round_num = self._rehab_attempts[username]
            old = self._workers_by_id.get(username)
            w = Worker(account=acct, role="real",
                       proxy_requester=self._proxy_requester,
                       **self._worker_common)
            # Replace old Worker in-place so the status table stays ordered.
            if old is not None and old in self._workers:
                self._workers[self._workers.index(old)] = w
            else:
                self._workers.append(w)
            self._workers_by_id[username] = w
            self._states[username] = "idle"
            task = asyncio.create_task(w.run(), name=f"worker-{username}-r{round_num}")
            self._tasks[username] = task
            log.info(f"[manager] rehab: {username} respawned (round {round_num})")

    # ── Main run ───────────────────────────────────────────────────────────────

    async def run(self) -> None:
        self._create_workers()
        n_real = len(self._real_accounts)
        log.info(
            f"[manager] starting: {n_real} real workers, "
            f"posto={self.posto_id}  nationality={self.nationality}  residence={self.residence}"
        )
        # Prune stale session files; pass active usernames so only known accounts are kept.
        import session_store as _ss
        active = {w.username for w in self._workers}
        _ss.cleanup_stale(max_age_days=7, active_usernames=active)

        for worker in self._workers:
            task = asyncio.create_task(
                worker.run(), name=f"worker-{worker.worker_id}")
            self._tasks[worker.worker_id] = task

        # serve_http() runs forever (while True: sleep(3600)) by design — it must be
        # its own cancellable task, not a bare coroutine in gather(), otherwise the
        # gather below never completes even after every worker finishes and
        # _lifecycle_monitor() returns. (Found 2026-06-18: "[manager] all real workers
        # finished — exiting" was printing correctly, but the process never actually
        # exited on its own — every prior test run was being manually killed
        # afterward, masking this.) _lifecycle_monitor cancels this task itself once
        # all workers are terminal.
        self._signal_task = asyncio.create_task(
            self._signal_bus.serve_http(self._signal_port), name="signal-http")

        try:
            await asyncio.gather(
                self._signal_task,
                self._lifecycle_monitor(),
                *self._tasks.values(),
                return_exceptions=True,
            )
        finally:
            if self._log_fh is not None:
                try:
                    self._log_fh.close()
                except Exception:
                    pass
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

            # Exit once all real workers are in terminal states
            all_real_done = all(
                self._states.get(w.worker_id) in ("done", "failed", "expired", "stopped", "blocked", "crashed")
                or self._tasks[w.worker_id].done()
                for w in self._workers
            )
            if all_real_done and self._real_accounts:
                if self._rehab_queue:
                    # Some workers failed due to proxy attrition — give them another chance.
                    self._spawn_rehab_wave()
                    # Loop continues; _lifecycle_monitor picks up the new tasks naturally.
                else:
                    log.info("[manager] all real workers finished — exiting")
                    self._signal_task.cancel()  # serve_http() never returns on its own — see run()
                    return

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
        k, v = k.strip(), v.strip()
        env[k] = v
        # Also push into os.environ (a real env var, if already set, wins) so
        # modules that check os.environ.get(...) directly — e.g. batch_apply.py's
        # EVIDENCE_SCREENSHOTS flag — actually see values set only in .env, not
        # just the args/solver_keys this dict feeds explicitly.
        os.environ.setdefault(k, v)
    return env


def _load_accounts(csv_file: Path, statuses: tuple[str, ...] = ("verified", "active")) -> list[dict]:
    # "login_failed" deliberately excluded by default — Manager._on_worker_status persists
    # that status on login-exhaustion specifically so these accounts stop being silently
    # re-picked by future runs. Pass statuses=(...,"login_failed") explicitly to retry them
    # (e.g. after getting a fresh proxy pool, since most exhaustions are proxy-luck, not
    # account-specific).
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
    _PDF_DIR = _CORE_DIR / "data" / "pdfs"
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
    # Priority: --proxy-file override > Webshare (default) > SOAX fallback.
    # ISP proxies are intentionally excluded from the login path — static ISP IPs
    # are consistently blocked by DataDome on the login POST. Webshare rotating
    # SOCKS5 proxies are the correct first-choice for login.
    if args.proxy_file:
        _proxy_file = _CORE_DIR / "data" / args.proxy_file
    elif _WEBSHARE_PROXY_FILE.exists():
        _proxy_file = _WEBSHARE_PROXY_FILE
    else:
        _proxy_file = _SOAX_PROXY_FILE
    log.info(f"[proxy] pool file: {_proxy_file.name}" + (f"  offset={args.proxy_offset}" if args.proxy_offset else ""))
    proxy_pool = PersistentProxyPool(_proxy_file, proxy_offset=args.proxy_offset)

    # ISP pool: available as slot-keepalive proxies only — never used for login.
    isp_pool = None
    if _ISP_PROXY_FILE.exists() and not args.no_isp:
        from app.core.isp_proxy_pool import IspProxyPool
        isp_pool = IspProxyPool(str(_ISP_PROXY_FILE))
        log.info(f"[proxy] ISP pool loaded: {isp_pool.size_available}/{isp_pool.size_total} proxies (keepalive only — not used for login)")

    # Slot manager
    slot_manager = SlotManager()

    # Load accounts
    if args.mode == "test":
        acct_file = _TEST_ACCT_FILE
        real_accounts = _load_accounts(acct_file)
        if not real_accounts:
            log.error(f"No verified/active accounts in {acct_file}")
            sys.exit(1)
        if args.account_offset:
            real_accounts = real_accounts[args.account_offset:]
        nationality = env.get("TEST_NATIONALITY", "CPV")
        residence   = env.get("TEST_RESIDENCE",   nationality)
        posto_id    = args.posto or env.get("TEST_POSTO_ID", "5086")
    else:
        acct_file = _REAL_ACCT_FILE
        real_accounts = list(reversed(_load_accounts(acct_file)))
        if args.account_offset:
            real_accounts = real_accounts[args.account_offset:]
        nationality   = args.nationality or env.get("NATIONALITY", "CPV")
        residence     = args.residence   or env.get("RESIDENCE",   nationality)
        posto_id      = args.posto or env.get("POSTO_ID", "5086")

    if args.count > 0:
        real_accounts = real_accounts[:args.count]

    log.info(
        f"[manager] mode={args.mode}  real={len(real_accounts)}  posto={posto_id}  "
        f"nationality={nationality}  residence={residence}"
    )
    if not real_accounts:
        log.error("No accounts to run — check CSV files")
        sys.exit(1)

    manager = Manager(
        real_accounts     = real_accounts,
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
        acct_file         = acct_file,
        max_rehab_rounds  = args.max_rehab_rounds,
    )
    await manager.run()


def main() -> None:
    # Force UTF-8 unbuffered stdout so session.py print() calls appear in real-time.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)

    env = _load_dotenv(_CORE_DIR / ".env")

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
    parser.add_argument("--max-rehab-rounds",     type=int, default=5, dest="max_rehab_rounds",
                        help="Max times a soft/proxy-failed worker is retried (default: 5; 0=disabled)")
    parser.add_argument("--proxy-offset",        type=int, default=0, dest="proxy_offset",
                        help="Start the proxy cursor at this index — use to manually partition a "
                             "proxy file between this process and a concurrently-running app.scout "
                             "(or another app.cli) pointed at the SAME file, since two processes "
                             "do not coordinate cursors in real time (see proxy_pool.py docstring)")

    args = parser.parse_args()

    # --- Per-run log file (stdout + stderr + logging, all in one place) ----------
    # IMPORTANT: exactly ONE file handle for the whole run. An earlier version opened
    # a second, independent handle via logging.FileHandler(_run_log_path) pointed at
    # the SAME path as this Tee's handle — two concurrent handles to one file on
    # Windows caused most logging-module records (login attempt warnings, etc.) to
    # silently vanish from the file while print()-based [session]/[solver] lines
    # still came through fine via the Tee. Fix: logging.basicConfig() is called
    # AFTER sys.stderr is swapped to the Tee, so its StreamHandler binds to the Tee
    # object directly — logging records flow through the exact same single file
    # handle as everything else, no second handle involved.
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

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-5s %(name)s  %(message)s",
        datefmt="%H:%M:%S",
        force=True,  # discard any handler a prior basicConfig/import may have installed
    )

    log.info(f"[manager] run log: {_run_log_path}")
    # ---------------------------------------------------------------------------

    asyncio.run(_run(args, env, ts=ts))


if __name__ == "__main__":
    main()
