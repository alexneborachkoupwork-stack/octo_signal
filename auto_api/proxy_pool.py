"""
Proxy pool manager — two classes:

ProxyPool
  Original pool for ad-hoc use (recent/rotate/random/first modes).

PersistentProxyPool
  Cross-run cursor + exit-IP deduplication for batch registration / login.
  State persists in <proxy_file>.proxy_state.json:
    cursor    — next list index (in-memory, flushed every CURSOR_FLUSH_EVERY calls)
    ip_cache  — {session_id: exit_ip} — avoids repeat HTTP checks for known proxies
    used_ips  — {exit_ip: {account, at}} — IPs already claimed (any run)
    daily_burns — server-side blocks, pruned nightly
"""

import atexit
import datetime
import json
import re
import random as _random
import threading
from pathlib import Path

# How often to flush the in-memory cursor to disk.
# Crash can replay at most this many proxy slots (acceptable trade-off for speed).
CURSOR_FLUSH_EVERY = 50

# IP-check endpoints, cycled round-robin to avoid rate-limiting any single one.
_IP_CHECK_URLS = [
    "https://api.ipify.org",
    "https://api4.my-ip.io/ip",
    "https://ipv4.icanhazip.com",
    "https://checkip.amazonaws.com",
]
_ip_url_idx  = 0
_ip_url_lock = threading.Lock()

_STATE_SUFFIX = ".proxy_state.json"

MODES = ("recent", "rotate", "random", "first")


