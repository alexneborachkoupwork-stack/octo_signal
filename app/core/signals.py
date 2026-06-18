"""
SlotSignalBus — resettable asyncio.Event + HTTP override server.

Replaces the single-shot event in SlotDetector with a resettable design
that survives multiple signal rounds (all-no-slot → reset → scouts resume → next round).

The signal carries NO slot data. A scout's (or a human's) only job is to say
"something is available, go check" — each waking real worker does its own fresh
/slots POST (with its own fresh CAPTCHA token) inside apply_book() when it wakes,
since that's the only way to get a non-stale, CAPTCHA-gated answer anyway. Carrying
slot data through the signal would just be a second, redundant, potentially-stale
data path alongside the one that already has to exist.

HTTP server on :8989:
  POST /signal        body ignored — pure wake-up trigger
  POST /signal/reset
  GET  /status
"""

import asyncio
import logging

log = logging.getLogger("app.core.signals")


class SlotSignalBus:
    def __init__(self, slot_manager):
        self._slot_manager = slot_manager
        self._event        = asyncio.Event()
        self._fired_count  = 0

    # ── Worker-facing API ──────────────────────────────────────────────────────

    async def wait(self) -> None:
        """Block until the next slot signal (used by real workers)."""
        await self._event.wait()

    def fire(self) -> None:
        """
        Called by scout workers when poll_slots_once() finds availability (truthy
        result) — NOT given the slots themselves, just the fact that some exist.
        Wakes all waiting real workers. Idempotent: a second fire() while already
        set is a no-op besides the (suppressed) count.
        """
        if not self._event.is_set():
            self._fired_count += 1
            log.info(f"[signal] fired (round #{self._fired_count})")
        self._event.set()

    def reset(self) -> None:
        """
        Called by Manager after all real workers return no_slot.
        Clears the event so workers re-enter wait().
        Scout polling loops are unaffected — they keep running independently.
        """
        self._event.clear()
        log.info("[signal] reset — scouts continue, real workers re-enter wait")

    def fire_synthetic(self) -> None:
        """
        External signal (human via HTTP / Telegram / a separate app.scout process).
        Pure wake-up — wakes all warmed real workers regardless of scout state.
        Each worker fetches its own fresh /slots data when it wakes; this call
        carries no slot data because there is none to carry.
        """
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
        POST /signal  body ignored — pure wake-up trigger
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
            self.fire_synthetic()
            return web.json_response({
                "ok": True,
                "fired_count": self._fired_count,
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
