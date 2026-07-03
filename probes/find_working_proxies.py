"""
Find proxies that pass both the Webshare tunnel AND the portal's IP check.
Tests proxies by making a real HTTPS GET to Authentication.jsp.
Writes passing proxies to app/core/data/proxies_working.txt.
"""
import ssl
import socket
import base64
import concurrent.futures
import sys
import random
import threading
import argparse
from pathlib import Path
from urllib.parse import urlparse

TARGET_HOST = "pedidodevistos.mne.gov.pt"
TARGET_PORT = 443
AUTH_PATH   = "/VistosOnline/Authentication.jsp"
TIMEOUT     = 10

DATA_DIR    = Path(__file__).parent.parent / "app" / "core" / "data"
OUT_FILE    = DATA_DIR / "proxies_working.txt"

# Geos known to 502 at Webshare gateway level — skip to save time
GATEWAY_BLOCKED_GEOS = {"DE", "FR", "NL", "SE", "PL"}

# Shared stop-early flag
_stop = threading.Event()


def probe_proxy(label: str, line: str) -> tuple[str, str, str]:
    """Returns (label, proxy_line, result)."""
    if _stop.is_set():
        return label, line, "SKIP"
    line = line.strip()
    if not line or line.startswith("#"):
        return label, line, "SKIP"
    p = urlparse(line)
    ph = p.hostname
    pp = p.port or 80
    pu = p.username or ""
    pw = p.password or ""
    creds = base64.b64encode(f"{pu}:{pw}".encode()).decode()

    try:
        s = socket.create_connection((ph, pp), timeout=TIMEOUT)
        req = (
            f"CONNECT {TARGET_HOST}:{TARGET_PORT} HTTP/1.1\r\n"
            f"Host: {TARGET_HOST}:{TARGET_PORT}\r\n"
            f"Proxy-Authorization: Basic {creds}\r\n"
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
        status = first.split()[1] if len(first.split()) > 1 else "0"
        if status != "200":
            s.close()
            return label, line, f"TUNNEL_FAIL({first.strip()})"

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
            return label, line, "BLOCKED(portal IP ban)"

        if 'name="username"' in body or 'type="password"' in body or "Authentication" in body[:2000]:
            return label, line, "OK"

        snippet = body[body.find("\r\n\r\n")+4:body.find("\r\n\r\n")+200].strip()
        return label, line, f"UNKNOWN({snippet[:80]!r})"

    except Exception as e:
        return label, line, f"ERR({type(e).__name__})"


def extract_geo(line: str) -> str:
    """Extract GEO code from Webshare username like Mylist1234-FR-1."""
    p = urlparse(line)
    u = p.username or ""
    parts = u.split("-")
    if len(parts) >= 2:
        return parts[-2].upper()
    return "??"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--file", default=str(DATA_DIR / "proxies_webshare.txt"),
                    help="Proxy file to scan")
    ap.add_argument("--workers", type=int, default=100, help="Concurrent workers")
    ap.add_argument("--stop-after", type=int, default=10,
                    help="Stop after this many OK proxies found (0=scan all)")
    ap.add_argument("--skip-geos", default=",".join(sorted(GATEWAY_BLOCKED_GEOS)),
                    help="Comma-separated GEOs to skip (gateway-blocked)")
    ap.add_argument("--only-geo", default="",
                    help="Only test this comma-separated list of GEOs")
    args = ap.parse_args()

    skip_geos = {g.strip().upper() for g in args.skip_geos.split(",") if g.strip()}
    only_geos = {g.strip().upper() for g in args.only_geo.split(",") if g.strip()}

    fpath = Path(args.file)
    if not fpath.exists():
        print(f"File not found: {fpath}")
        sys.exit(1)

    raw = [l.strip() for l in fpath.read_text(encoding="utf-8-sig").splitlines()
           if l.strip() and not l.startswith("#")]

    # Filter by geo
    filtered = []
    skipped_geo = 0
    for line in raw:
        geo = extract_geo(line)
        if only_geos and geo not in only_geos:
            skipped_geo += 1
            continue
        if not only_geos and geo in skip_geos:
            skipped_geo += 1
            continue
        filtered.append(line)

    # Shuffle so we hit many different geos/IPs early
    random.shuffle(filtered)

    all_proxies = [(f"{extract_geo(l)}-{i+1}", l) for i, l in enumerate(filtered)]

    print(f"Loaded {len(raw)} proxies; skipped {skipped_geo} (geo filter); testing {len(all_proxies)}")
    if skip_geos and not only_geos:
        print(f"Skipping gateway-blocked geos: {sorted(skip_geos)}")
    print(f"Workers: {args.workers}  |  Stop-after: {args.stop_after or 'unlimited'}")
    print()

    ok: list[str] = []
    blocked = tunnel_fail = errors = skipped = 0
    tested = 0
    geo_stats: dict[str, dict] = {}  # geo → {ok, block, tunnel, err}

    def record(geo, key):
        if geo not in geo_stats:
            geo_stats[geo] = {"ok": 0, "block": 0, "tunnel": 0, "err": 0}
        geo_stats[geo][key] += 1

    with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as ex:
        futures = {ex.submit(probe_proxy, lbl, line): (lbl, line)
                   for lbl, line in all_proxies}
        for fut in concurrent.futures.as_completed(futures):
            label, line, result = fut.result()
            geo = extract_geo(line)

            if result == "SKIP":
                skipped += 1
                continue

            tested += 1

            if result == "OK":
                ok.append(line)
                record(geo, "ok")
                print(f"  PASS  {label:12s}  {line}")
                sys.stdout.flush()
                if args.stop_after and len(ok) >= args.stop_after:
                    _stop.set()
            elif result.startswith("BLOCKED"):
                blocked += 1
                record(geo, "block")
                # print one sample per geo so we know it's reaching the portal
                if geo_stats[geo]["block"] == 1:
                    print(f"  BLOCK {label:12s}  (first {geo} portal-blocked)")
                    sys.stdout.flush()
            elif result.startswith("TUNNEL_FAIL"):
                tunnel_fail += 1
                record(geo, "tunnel")
                if geo_stats[geo]["tunnel"] == 1:
                    print(f"  TUNFL {label:12s}  {result}  (first {geo} tunnel fail)")
                    sys.stdout.flush()
            else:
                errors += 1
                record(geo, "err")
                print(f"  ERR   {label:12s}  {result}")
                sys.stdout.flush()

    print(f"\n{'='*70}")
    print(f"Results: {len(ok)} OK  |  {blocked} portal-blocked  |  {tunnel_fail} tunnel-fail  |  {errors} errors  |  {tested} tested")

    # Per-geo summary
    if geo_stats:
        print("\nPer-geo breakdown:")
        for geo in sorted(geo_stats):
            s = geo_stats[geo]
            print(f"  {geo:4s}  ok={s['ok']}  block={s['block']}  tunnel={s['tunnel']}  err={s['err']}")

    if ok:
        # Append to existing if present, else create
        existing = []
        if OUT_FILE.exists():
            existing = [l for l in OUT_FILE.read_text().splitlines() if l.strip()]
        combined = list(dict.fromkeys(existing + ok))  # dedup, preserve order
        OUT_FILE.write_text("\n".join(combined) + "\n", encoding="utf-8")
        print(f"\nWritten {len(ok)} working proxies ({len(combined)} total) → {OUT_FILE}")
    else:
        print("\nNo working proxies found.")


if __name__ == "__main__":
    main()
