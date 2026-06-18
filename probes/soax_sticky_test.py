"""
SOAX sticky session IP-consistency diagnostic.

Sends repeated primp GET requests through a single SOAX session URL and
reports whether the exit IP stays constant across the full duration.

Also probes the e-VISA portal each cycle to confirm reachability.

Usage:
    uv run python soax_sticky_test.py [--proxy URL] [--duration 600] [--interval 60]
    (reads PROXY from .env if --proxy omitted)
"""

import argparse
import time
from pathlib import Path


_ENV_FILE        = Path(__file__).parent / ".env"
_SOAX_PROXY_FILE = Path(__file__).parent / "data" / "proxies_soax.txt"
_PORTAL          = "https://pedidodevistos.mne.gov.pt/VistosOnline/"
_IP_URL          = "https://api.ipify.org?format=json"


def _load_env() -> dict:
    env: dict[str, str] = {}
    if not _ENV_FILE.exists():
        return env
    for line in _ENV_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        env[k.strip()] = v.strip()
    return env


def _make_client(proxy: str | None):
    import primp
    kwargs = {"verify": True, "follow_redirects": True, "timeout": 20}
    if proxy:
        kwargs["proxy"] = proxy
    return primp.Client(**kwargs)


def _probe(client, proxy_label: str) -> tuple[str, str]:
    """Returns (exit_ip, portal_status). Raises on hard error."""
    import json as _json

    # IP check
    try:
        r = client.get(_IP_URL, timeout=15)
        data = _json.loads(r.text)
        exit_ip = data.get("ip", "?")
    except Exception as e:
        exit_ip = f"ERROR:{e}"

    # Portal reachability (no-follow to keep cookies clean)
    try:
        r2 = client.get(_PORTAL, timeout=10, follow_redirects=False)
        if r2.status_code == 200:
            portal_status = "200-ok"
        elif r2.status_code == 302:
            portal_status = "302-auth"   # portal up, session redirect
        elif r2.status_code >= 500:
            portal_status = f"{r2.status_code}-server-err"
        else:
            portal_status = f"{r2.status_code}"
    except Exception as e:
        portal_status = f"ERR({type(e).__name__})"

    return exit_ip, portal_status


def run(proxy: str | None, duration: int, interval: int) -> None:
    print(f"\n[soax-sticky] proxy  : {proxy or '(no proxy)'}")
    print(f"[soax-sticky] duration: {duration}s  interval: {interval}s")
    print(f"[soax-sticky] ip-url  : {_IP_URL}")
    print(f"[soax-sticky] portal  : {_PORTAL}")
    print()
    print(f"{'#':<4}  {'time':>6}s  {'exit_ip':<20}  portal")
    print("-" * 52)

    client = _make_client(proxy)
    rows: list[dict] = []
    start = time.time()
    cycle = 0

    while True:
        t0 = time.time()
        elapsed = t0 - start
        exit_ip, portal_status = _probe(client, proxy or "direct")
        latency = int((time.time() - t0) * 1000)

        cycle += 1
        rows.append({"cycle": cycle, "elapsed": elapsed, "exit_ip": exit_ip,
                     "portal": portal_status, "latency_ms": latency})
        print(f"{cycle:<4}  {elapsed:>6.0f}s  {exit_ip:<20}  {portal_status}  ({latency}ms)")

        remaining = duration - (time.time() - start)
        if remaining <= 0:
            break
        sleep_for = min(interval, remaining)
        time.sleep(sleep_for)

    # Summary
    valid_ips = [r["exit_ip"] for r in rows if not r["exit_ip"].startswith("ERROR")]
    unique_ips = set(valid_ips)
    errors = [r for r in rows if r["exit_ip"].startswith("ERROR")]

    print()
    print("=" * 52)
    print(f"[soax-sticky] SUMMARY  cycles={len(rows)}  errors={len(errors)}")
    if len(unique_ips) == 1:
        print(f"[soax-sticky] RESULT: CONSISTENT  exit_ip={next(iter(unique_ips))}")
    elif len(unique_ips) == 0:
        print(f"[soax-sticky] RESULT: ALL ERRORS — could not determine exit IP")
    else:
        print(f"[soax-sticky] RESULT: INCONSISTENT  unique_ips={len(unique_ips)}  ips={sorted(unique_ips)}")
    print("=" * 52)


def _first_soax_proxy() -> str | None:
    """Return the first proxy URL from proxies_soax.txt, or None."""
    if not _SOAX_PROXY_FILE.exists():
        return None
    for line in _SOAX_PROXY_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            return line if line.startswith("http") else f"http://{line}"
    return None


def main() -> None:
    env = _load_env()
    parser = argparse.ArgumentParser(description="SOAX sticky session IP-consistency test")
    parser.add_argument("--proxy",    default="",
                        help="Proxy URL (default: PROXY from .env, then first line of proxies_soax.txt)")
    parser.add_argument("--duration", type=int, default=600,
                        help="Total test duration in seconds (default: 600)")
    parser.add_argument("--interval", type=int, default=60,
                        help="Seconds between probes (default: 60)")
    args = parser.parse_args()

    proxy = args.proxy.strip() or env.get("PROXY", "").strip() or _first_soax_proxy()
    if not proxy:
        print("[soax-sticky] WARNING: no proxy configured — testing direct connection")

    run(proxy, args.duration, args.interval)


if __name__ == "__main__":
    main()
