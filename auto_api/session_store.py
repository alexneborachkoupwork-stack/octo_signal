"""
Cookie persistence for logged-in primp sessions.

Saves/loads {cookies, proxy} per account to auto_api/sessions/<username>.json.
Critical: Vistos_sid has Path=/VistosOnline/ — must probe with the full path URL.
"""

import json
import time
from pathlib import Path
from primp import Client
import session as sess

COOKIES_URL  = "https://pedidodevistos.mne.gov.pt/VistosOnline/"
_SESSIONS_DIR = Path(__file__).parent / "sessions"


def _path(username: str) -> Path:
    _SESSIONS_DIR.mkdir(exist_ok=True)
    return _SESSIONS_DIR / f"{username}.json"


def save(username: str, client: Client, proxy: str | None,
         checkpoint: str = "login", **meta) -> None:
    """Save cookies + optional metadata. checkpoint='login' or 'schedule_jsp'."""
    cookies = client.get_cookies(COOKIES_URL)
    data = {
        "username":   username,
        "proxy":      proxy,
        "cookies":    cookies,
        "saved_at":   time.time(),
        "checkpoint": checkpoint,
        **meta,
    }
    _path(username).write_text(json.dumps(data, indent=2), encoding="utf-8")


def load(username: str) -> tuple[Client, str | None] | None:
    """Load a session. Returns (client, proxy) or None if no saved session."""
    p = _path(username)
    if not p.exists():
        return None
    data = json.loads(p.read_text(encoding="utf-8"))
    proxy   = data.get("proxy")
    cookies = data.get("cookies", {})

    kwargs = {
        "impersonate":      sess.IMPERSONATE,
        "follow_redirects": True,
        "verify":           True,
    }
    if proxy:
        kwargs["proxy"] = proxy

    client = Client(**kwargs)
    if cookies:
        try:
            client.set_cookies(COOKIES_URL, cookies)
        except Exception:
            # Fallback: inject one by one if set_cookies signature differs
            for name, value in cookies.items():
                client.set_cookies(COOKIES_URL, {name: value})

    return client, proxy


def load_meta(username: str) -> dict:
    """Return full saved metadata dict (checkpoint, posto_id, posto_pdf, etc.), or {}."""
    p = _path(username)
    if not p.exists():
        return {}
    return json.loads(p.read_text(encoding="utf-8"))


def probe_session(client: Client) -> str:
    """
    Probe /VistosOnline/ and return a three-way status:
      'alive' — 200 + session indicator in body (Questionario / logout link)
      'dead'  — 302 redirect (Vistos_sid expired)
      'down'  — connection error, timeout, or 5xx (portal unreachable)
    """
    try:
        r = client.get(
            COOKIES_URL,
            headers={**sess.HEADERS_NAV, "Sec-Fetch-Site": "same-origin"},
            timeout=15,
            follow_redirects=False,
        )
        if r.status_code == 302:
            return "dead"
        if r.status_code >= 500:
            return "down"
        alive = r.status_code == 200 and (
            "Questionario" in r.text or "logout" in r.text.lower()
        )
        return "alive" if alive else "dead"
    except Exception:
        return "down"


def is_alive(client: Client) -> bool:
    """
    Probe /VistosOnline/ without following redirects.
    Alive: 200 + 'Questionario' in body.
    Dead: 302 redirect back to Authentication.jsp.
    """
    return probe_session(client) == "alive"


def delete(username: str) -> None:
    p = _path(username)
    if p.exists():
        p.unlink()
