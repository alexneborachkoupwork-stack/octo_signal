"""
SlotSignalBus — resettable asyncio.Event + HTTP override server.

Replaces the single-shot event in SlotDetector with a resettable design
that survives multiple signal rounds (all-no-slot → reset → scouts resume → next round).

HTTP server on :8989 (same contract as SlotDetector._override_server):
  POST /signal  {}  or  {"date":"YYYY-MM-DD","period_id":"1"}
  GET  /status
"""

import asyncio
import json
import logging

import engine  # noqa: F401 — triggers __init__.py path setup

log = logging.getLogger("engine.signals")


class SlotSignalBus:
    def __init__(self, slot_manager):
        self._slot_manager = slot_manager
        self._event        = asyncio.Event()
        self._fired_count  = 0

    # ── Worker-facing API ──────────────────────────────────────────────────────

    async def wait(self) -> None:
        """Block until the next slot signal (used by real workers)."""
        await self._event.wait()

    def fire(self, slots: list[dict]) -> None:
        """
        Called by scout workers when poll_slots_once() returns data.
        Updates the slot pool and wakes all waiting real workers.
        Idempotent: if already set, only updates the pool.
        """
        added = self._slot_manager.update_pool(slots)
        if added > 0:
            log.info(f"[signal] {added} new slot(s) added to pool")
        if not self._event.is_set():
            self._fired_count += 1
            log.info(f"[signal] fired (round #{self._fired_count})  pool={self._slot_manager.stats()['pool']}")
        self._event.set()

    def reset(self) -> None:
        """
        Called by Manager after all real workers return no_slot.
        Clears the event so workers re-enter wait().
        Scout polling loops are unaffected — they keep running independently.
        """
        self._event.clear()
        log.info("[signal] reset — scouts continue, real workers re-enter wait")

    def fire_synthetic(self, date: str | None = None, period_id: str | None = None) -> None:
        """
        External signal (human via HTTP / Telegram).
        Optionally injects a specific slot into the pool first.
        Wakes all warmed real workers regardless of scout state.
        """
        if date and period_id:
            self._slot_manager.update_pool([{"date": date, "periods": [{"id": period_id}]}])
            log.info(f"[signal] synthetic slot injected: {date} period {period_id}")
        if not self._event.is_set():
            self._fired_count += 1
            log.info(f"[signal] synthetic signal fired (round #{self._fired_count})")
        self._event.set()

    @property
    def fired_count(self) -> int:
        return self._fired_count

    # ── HTTP override server ───────────────────────────────────────────────────

    async def serve_http(self, port: int = 8989) -> None:
        """
        Tiny aiohttp server on :port.
        POST /signal  body: {} or {"date":"...","period_id":"..."}
        GET  /status  returns JSON stats
        Runs forever until cancelled.
        """
        try:
            from aiohttp import web
        except ImportError:
            log.warning("[signal] aiohttp not installed — HTTP signal server disabled")
            await asyncio.sleep(3600 * 24)  # keep coroutine alive without exiting gather
            return

        async def handle_signal(request: web.Request) -> web.Response:
            try:
                body = await request.json()
            except Exception:
                body = {}
            date      = body.get("date")
            period_id = body.get("period_id")
            self.fire_synthetic(date, period_id)
            return web.json_response({
                "ok": True,
                "fired_count": self._fired_count,
                "pool": self._slot_manager.stats()["pool"],
            })

        async def handle_status(request: web.Request) -> web.Response:
            return web.json_response({
                "slot_manager":  self._slot_manager.stats(),
                "fired_count":   self._fired_count,
                "event_set":     self._event.is_set(),
            })

        async def handle_reset(request: web.Request) -> web.Response:
            was_set = self._event.is_set()
            self.reset()
            log.info("[signal] manual reset via HTTP")
            return web.json_response({"ok": True, "was_set": was_set})

        app = web.Application()
        app.router.add_post("/signal",       handle_signal)
        app.router.add_post("/signal/reset", handle_reset)
        app.router.add_get("/status",        handle_status)

        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "0.0.0.0", port)
        await site.start()
        log.info(f"[signal] HTTP server on :{port}  POST /signal  GET /status")

        try:
            while True:
                await asyncio.sleep(3600)
        finally:
            await runner.cleanup()
