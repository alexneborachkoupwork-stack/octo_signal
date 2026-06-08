"""
Proxy pool manager.
Loads proxies from a text file (one per line, format: user:pass@host:port).
Supports multiple rotation modes and persists state between runs.

Modes:
  recent   — reuse the last proxy that succeeded (default, avoids unnecessary rotation)
  rotate   — round-robin through all proxies, advancing one step each call
  random   — pick a random proxy each call
  first    — always use the first proxy in the file (useful for debugging)

State is persisted to .proxy_state.json next to the proxies file so the
"recent" mode survives process restarts.
"""

import datetime
import json
import random as _random
import threading
from pathlib import Path

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
    """Add http:// scheme if missing."""
    if line.startswith("http://") or line.startswith("https://"):
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
