"""
HTTP CONNECT probe: tests proxies against the portal.
Phase 1: raw CONNECT (TCP tunnel)
Phase 2: TLS handshake through the tunnel (actual HTTPS reachability)
Phase 3: Chromium-style CONNECT headers (matches what Playwright sends)
"""
import ssl
import socket
import base64
import concurrent.futures
from pathlib import Path
from urllib.parse import urlparse

TARGET_HOST = "pedidodevistos.mne.gov.pt"
TARGET_PORT = 443
TIMEOUT = 12

DATA_DIR = Path(__file__).parent.parent / "app" / "core" / "data"

GEO_FILES = {
    "FR":  DATA_DIR / "proxies_webshare.txt",
    "DE":  DATA_DIR / "proxies_webshare_DE.txt",
    "GB":  DATA_DIR / "proxies_webshare_GB.txt",
    "NL":  DATA_DIR / "proxies_webshare_NL.txt",
    "IT":  DATA_DIR / "proxies_webshare_IT.txt",
    "ES":  DATA_DIR / "proxies_webshare_ES.txt",
    "PT":  DATA_DIR / "proxies_webshare_PT.txt",
    "SE":  DATA_DIR / "proxies_webshare_SE.txt",
    "PL":  DATA_DIR / "proxies_webshare_PL.txt",
    "CZ":  DATA_DIR / "proxies_webshare_CZ.txt",
    "RO":  DATA_DIR / "proxies_webshare_RO.txt",
}


def parse_proxy(line: str):
    line = line.strip()
    if not line or line.startswith("#"):
        return None
    p = urlparse(line)
    return p.hostname, p.port or 80, p.username or "", p.password or ""


def _connect_tunnel(ph, pp, pu, pw, chromium_style=False) -> socket.socket:
    """Open TCP→proxy→CONNECT tunnel. Returns connected raw socket or raises."""
    s = socket.create_connection((ph, pp), timeout=TIMEOUT)
    creds = base64.b64encode(f"{pu}:{pw}".encode()).decode() if pu else ""
    if chromium_style:
        # Mimic Chromium/Playwright CONNECT headers
        auth_hdr = f"Proxy-Authorization: Basic {creds}\r\n" if creds else ""
        req = (
            f"CONNECT {TARGET_HOST}:{TARGET_PORT} HTTP/1.1\r\n"
            f"Host: {TARGET_HOST}:{TARGET_PORT}\r\n"
            f"Proxy-Connection: keep-alive\r\n"
            f"User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            f"AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36\r\n"
            f"{auth_hdr}"
            "\r\n"
        )
    else:
        auth_hdr = f"Proxy-Authorization: Basic {creds}\r\n" if creds else ""
        req = (
            f"CONNECT {TARGET_HOST}:{TARGET_PORT} HTTP/1.1\r\n"
            f"Host: {TARGET_HOST}:{TARGET_PORT}\r\n"
            f"{auth_hdr}"
            "\r\n"
        )
    s.sendall(req.encode())
    resp = b""
    while b"\r\n\r\n" not in resp:
        chunk = s.recv(4096)
        if not chunk:
            break
        resp += chunk
    first_line = resp.decode(errors="replace").split("\r\n")[0]
    status = first_line.split(" ")[1] if len(first_line.split(" ")) > 1 else "?"
    if status != "200":
        s.close()
        raise ConnectionError(f"CONNECT rejected: {first_line.strip()}")
    return s


def test_proxy(geo: str, proxy_line: str) -> str:
    parsed = parse_proxy(proxy_line)
    if not parsed:
        return f"{geo:4s} SKIP"
    ph, pp, pu, pw = parsed

    # --- Phase 1: plain CONNECT ---
    try:
        s = _connect_tunnel(ph, pp, pu, pw, chromium_style=False)
        s.close()
        plain_ok = True
    except Exception as e:
        return f"{geo:4s} FAIL plain CONNECT: {e}"

    # --- Phase 2: TLS handshake through tunnel ---
    tls_ok = False
    tls_note = ""
    try:
        s = _connect_tunnel(ph, pp, pu, pw, chromium_style=False)
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        tls_s = ctx.wrap_socket(s, server_hostname=TARGET_HOST)
        tls_s.close()
        tls_ok = True
    except Exception as e:
        tls_note = f"TLS fail: {e}"

    # --- Phase 3: Chromium-style CONNECT ---
    chrome_ok = False
    chrome_note = ""
    try:
        s = _connect_tunnel(ph, pp, pu, pw, chromium_style=True)
        s.close()
        chrome_ok = True
    except Exception as e:
        chrome_note = f"Chrome CONNECT fail: {e}"

    parts = []
    parts.append("plain=OK")
    parts.append("TLS=" + ("OK" if tls_ok else f"FAIL({tls_note})"))
    parts.append("chrome=" + ("OK" if chrome_ok else f"FAIL({chrome_note})"))
    status = "OK" if (tls_ok and chrome_ok) else ("PARTIAL" if plain_ok else "FAIL")
    return f"{geo:4s} {status:8s} | {' | '.join(parts)}"


def main():
    tasks = {}
    for geo, fpath in GEO_FILES.items():
        if not fpath.exists():
            continue
        lines = [l for l in fpath.read_text(encoding="utf-8-sig").splitlines()
                 if l.strip() and not l.startswith("#")]
        if lines:
            tasks[geo] = lines[0]

    print(f"Testing {len(tasks)} geos — {TARGET_HOST}:{TARGET_PORT}\n")
    print(f"{'GEO':4s} {'RESULT':8s} | plain CONNECT | TLS handshake | Chromium CONNECT")
    print("-" * 75)
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(tasks)) as ex:
        futures = {ex.submit(test_proxy, geo, line): geo for geo, line in tasks.items()}
        results = []
        for fut in concurrent.futures.as_completed(futures):
            results.append(fut.result())
        for r in sorted(results):
            print(r)


if __name__ == "__main__":
    main()
