"""
IspProxyPool — static ISP proxy pool with thread-safe acquisition.
Each proxy is handed out once; when exhausted, callers fall back to SOAX.
"""
import threading
from typing import Callable


def _normalise(line: str) -> str:
    line = line.strip()
    if not line or line.startswith("#"):
        return ""
    if not line.startswith(("http://", "https://", "socks5://", "socks4://")):
        line = f"http://{line}"
    return line


class IspProxyPool:
    def __init__(self, proxy_file: str):
        with open(proxy_file) as f:
            self._available = [n for line in f if (n := _normalise(line))]
        self._lock = threading.Lock()
        self._total = len(self._available)

    def acquire(self) -> str | None:
        with self._lock:
            if not self._available:
                return None
            return self._available.pop(0)

    def release(self, url: str) -> None:
        """Return a proxy back to the pool (e.g. after a non-fatal failure)."""
        with self._lock:
            self._available.append(url)

    @property
    def is_exhausted(self) -> bool:
        with self._lock:
            return not self._available

    @property
    def size_available(self) -> int:
        with self._lock:
            return len(self._available)

    @property
    def size_total(self) -> int:
        return self._total


class IspFirstRequester:
    """
    Per-worker proxy requester: stick to one ISP proxy for ISP_STICK login attempts,
    then rotate to the next ISP proxy. Falls back to soax_advance() when ISP pool
    is exhausted. One instance per real worker — not shared.
    """
    ISP_STICK = 3  # login attempts before rotating to next ISP proxy

    def __init__(self, isp_pool: IspProxyPool, soax_advance: Callable[[], str]):
        self._isp_pool = isp_pool
        self._soax_advance = soax_advance
        self._current_isp: str | None = None
        self._tries = 0

    def __call__(self) -> str:
        # Already on SOAX (ISP was fully exhausted when this worker started)
        if self._current_isp is None and self._isp_pool.is_exhausted:
            return self._soax_advance()

        # Acquire first ISP proxy for this worker
        if self._current_isp is None:
            self._current_isp = self._isp_pool.acquire()
            self._tries = 0
            if self._current_isp is None:
                return self._soax_advance()

        self._tries += 1
        if self._tries > self.ISP_STICK:
            # Rotate to next ISP proxy
            self._current_isp = self._isp_pool.acquire()
            self._tries = 1
            if self._current_isp is None:
                return self._soax_advance()

        return self._current_isp

    def mark_failed(self) -> None:
        """Call when the current ISP proxy has hard-failed — rotate immediately."""
        self._current_isp = None
        self._tries = 0
