"""
Thin async wrapper around the Octo Browser local API (http://127.0.0.1:58888).
No auth token is required for the local API.
"""
import httpx
from dataclasses import dataclass
from typing import Optional

from config import OCTO_LOCAL_API


@dataclass
class ProfileSession:
    uuid: str
    ws_endpoint: str
    debug_port: int
    headless: bool


class OctoClient:
    def __init__(self, base_url: str = OCTO_LOCAL_API, timeout: float = 120.0):
        self._client = httpx.AsyncClient(base_url=base_url, timeout=timeout)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        await self._client.aclose()

    async def list_active_profiles(self) -> list[dict]:
        """Return all currently running Octo profiles."""
        resp = await self._client.get("/api/profiles/active")
        resp.raise_for_status()
        return resp.json()

    async def start_profile(
        self,
        uuid: str,
        headless: bool = False,
        only_local: bool = True,
        timeout: int = 120,
    ) -> ProfileSession:
        """
        Ask Octo to launch the profile and return CDP connection details.
        If the profile is already running, Octo returns the existing session.
        """
        resp = await self._client.post(
            "/api/profiles/start",
            json={
                "uuid": uuid,
                "headless": headless,
                "debug_port": True,
                "only_local": only_local,
                "timeout": timeout,
            },
        )
        resp.raise_for_status()
        data = resp.json()
        return ProfileSession(
            uuid=data["uuid"],
            ws_endpoint=data["ws_endpoint"],
            debug_port=data["debug_port"],
            headless=data.get("headless", headless),
        )

    async def stop_profile(self, uuid: str) -> None:
        resp = await self._client.post("/api/profiles/stop", json={"uuid": uuid})
        resp.raise_for_status()

    async def get_active_session(self, uuid: str) -> Optional[ProfileSession]:
        """Return the session info if the profile is already running, else None."""
        profiles = await self.list_active_profiles()
        for p in profiles:
            if p.get("uuid") == uuid:
                return ProfileSession(
                    uuid=p["uuid"],
                    ws_endpoint=p["ws_endpoint"],
                    debug_port=p["debug_port"],
                    headless=p.get("headless", False),
                )
        return None
