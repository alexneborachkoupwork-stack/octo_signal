"""Temporary email — switchable between mail.tm and a self-hosted Cloudflare Worker inbox.

Provider is selected via the EMAIL_PROVIDER environment variable (or config.py):
  EMAIL_PROVIDER=mailtm       (default) — mail.tm public API
  EMAIL_PROVIDER=cloudflare   — Cloudflare Worker inbox (requires CF_* vars)

Cloudflare env vars:
  CF_MAIL_DOMAIN   — e.g. "mail.yourdomain.com"
  CF_WORKER_URL    — e.g. "https://octo-mail-recv.yourname.workers.dev"
  CF_WORKER_SECRET — shared secret (must match wrangler secret API_SECRET)

Both backends expose the same interface:
  create_account()         -> {email, password, jwt}
  wait_for_message(jwt)    -> dict | None
  extract_token(msg)       -> str | None
"""
import asyncio
import os
import random
import re
import string
import time

import httpx

# ---------------------------------------------------------------------------
# Shared
# ---------------------------------------------------------------------------

_TIMEOUT = httpx.Timeout(15.0)


def _hex_to_uuid(h: str) -> str:
    return f"{h[:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:32]}"


def extract_token(msg: dict) -> str | None:
    """Extract verification token from email body, normalising to UUID format."""
    body = msg.get("text", "") or msg.get("html", "")
    patterns = [
        r"\b([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\b",
        r"\b([0-9a-f]{32})\b",
        r"[?&]token=([A-Za-z0-9\-]{6,})",
        r"token[=:\s]+([A-Za-z0-9\-]{6,})",
        r"\b([0-9]{4,8})\b",
    ]
    for pat in patterns:
        m = re.search(pat, body, re.IGNORECASE)
        if m:
            token = m.group(1)
            if re.fullmatch(r"[0-9a-f]{32}", token, re.IGNORECASE):
                token = _hex_to_uuid(token)
            return token
    return None


# ---------------------------------------------------------------------------
# mail.tm backend
# ---------------------------------------------------------------------------

_MAILTM = "https://api.mail.tm"


async def _mt_domain() -> str:
    async with httpx.AsyncClient(timeout=_TIMEOUT) as c:
        r = await c.get(f"{_MAILTM}/domains")
        r.raise_for_status()
        return r.json()["hydra:member"][0]["domain"]


async def _mt_create_account() -> dict:
    domain = await _mt_domain()
    local  = "".join(random.choices(string.ascii_lowercase + string.digits, k=10))
    email  = f"{local}@{domain}"
    pwd    = "".join(random.choices(string.ascii_letters + string.digits, k=18))

    async with httpx.AsyncClient(timeout=_TIMEOUT) as c:
        r = await c.post(f"{_MAILTM}/accounts", json={"address": email, "password": pwd})
        r.raise_for_status()
        r = await c.post(f"{_MAILTM}/token", json={"address": email, "password": pwd})
        r.raise_for_status()
        jwt = r.json()["token"]

    return {"email": email, "password": pwd, "jwt": jwt}


async def _mt_wait_for_message(jwt: str, timeout_s: int = 120, poll_s: float = 5.0) -> dict | None:
    headers  = {"Authorization": f"Bearer {jwt}"}
    deadline = time.monotonic() + timeout_s

    async with httpx.AsyncClient(timeout=_TIMEOUT) as c:
        while time.monotonic() < deadline:
            r = await c.get(f"{_MAILTM}/messages", headers=headers)
            r.raise_for_status()
            items = r.json()["hydra:member"]
            if items:
                r2 = await c.get(f"{_MAILTM}/messages/{items[0]['id']}", headers=headers)
                r2.raise_for_status()
                return r2.json()
            await asyncio.sleep(poll_s)

    return None


# ---------------------------------------------------------------------------
# Cloudflare Worker backend
# ---------------------------------------------------------------------------

def _cf_config() -> tuple[str, str, str]:
    domain = os.environ.get("CF_MAIL_DOMAIN", "")
    url    = os.environ.get("CF_WORKER_URL", "").rstrip("/")
    secret = os.environ.get("CF_WORKER_SECRET", "")
    if not (domain and url and secret):
        raise RuntimeError(
            "Cloudflare email backend requires CF_MAIL_DOMAIN, CF_WORKER_URL, "
            "and CF_WORKER_SECRET environment variables."
        )
    return domain, url, secret


async def _cf_create_account() -> dict:
    domain, _, _ = _cf_config()
    local = "".join(random.choices(string.ascii_lowercase + string.digits, k=10))
    email = f"{local}@{domain}"
    # jwt field holds the local part — used as the inbox polling key.
    return {"email": email, "password": "", "jwt": local}


async def _cf_wait_for_message(local: str, timeout_s: int = 120, poll_s: float = 5.0) -> dict | None:
    _, url, secret = _cf_config()
    headers  = {"X-Secret": secret}
    deadline = time.monotonic() + timeout_s

    async with httpx.AsyncClient(timeout=_TIMEOUT) as c:
        while time.monotonic() < deadline:
            r = await c.get(f"{url}/{local}", headers=headers)
            r.raise_for_status()
            messages = r.json()
            if messages:
                return {"text": messages[0]["text"], "html": ""}
            await asyncio.sleep(poll_s)

    return None


# ---------------------------------------------------------------------------
# Public facade — delegates based on EMAIL_PROVIDER
# ---------------------------------------------------------------------------

def _provider() -> str:
    try:
        from config import EMAIL_PROVIDER  # type: ignore
        return EMAIL_PROVIDER
    except (ImportError, AttributeError):
        pass
    return os.environ.get("EMAIL_PROVIDER", "mailtm")


async def create_account() -> dict:
    """Create a disposable inbox. Returns {email, password, jwt}."""
    if _provider() == "cloudflare":
        return await _cf_create_account()
    return await _mt_create_account()


async def wait_for_message(jwt: str, timeout_s: int = 120, poll_s: float = 5.0) -> dict | None:
    """Poll inbox until a message arrives or timeout. Returns {text, html} or None."""
    if _provider() == "cloudflare":
        return await _cf_wait_for_message(jwt, timeout_s, poll_s)
    return await _mt_wait_for_message(jwt, timeout_s, poll_s)
