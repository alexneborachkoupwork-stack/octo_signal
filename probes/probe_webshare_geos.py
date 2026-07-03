"""
Generate and probe Webshare proxy strings for every country code Webshare offers.
Tests N proxies per geo (default 50). Reports:
  - GATEWAY_BLOCKED  → Webshare 502 (geo not routable through gateway)
  - NOT_IN_PLAN      → Webshare 407 or 403 (account doesn't have this geo)
  - PORTAL_BLOCKED   → tunnel OK but portal IP-bans the exit IP
  - OK               → tunnel OK + portal serves login form

Writes any OK proxies to app/core/data/proxies_working.txt.
"""
import ssl
import socket
import base64
import concurrent.futures
import sys
import random
import threading
import argparse
from collections import defaultdict
from pathlib import Path
from urllib.parse import urlparse

TARGET_HOST = "pedidodevistos.mne.gov.pt"
TARGET_PORT = 443
AUTH_PATH   = "/VistosOnline/Authentication.jsp"
TIMEOUT     = 10

DATA_DIR = Path(__file__).parent.parent / "app" / "core" / "data"
OUT_FILE = DATA_DIR / "proxies_working.txt"

# Webshare residential countries (ISO 3166-1 alpha-2)
# Drawn from Webshare's documented country list; skipping ones we know are 502
KNOWN_GATEWAY_BLOCKED = {"DE", "FR", "NL", "SE", "PL"}  # confirmed 502 in prior runs
ALREADY_TESTED = {"GB", "RO", "ES", "IT", "PT", "CZ", "BE", "BG", "RS", "DO"}  # all blocked

ALL_GEOS = [
    # Europe (untested)
    "AT", "CH", "NO", "DK", "FI", "IE", "GR", "HR", "HU", "SK",
    "SI", "LT", "LV", "EE", "LU", "MT", "CY", "IS", "AL", "BA",
    "ME", "MK", "MD", "UA", "AM", "GE", "AZ",
    # Asia-Pacific
    "IN", "JP", "KR", "SG", "HK", "TW", "PH", "MY", "ID", "TH",
    "VN", "BD", "PK", "LK", "NP", "MM", "KH", "LA", "MN",
    "AU", "NZ",
    # Americas
    "US", "CA", "MX", "BR", "AR", "CO", "CL", "PE", "VE", "EC",
    "BO", "PY", "UY", "CR", "GT", "HN", "SV", "NI", "PA", "JM",
    "TT", "PR", "CU",
    # Africa / Middle East
    "ZA", "NG", "KE", "EG", "MA", "TN", "GH", "ET", "TZ", "SN",
    "TR", "IL", "SA", "AE", "KW", "QA", "BH", "OM", "JO", "IQ",
    "KZ", "UZ", "TM", "KG", "TJ",
    # Already tested (include in full scan if --include-tested)
    # "GB","RO","ES","IT","PT","CZ","BE","BG","RS","DO"
]

_stop = threading.Event()
_ok_lock = threading.Lock()
_ok_found = []


