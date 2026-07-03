"""
Portal health checker — distinguishes 'proxy blocked' from 'portal server down/maintenance'.

Usage:
    from portal_health import portal_up, check_portal_now, start_portal_monitor

    await start_portal_monitor()   # call once from manager at startup
    if not portal_up():
        ...  # server is down or in maintenance, don't burn proxies

Direct probe via httpx (no proxy). Treats both connection failure AND maintenance
page responses as "down" — workers should pause in both cases.
"""

import asyncio
import logging
import time

import httpx

log = logging.getLogger("app.portal_health")

PORTAL_URL   = "https://pedidodevistos.mne.gov.pt/VistosOnline/"
CHECK_INTERVAL = 60        # seconds between background checks
PROBE_TIMEOUT  = 15        # seconds for each HTTP probe
DOWN_LOG_EVERY = 300       # log "still down" no more than once per 5 min

# Strings in the response body that indicate the portal is in maintenance mode.
_MAINTENANCE_MARKERS = (
    "serviço em manutenção",
    "manutenção",
    "em manutenção",
    "tente novamente mais tarde",
    "try again later",
    "service unavailable",
    "temporarily unavailable",
)

_is_up: bool       = True   # optimistic start
_last_check: float = 0.0
_last_down_log: float = 0.0
_down_reason: str  = ""
_monitor_task: asyncio.Task | None = None
_default_proxy: str | None = None   # set once at startup via set_proxy()


def set_proxy(proxy_url: str) -> None:
    """Set the proxy used by the background monitor and bare check_portal_now() calls."""
    global _default_proxy
    _default_proxy = proxy_url


def portal_up() -> bool:
    """Return cached portal health state (non-blocking)."""
    return _is_up


def check_portal_now(proxy: str | None = None) -> bool:
    """Synchronous probe via proxy. Returns True if portal is up and not in maintenance."""
    global _is_up, _last_check, _last_down_log, _down_reason
    _proxy = proxy or _default_proxy
    try:
        with httpx.Client(timeout=PROBE_TIMEOUT, follow_redirects=True,
                          proxy=_proxy if _proxy else None) as client:
            r = client.get(PORTAL_URL)
        # 403 means WAF blocked our raw probe — portal is up, just rejecting bot requests.
        # Only 5xx or connection errors mean truly down.
        if r.status_code >= 500:
            _is_up = False
            _down_reason = f"HTTP {r.status_code}"
            _last_check = time.time()
            now = time.time()
            if now - _last_down_log >= DOWN_LOG_EVERY:
                log.warning(f"[portal_health] portal DOWN: {_down_reason}")
                _last_down_log = now
            return False
        body_lower = r.text.lower()
        for marker in _MAINTENANCE_MARKERS:
            if marker in body_lower:
                _is_up = False
                _down_reason = f"maintenance ({marker!r} in response)"
                _last_check = time.time()
                now = time.time()
                if now - _last_down_log >= DOWN_LOG_EVERY:
                    log.warning(f"[portal_health] portal MAINTENANCE: {_down_reason}")
                    _last_down_log = now
                return False
        _is_up = True
        _down_reason = ""
        _last_check = time.time()
        return True
    except Exception as e:
        _is_up = False
        _down_reason = str(e)
        _last_check = time.time()
        now = time.time()
        if now - _last_down_log >= DOWN_LOG_EVERY:
            log.warning(f"[portal_health] portal DOWN: {e}")
            _last_down_log = now
        return False


async def _monitor_loop() -> None:
    global _is_up, _last_check, _last_down_log
    loop = asyncio.get_event_loop()
    while True:
        await asyncio.sleep(CHECK_INTERVAL)
        try:
            was_up = _is_up
            result = await loop.run_in_executor(None, check_portal_now)
            if result and not was_up:
                log.info("[portal_health] portal is back UP")
            elif not result:
                now = time.time()
                if now - _last_down_log >= DOWN_LOG_EVERY:
                    log.warning("[portal_health] portal still DOWN")
                    _last_down_log = now
        except Exception as e:
            log.debug(f"[portal_health] monitor error: {e}")


async def start_portal_monitor() -> None:
    """Start the background health monitor (idempotent — safe to call multiple times)."""
    global _monitor_task
    if _monitor_task is None or _monitor_task.done():
        _monitor_task = asyncio.create_task(_monitor_loop())
        log.info("[portal_health] background monitor started")
    await asyncio.get_event_loop().run_in_executor(None, check_portal_now)