class ProxyPool:
    def __init__(self, proxy_file: str | Path):
        self._file  = Path(proxy_file)
        self._state_file = self._file.with_suffix(_STATE_SUFFIX)
        self._proxies: list[str] = []
        self._failed:  set[str]  = set()
        self._state:   dict      = {}
        self._lock = threading.Lock()

        self._load_proxies()
        self._load_state()

    # ── Public API ─────────────────────────────────────────────────────────────

    def get(self, mode: str = "recent") -> str:
        """
        Return a proxy URL string (http://user:pass@host:port).
        Raises RuntimeError if no proxies are available.
        """
        if mode not in MODES:
            raise ValueError(f"Unknown mode '{mode}'. Choose from: {MODES}")

        pool = self._available()
        if not pool:
            # All proxies failed — reset and try again
            print("[proxy] All proxies marked failed — resetting failure list")
            self._failed.clear()
            pool = self._available()
        if not pool:
            raise RuntimeError("Proxy pool is empty")

        if mode == "first":
            proxy = pool[0]

        elif mode == "recent":
            recent = self._state.get("recent")
            proxy  = recent if recent in pool else pool[0]

        elif mode == "rotate":
            idx   = self._state.get("rotate_index", 0) % len(pool)
            proxy = pool[idx]
            self._state["rotate_index"] = (idx + 1) % len(pool)
            self._save_state()

        elif mode == "random":
            proxy = _random.choice(pool)

        return proxy

    def mark_ok(self, proxy: str) -> None:
        """Record this proxy as the last successful one."""
        with self._lock:
            self._failed.discard(proxy)
            self._state["recent"] = proxy
            self._save_state()

    def mark_failed(self, proxy: str) -> None:
        """Exclude this proxy from the pool until all proxies fail (then reset)."""
        with self._lock:
            print(f"[proxy] marking failed: {_redact(proxy)}")
            self._failed.add(proxy)
            if self._state.get("recent") == proxy:
                del self._state["recent"]
            self._save_state()

    def burn_today(self, proxy: str, reason: str = "") -> None:
        """Mark proxy as burned for today only — auto-clears at midnight."""
        with self._lock:
            today = datetime.date.today().isoformat()
            burns = self._state.setdefault("daily_burns", {})
            daily = burns.setdefault(today, {})
            daily[proxy] = reason or "burned"
            # Prune dates older than today
            self._state["daily_burns"] = {d: v for d, v in burns.items() if d >= today}
            self._save_state()
        print(f"[proxy] burn_today: {_redact(proxy)}  reason={reason or 'burned'}")

    def daily_available(self) -> list[str]:
        """Proxies not burned today and not permanently failed."""
        with self._lock:
            today = datetime.date.today().isoformat()
            today_burns = set(
                self._state.get("daily_burns", {}).get(today, {}).keys()
            )
            return [p for p in self._proxies
                    if p not in self._failed and p not in today_burns]

    def is_burned_today(self, proxy: str) -> bool:
        today = datetime.date.today().isoformat()
        return proxy in self._state.get("daily_burns", {}).get(today, {})

    def all(self) -> list[str]:
        return list(self._proxies)

    def available(self) -> list[str]:
        return self._available()

    def status(self) -> None:
        print(f"[proxy] file    : {self._file}")
        print(f"[proxy] total   : {len(self._proxies)}")
        print(f"[proxy] failed  : {len(self._failed)}")
        print(f"[proxy] recent  : {_redact(self._state.get('recent', '(none)'))}")
        print(f"[proxy] rotate@ : {self._state.get('rotate_index', 0)}")
        for i, p in enumerate(self._proxies):
            tag = " [FAILED]" if p in self._failed else ""
            mark = " <recent>" if p == self._state.get("recent") else ""
            print(f"  [{i:02d}] {_redact(p)}{tag}{mark}")

    # ── Internals ──────────────────────────────────────────────────────────────

    def _available(self) -> list[str]:
        return [p for p in self._proxies if p not in self._failed]

    def _load_proxies(self) -> None:
        if not self._file.exists():
            raise FileNotFoundError(f"Proxy file not found: {self._file}")
        raw = self._file.read_text(encoding="utf-8").splitlines()
        seen = set()
        for line in raw:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            url = _normalise(line)
            if url not in seen:
                self._proxies.append(url)
                seen.add(url)
        if not self._proxies:
            raise ValueError(f"No proxies found in {self._file}")
        print(f"[proxy] loaded {len(self._proxies)} proxies from {self._file.name}")

    def _load_state(self) -> None:
        if self._state_file.exists():
            try:
                self._state = json.loads(self._state_file.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                self._state = {}
        # Prune state entries that refer to proxies no longer in the file
        if self._state.get("recent") not in self._proxies:
            self._state.pop("recent", None)

    def _save_state(self) -> None:
        try:
            self._state_file.write_text(
                json.dumps(self._state, indent=2), encoding="utf-8"
            )
        except OSError:
            pass


# ── Helpers ────────────────────────────────────────────────────────────────────

def _normalise(line: str) -> str:
    """Add http:// scheme if missing; preserve socks5:// and socks4:// as-is."""
    if line.startswith(("http://", "https://", "socks5://", "socks4://")):
        return line
    return f"http://{line}"


def _redact(url: str) -> str:
    """Hide password in proxy URL for display."""
    if not url or ":" not in url:
        return url
    try:
        # http://user:pass@host:port  →  http://user:****@host:port
        scheme, rest = url.split("://", 1)
        if "@" in rest:
            creds, hostport = rest.rsplit("@", 1)
            user = creds.split(":")[0]
            return f"{scheme}://{user}:****@{hostport}"
    except Exception:
        pass
    return url


# ── Module-level convenience ───────────────────────────────────────────────────

_DEFAULT_FILE = Path(__file__).parent / "data" / "proxies_isp.txt"
_pool: ProxyPool | None = None


def get_pool(proxy_file: str | Path = _DEFAULT_FILE) -> ProxyPool:
    """Return (and cache) the module-level pool instance."""
    global _pool
    if _pool is None or Path(proxy_file) != _DEFAULT_FILE:
        _pool = ProxyPool(proxy_file)
    return _pool


# ── Exit IP helpers ────────────────────────────────────────────────────────────

def _next_ip_check_url() -> str:
    global _ip_url_idx
    with _ip_url_lock:
        url = _IP_CHECK_URLS[_ip_url_idx % len(_IP_CHECK_URLS)]
        _ip_url_idx += 1
        return url


def _get_exit_ip(proxy_url: str, timeout: int = 8) -> str | None:
    """
    Resolve the real exit IP of a proxy. Cycles through multiple check endpoints
    round-robin to avoid rate-limiting any one service.
    Returns None on any failure (dead proxy, timeout, non-routable).
    """
    url = _next_ip_check_url()
    try:
        import primp as _primp
        c = _primp.Client(proxy=proxy_url, verify=False,
                          follow_redirects=True, timeout=timeout)
        r = c.get(url)
        ip = r.text.strip()
        return ip if ip and "." in ip else None
    except Exception:
        return None


def _extract_session_id(proxy_url: str) -> str:
    """
    Extract SOAX sessionid from a proxy URL for use as an IP-cache key.
    Falls back to the full URL for non-SOAX proxies.
    """
    m = re.search(r"sessionid-([^-@:/?]+)-", proxy_url)
    return m.group(1) if m else proxy_url


# ── PersistentProxyPool ────────────────────────────────────────────────────────

class PersistentProxyPool:
    """
    High-concurrency proxy pool with cross-run persistence and exit-IP deduplication.

    Optimised for 200-500 concurrent workers:

    advance()
      In-memory cursor (no disk write per call). Flushed every CURSOR_FLUSH_EVERY
      advances and at process exit via atexit. Crash replays at most
      CURSOR_FLUSH_EVERY slots — acceptable since verify_and_claim() provides the
      real deduplication guarantee.

    verify_and_claim(proxy, account)
      1. Extracts session ID from the proxy URL.
      2. Checks ip_cache[session_id] — if already resolved, no HTTP request needed.
      3. If not cached: makes one HTTP request (cycles endpoints to avoid rate limits).
      4. Checks used_ips — if this exit IP was ever claimed, returns None.
      5. Otherwise marks IP used, caches session_id→IP, saves state, returns IP.
      Run this in an executor thread (~5-8 s for first check, ~0 ms for cached).

    State file: <proxy_file>.proxy_state.json
      cursor      — flushed cursor value
      ip_cache    — {session_id: exit_ip}  (permanent, grows across runs)
      used_ips    — {exit_ip: {account, at}}  (cleared by reset())
      daily_burns — {date: {proxy: reason}}   (pruned nightly)

    Reset:
      pool.reset()                 — clears used_ips + resets cursor to 0
      pool.reset(keep_cursor=True) — clears used_ips only
    """

    def __init__(self, proxy_file: str | Path):
        self._file       = Path(proxy_file)
        self._state_file = self._file.with_suffix(_STATE_SUFFIX)
        self._proxies: list[str] = []
        self._lock        = threading.Lock()
        self._state: dict = {}
        self._mem_cursor  = 0   # in-memory; flushed periodically
        self._load_proxies()
        self._load_state()
        atexit.register(self._flush_cursor)
        print(f"[proxy] PersistentProxyPool: {len(self._proxies)} proxies  "
              f"cursor={self._mem_cursor}  used_ips={self.used_count}  "
              f"cached_ips={len(self._state.get('ip_cache', {}))}")

    # ── Public API ─────────────────────────────────────────────────────────────

    def advance(self) -> str:
        """
        Return the next proxy (thread-safe). Cursor is kept in memory and flushed
        to disk every CURSOR_FLUSH_EVERY calls — no disk write on every advance.
        Two concurrent callers always get different proxies.
        """
        with self._lock:
            idx = self._mem_cursor % len(self._proxies)
            proxy = self._proxies[idx]
            self._mem_cursor += 1
            if self._mem_cursor % CURSOR_FLUSH_EVERY == 0:
                self._state["cursor"] = self._mem_cursor
                self._save_state()
            return proxy

    def verify_and_claim(self, proxy: str, account: str = "") -> str | None:
        """
        Resolve the proxy's real exit IP and claim it for `account`.
        Returns the IP string on success, None if the IP is already used or
        the proxy is dead/unreachable.

        Sticky proxies (SOAX): ip_cache maps session_id → exit_ip so already-
        resolved proxies skip the HTTP request entirely (~0 ms fast path).

        Rotating proxies (Webshare -rotate): ip_cache is bypassed — each TCP
        connection gets a fresh exit IP from the provider, so we always make a
        fresh HTTP check. used_ips deduplication still prevents two concurrent
        workers from landing on the same exit IP.

        Safe to call from 200-500 concurrent executor threads.
        """
        rotating   = self.is_rotating
        session_id = _extract_session_id(proxy)

        if not rotating:
            # Fast path: session ID already resolved — no HTTP request
            with self._lock:
                ip_cache = self._state.get("ip_cache", {})
                ip = ip_cache.get(session_id)
                if ip is not None:
                    used = self._state.get("used_ips", {})
                    if ip in used:
                        return None
                    used[ip] = {
                        "account": account,
                        "at":      datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
                    }
                    self._state["used_ips"] = used
                    self._save_state()
                    return ip

        # Slow path: HTTP request to discover actual exit IP
        ip = _get_exit_ip(proxy)
        if not ip:
            return None

        with self._lock:
            if not rotating:
                # Cache session_id → exit_ip for sticky proxies only
                ip_cache = self._state.setdefault("ip_cache", {})
                ip_cache[session_id] = ip

            used = self._state.setdefault("used_ips", {})
            if ip in used:
                if not rotating:
                    self._save_state()  # persist cache entry even on collision
                return None
            used[ip] = {
                "account": account,
                "at":      datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
            }
            self._save_state()
            return ip

    def burn_today(self, proxy: str, reason: str = "") -> None:
        """Mark a proxy burned for today (server-side block). Auto-cleared at midnight."""
        with self._lock:
            today = datetime.date.today().isoformat()
            burns = self._state.setdefault("daily_burns", {})
            daily = burns.setdefault(today, {})
            daily[proxy] = reason or "burned"
            self._state["daily_burns"] = {d: v for d, v in burns.items() if d >= today}
            self._save_state()
        print(f"[proxy] burn_today: {_redact(proxy)}  reason={reason or 'burned'}")

    def reset(self, keep_cursor: bool = False) -> None:
        """
        Clear used_ips so all proxies become available again.
        ip_cache is preserved (no need to re-check known IPs).
        keep_cursor=False: also reset cursor to 0.
        keep_cursor=True: cursor stays (continue past already-consumed proxies).
        """
        with self._lock:
            if not keep_cursor:
                self._mem_cursor = 0
                self._state["cursor"] = 0
            self._state["used_ips"] = {}
            self._save_state()
        action = "cursor kept" if keep_cursor else "cursor=0"
        print(f"[proxy] reset: used_ips cleared ({action})  "
              f"{len(self._proxies)} proxies available  "
              f"ip_cache preserved ({len(self._state.get('ip_cache', {}))} entries)")

    # ── Properties ─────────────────────────────────────────────────────────────

    @property
    def is_rotating(self) -> bool:
        """True for Webshare-style rotating proxies (username contains '-rotate').
        Rotating proxies hand out a fresh exit IP on every new TCP connection, so
        the ip_cache (which maps a stable session ID → exit IP) does not apply."""
        return any('-rotate' in p for p in self._proxies)

    @property
    def total(self) -> int:
        return len(self._proxies)

    @property
    def cursor(self) -> int:
        return self._mem_cursor

    @property
    def used_count(self) -> int:
        return len(self._state.get("used_ips", {}))

    def status(self) -> None:
        today_burns = len(self._state.get("daily_burns", {})
                         .get(datetime.date.today().isoformat(), {}))
        cached      = len(self._state.get("ip_cache", {}))
        used        = self.used_count
        print(f"[proxy] file             : {self._file.name}")
        print(f"[proxy] total            : {self.total}")
        print(f"[proxy] cursor           : {self._mem_cursor}")
        print(f"[proxy] used_ips         : {used}")
        print(f"[proxy] ip_cache entries : {cached}")
        print(f"[proxy] burns_today      : {today_burns}")
        print(f"[proxy] approx_remaining : {max(0, self.total - used - today_burns)}")

    # ── Internals ──────────────────────────────────────────────────────────────

    def _load_proxies(self) -> None:
        if not self._file.exists():
            raise FileNotFoundError(f"Proxy file not found: {self._file}")
        seen: set[str] = set()
        for line in self._file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            url = _normalise(line)
            if url not in seen:
                self._proxies.append(url)
                seen.add(url)
        if not self._proxies:
            raise ValueError(f"No proxies found in {self._file}")

    def _load_state(self) -> None:
        if self._state_file.exists():
            try:
                self._state = json.loads(
                    self._state_file.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                self._state = {}
        self._mem_cursor = self._state.get("cursor", 0)

    def _save_state(self) -> None:
        try:
            self._state_file.write_text(
                json.dumps(self._state, indent=2), encoding="utf-8")
        except OSError:
            pass

    def _flush_cursor(self) -> None:
        """Persist in-memory cursor to disk — called by atexit."""
        with self._lock:
            self._state["cursor"] = self._mem_cursor
            self._save_state()
