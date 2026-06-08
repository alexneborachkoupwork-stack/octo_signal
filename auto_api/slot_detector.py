"""
Rotating slot detector + human override server.

One detector session polls for slots every POLL_INTERVAL seconds.
If the active detector dies, another session is rotated in automatically.
Human override: POST http://localhost:8989/signal with JSON body to inject
slots directly (always takes priority, no guards).

Usage (from batch_warmup.py):
    detector = SlotDetector(slot_manager, coordinator)
    await detector.start(posto_id="5086")
    slots = await detector.wait()          # all workers await this
"""

import asyncio
import json
import logging
import os
import time
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from slot_manager import SlotManager

def _env_int(key: str, default: int) -> int:
    """Read an integer from .env file, falling back to default."""
    env_file = Path(__file__).parent / ".env"
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith(key + "="):
                try:
                    return int(line.split("=", 1)[1].strip())
                except ValueError:
                    pass
    return int(os.environ.get(key, default))

OVERRIDE_PORT   = _env_int("SLOT_OVERRIDE_PORT", 8989)
POLL_INTERVAL   = _env_int("SLOT_POLL_INTERVAL", 30)
CAPTCHA_ACTION  = "SCHEDULE_EVISA"

log = logging.getLogger("slot_detector")


class SlotDetector:
    def __init__(self, slot_manager: "SlotManager", coordinator):
        self._sm          = slot_manager
        self._coord       = coordinator   # has .sessions: dict[str, WorkerState]
        self._posto_id    = ""
        self._event       = asyncio.Event()
        self._tasks: list[asyncio.Task] = []

    async def start(self, posto_id: str) -> None:
        self._posto_id = posto_id
        self._tasks.append(asyncio.create_task(self._poll_loop(),    name="slot-poll"))
        self._tasks.append(asyncio.create_task(self._override_server(), name="slot-override"))

    async def wait(self) -> None:
        """Suspend until slots are detected (either by poll or human override)."""
        await self._event.wait()

    def signal_slots(self, slots: list[dict]) -> None:
        """Called from poll or human override — fires the event for all waiters."""
        added = self._sm.update_pool(slots)
        log.info(f"[detector] signal_slots: {added} new slots added  pool={self._sm.stats()}")
        self._event.set()

    async def _poll_loop(self) -> None:
        while not self._event.is_set():
            session = self._coord.pick_detector_session()
            if session is None:
                log.warning("[detector] no detector session available, waiting...")
                await asyncio.sleep(POLL_INTERVAL)
                continue

            try:
                slots = await session.poll_slots_once(self._posto_id)
                if slots:
                    log.info(f"[detector] found {len(slots)} slot entries via {session.username}")
                    self.signal_slots(slots)
                    return
                log.debug(f"[detector] no slots yet (session={session.username})")
            except Exception as e:
                log.warning(f"[detector] poll failed ({session.username}): {e}")
                self._coord.mark_detector_failed(session.username)

            await asyncio.sleep(POLL_INTERVAL)

    async def _override_server(self) -> None:
        """
        Tiny aiohttp server on :8989.
        POST /signal  {"posto_id":"5086","date":"2026-07-01","period_id":"1"}
          → injects slot immediately, fires event
        POST /status
          → returns warmup progress JSON
        """
        try:
            from aiohttp import web
        except ImportError:
            log.warning("[detector] aiohttp not installed — human override server disabled")
            return

        async def _handle_signal(req):
            try:
                body = await req.json()
            except Exception:
                return web.Response(status=400, text="invalid JSON")

            date      = body.get("date", "")
            period_id = str(body.get("period_id", ""))
            if not date or not period_id:
                return web.Response(status=400, text="date and period_id required")

            slots = [{"date": date, "periods": [{"id": period_id}]}]
            self.signal_slots(slots)
            log.info(f"[detector] human override: date={date} period_id={period_id}")
            return web.Response(text=json.dumps({"ok": True, "date": date,
                                                 "period_id": period_id}),
                                content_type="application/json")

        async def _handle_status(req):
            stats = self._sm.stats()
            warmup = self._coord.warmup_stats() if hasattr(self._coord, "warmup_stats") else {}
            payload = {"slot_manager": stats, "warmup": warmup,
                       "detected": self._event.is_set()}
            return web.Response(text=json.dumps(payload), content_type="application/json")

        app = web.Application()
        app.router.add_post("/signal", _handle_signal)
        app.router.add_post("/status", _handle_status)

        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "0.0.0.0", OVERRIDE_PORT)
        await site.start()
        log.info(f"[detector] override server listening on :{OVERRIDE_PORT}")

        # Keep running until cancelled
        try:
            while True:
                await asyncio.sleep(3600)
        except asyncio.CancelledError:
            await runner.cleanup()

    def stop(self) -> None:
        for t in self._tasks:
            t.cancel()
