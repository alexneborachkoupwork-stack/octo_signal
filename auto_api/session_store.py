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


def save(username: str, client: Client, proxy: str | None) -> None:
    cookies = client.get_cookies(COOKIES_URL)
    data = {
        "username":   username,
        "proxy":      proxy,
        "cookies":    cookies,
        "saved_at":   time.time(),
    }
    _path(username).write_text(json.dumps(data, indent=2), encoding="utf-8")


def load(username: str) -> tuple[Client, str | None] | None:
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
    # Inject saved cookies so the session is immediately usable
    for name, value in cookies.items():
        client.set_cookie(COOKIES_URL, name, value)

    return client, proxy


def is_alive(client: Client) -> bool:
    """
    Probe /VistosOnline/ without following redirects.
    Alive: 200 + 'Questionario' in body.
    Dead: 302 redirect back to Authentication.jsp.
    """
    try:
        r = client.get(
            COOKIES_URL,
            headers={**sess.HEADERS_NAV, "Sec-Fetch-Site": "same-origin"},
            timeout=15,
            follow_redirects=False,
        )
        if r.status_code == 302:
            return False
        return r.status_code == 200 and ("Questionario" in r.text or "logout" in r.text.lower())
    except Exception:
        return False


def delete(username: str) -> None:
    p = _path(username)
    if p.exists():
        p.unlink()