def probe(geo: str, idx: int, creds_b64: str, user: str, pwd: str) -> tuple[str, int, str]:
    """Returns (geo, idx, result-tag)."""
    if _stop.is_set():
        return geo, idx, "SKIP"
    line = f"http://{user}:{pwd}@p.webshare.io:80"
    try:
        s = socket.create_connection(("p.webshare.io", 80), timeout=TIMEOUT)
        req = (
            f"CONNECT {TARGET_HOST}:{TARGET_PORT} HTTP/1.1\r\n"
            f"Host: {TARGET_HOST}:{TARGET_PORT}\r\n"
            f"Proxy-Authorization: Basic {creds_b64}\r\n"
            "\r\n"
        )
        s.sendall(req.encode())
        resp = b""
        while b"\r\n\r\n" not in resp:
            chunk = s.recv(4096)
            if not chunk:
                break
            resp += chunk
        first = resp.decode(errors="replace").split("\r\n")[0]
        parts = first.split()
        status = parts[1] if len(parts) > 1 else "0"

        if status == "502":
            s.close()
            return geo, idx, "GATEWAY_BLOCKED"
        if status in ("407", "403", "402", "401"):
            s.close()
            return geo, idx, f"NOT_IN_PLAN({status})"
        if status != "200":
            s.close()
            return geo, idx, f"TUNNEL_FAIL({first.strip()[:60]})"

        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        ts = ctx.wrap_socket(s, server_hostname=TARGET_HOST)

        get_req = (
            f"GET {AUTH_PATH} HTTP/1.1\r\n"
            f"Host: {TARGET_HOST}\r\n"
            f"User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36\r\n"
            f"Accept: text/html\r\n"
            f"Connection: close\r\n"
            "\r\n"
        )
        ts.sendall(get_req.encode())
        html = b""
        while True:
            chunk = ts.recv(8192)
            if not chunk:
                break
            html += chunk
            if len(html) > 32768:
                break
        ts.close()

        body = html.decode(errors="replace")
        if "Não foi possível processar o pedido" in body or "Request could not be processed" in body:
            return geo, idx, "PORTAL_BLOCKED"
        if 'name="username"' in body or 'type="password"' in body or "Authentication" in body[:2000]:
            return geo, idx, "OK"

        snippet = body[body.find("\r\n\r\n")+4:body.find("\r\n\r\n")+120].strip()
        return geo, idx, f"UNKNOWN({snippet[:60]!r})"

    except Exception as e:
        return geo, idx, f"ERR({type(e).__name__})"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--per-geo", type=int, default=50,
                    help="Proxy indices to test per geo (1..N)")
    ap.add_argument("--workers", type=int, default=200)
    ap.add_argument("--stop-after", type=int, default=10,
                    help="Stop after finding this many OK proxies (0=unlimited)")
    ap.add_argument("--include-tested", action="store_true",
                    help="Also re-test geos from the previous 600-proxy sweep")
    ap.add_argument("--username-prefix", default="Mylist1234",
                    help="Username prefix before -{GEO}-{N}")
    ap.add_argument("--password", default="Saulo12345")
    args = ap.parse_args()

    geos = list(ALL_GEOS)
    if args.include_tested:
        geos += sorted(ALREADY_TESTED)
    # Remove known gateway-blocked geos to save time
    geos = [g for g in geos if g not in KNOWN_GATEWAY_BLOCKED]

    # Build task list: for each geo test indices 1..per_geo (shuffled within geo)
    tasks = []
    for geo in geos:
        idxs = list(range(1, args.per_geo + 1))
        random.shuffle(idxs)
        for idx in idxs:
            user = f"{args.username_prefix}-{geo}-{idx}"
            pwd  = args.password
            creds_b64 = base64.b64encode(f"{user}:{pwd}".encode()).decode()
            tasks.append((geo, idx, creds_b64, user, pwd))

    random.shuffle(tasks)  # mix geos so we get coverage across all early

    print(f"Testing {len(geos)} geos x {args.per_geo} proxies = {len(tasks)} total")
    print(f"Workers: {args.workers}  Stop-after: {args.stop_after or 'unlimited'}\n")

    geo_result = defaultdict(lambda: defaultdict(int))  # geo → tag → count
    ok_proxies = []
    tested = 0

    with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as ex:
        futures = {ex.submit(probe, geo, idx, cb, u, p): (geo, idx, u, p)
                   for geo, idx, cb, u, p in tasks}

        for fut in concurrent.futures.as_completed(futures):
            geo, idx, result = fut.result()
            if result == "SKIP":
                continue
            tested += 1

            # Categorise into short tag
            if result == "OK":
                tag = "OK"
            elif result == "GATEWAY_BLOCKED":
                tag = "GW_BLOCK"
            elif result.startswith("NOT_IN_PLAN"):
                tag = "NO_PLAN"
            elif result == "PORTAL_BLOCKED":
                tag = "PORTAL"
            else:
                tag = "ERR"

            geo_result[geo][tag] += 1

            if result == "OK":
                proxy_line = f"http://{futures[fut][2]}:{futures[fut][3]}@p.webshare.io:80"
                ok_proxies.append(proxy_line)
                print(f"  PASS  {geo}-{idx:4d}  {proxy_line}")
                sys.stdout.flush()
                if args.stop_after and len(ok_proxies) >= args.stop_after:
                    _stop.set()
            else:
                # Print first occurrence of each tag per geo
                if geo_result[geo][tag] == 1:
                    print(f"  {tag:10s} {geo}-{idx:4d}  {result[:70]}")
                    sys.stdout.flush()

    print(f"\n{'='*70}")
    print(f"Tested {tested} proxies  |  {len(ok_proxies)} OK\n")

    # Summary table
    all_tags = ["OK", "PORTAL", "GW_BLOCK", "NO_PLAN", "ERR"]
    header = f"{'GEO':6s}" + "".join(f"{t:12s}" for t in all_tags)
    print(header)
    print("-" * len(header))
    for geo in sorted(geo_result):
        r = geo_result[geo]
        row = f"{geo:6s}" + "".join(f"{r.get(t,0):<12d}" for t in all_tags)
        print(row)

    if ok_proxies:
        existing = []
        if OUT_FILE.exists():
            existing = [l for l in OUT_FILE.read_text().splitlines() if l.strip()]
        combined = list(dict.fromkeys(existing + ok_proxies))
        OUT_FILE.write_text("\n".join(combined) + "\n", encoding="utf-8")
        print(f"\nWritten {len(ok_proxies)} working proxies → {OUT_FILE}")
    else:
        print("\nNo working proxies found in any geo.")


if __name__ == "__main__":
    main()
