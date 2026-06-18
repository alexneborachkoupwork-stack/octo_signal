"""
Geo-location probe for Webshare SOCKS5 proxy pool.

Samples N proxies from proxies_webshare.txt, for each:
  1. Resolves exit IP + country via ipwho.is (through the proxy itself)
  2. Tests connectivity to the target site via primp (SOCKS5 direct)
     - Tunnel OK  = socks5 reached the target (no connection error)
     - No-challenge = HTTP response did not include bot-detection JS
  3. Groups results by country and reports pass rates.

Usage:
  uv run python probe_webshare_geo.py               # 60 proxies, 20 workers
  uv run python probe_webshare_geo.py --sample 120 --workers 30
  uv run python probe_webshare_geo.py --start 200 --sample 60   # offset into list
"""

import argparse
import json
import threading
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

TARGET_URL   = "https://pedidodevistos.mne.gov.pt/VistosOnline/"
IPWHO_URL    = "https://ipwho.is/"
_GEO_TIMEOUT = 10   # seconds for geo lookup (fast service)
_TGT_TIMEOUT = 25   # seconds for target site (matches session.py default)
_print_lock  = threading.Lock()


# ── Per-proxy probe ────────────────────────────────────────────────────────────

def probe_one(proxy: str, idx: int) -> dict:
    """
    Returns dict:
      proxy, idx, exit_ip, country, country_code, city,
      tunnel_ok (bool), no_challenge (bool), error (str|None)
    """
    result = {
        "proxy":        proxy,
        "idx":          idx,
        "exit_ip":      None,
        "country":      "Unknown",
        "country_code": "??",
        "city":         "",
        "tunnel_ok":    False,
        "no_challenge": False,
        "error":        None,
    }

    try:
        import primp
        # Step 1 — exit IP + country (fast geo service)
        geo_client = primp.Client(proxy=proxy, verify=False,
                                  follow_redirects=True, timeout=_GEO_TIMEOUT)
        try:
            r = geo_client.get(IPWHO_URL)
            geo = r.json() if r.status_code == 200 else {}
            result["exit_ip"]      = geo.get("ip", "")
            result["country"]      = geo.get("country", "Unknown")
            result["country_code"] = geo.get("country_code", "??")
            result["city"]         = geo.get("city", "")
        except Exception as e:
            result["error"] = f"geo:{type(e).__name__}:{str(e)[:60]}"

        # Step 2 — target connectivity (separate client with longer timeout)
        if result["exit_ip"]:
            tgt_client = primp.Client(proxy=proxy, verify=False,
                                      follow_redirects=True, timeout=_TGT_TIMEOUT)
            try:
                r2 = tgt_client.get(TARGET_URL)
                result["tunnel_ok"]    = True
                result["no_challenge"] = "/ch/bd.js" not in r2.text
            except Exception as e:
                ename = type(e).__name__
                # Distinguish blocked (ConnectError) from slow (TimeoutError)
                tag = "TIMEOUT" if "Timeout" in ename else "BLOCKED"
                tgt_err = f"{tag}:{str(e)[:50]}"
                result["error"] = (result["error"] + " | " + tgt_err
                                   if result["error"] else tgt_err)

    except Exception as e:
        result["error"] = f"client:{type(e).__name__}:{str(e)[:60]}"

    if result["tunnel_ok"] and result["no_challenge"]:
        status = "OK "
    elif result["tunnel_ok"]:
        status = "WAF"
    elif result["exit_ip"] and result["error"] and "TIMEOUT" in result.get("error",""):
        status = "SLW"   # slow — may work with longer timeout
    else:
        status = "ERR"

    with _print_lock:
        cc   = result["country_code"]
        ip   = (result["exit_ip"] or "?")[:16]
        city = result["city"][:15]
        err  = f"  [{result['error'][:60]}]" if result["error"] else ""
        print(f"  [{idx:04d}] {status}  {cc:<4} {ip:<17} {city:<18}{err}")

    return result


# ── Aggregate + report ────────────────────────────────────────────────────────

def report(results: list[dict]) -> None:
    by_country: dict[str, list[dict]] = defaultdict(list)
    for r in results:
        key = f"{r['country_code']} ({r['country']})"
        by_country[key].append(r)

    def _is_slow(r):
        return (not r["tunnel_ok"] and r["exit_ip"]
                and "TIMEOUT" in (r.get("error") or ""))

    total  = len(results)
    alive  = sum(1 for r in results if r["tunnel_ok"])
    slow   = sum(1 for r in results if _is_slow(r))
    clean  = sum(1 for r in results if r["tunnel_ok"] and r["no_challenge"])
    blocked= sum(1 for r in results if not r["tunnel_ok"] and not _is_slow(r))

    print(f"\n{'='*72}")
    print(f"  WEBSHARE GEO PROBE  —  {total} proxies sampled")
    print(f"  OK (no challenge) : {clean}/{total} ({clean*100//total if total else 0}%)")
    print(f"  Tunnel OK (w/WAF) : {alive}/{total} ({alive*100//total if total else 0}%)")
    print(f"  Slow (timeout)    : {slow}  — may work in actual flow (30s timeout)")
    print(f"  Blocked/dead      : {blocked}")
    print(f"{'='*72}")
    print(f"  {'Country':<28} {'N':>5} {'OK':>5} {'Slow':>6} {'Block':>7} {'OK%':>6}")
    print(f"  {'-'*58}")

    rows = []
    for country, rlist in by_country.items():
        n       = len(rlist)
        t_ok    = sum(1 for r in rlist if r["tunnel_ok"] and r["no_challenge"])
        slow_n  = sum(1 for r in rlist if _is_slow(r))
        blk     = n - t_ok - slow_n - sum(1 for r in rlist if r["tunnel_ok"] and not r["no_challenge"])
        rate    = t_ok * 100 // n if n else 0
        rows.append((rate, country, n, t_ok, slow_n, blk))

    for rate, country, n, t_ok, slow_n, blk in sorted(rows, reverse=True):
        bar = "+" * (rate // 10) + ("~" * min(slow_n, 5))
        print(f"  {country:<28} {n:>5} {t_ok:>5} {slow_n:>6} {blk:>7}   {rate:>3}%  {bar}")

    print(f"{'='*72}")

    # Top IPs per country
    good = [r for r in results if r["tunnel_ok"] and r["no_challenge"]]
    if good:
        print(f"\n  Best proxy indices (tunnel OK, no challenge):")
        for r in good[:20]:
            print(f"    [{r['idx']:04d}]  {r['country_code']}  {r['exit_ip']}")

    bad_countries = {r["country_code"] for r in results
                     if not r["tunnel_ok"] and r["country_code"] != "??"}
    good_countries = {r["country_code"] for r in results
                      if r["tunnel_ok"] and r["no_challenge"]}
    if bad_countries - good_countries:
        print(f"\n  Countries with 0% tunnel success: {bad_countries - good_countries}")


# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sample",  type=int, default=60,
                    help="Number of proxies to test (default: 60)")
    ap.add_argument("--workers", type=int, default=20,
                    help="Parallel workers (default: 20)")
    ap.add_argument("--start",   type=int, default=0,
                    help="Start offset into proxy list (default: 0)")
    ap.add_argument("--proxy-file", default="data/proxies_webshare.txt",
                    help="Proxy list file")
    args = ap.parse_args()

    proxy_file = Path(__file__).parent / args.proxy_file
    all_proxies = [l.strip() for l in proxy_file.read_text().splitlines()
                   if l.strip() and not l.startswith("#")]

    total_available = len(all_proxies)
    start  = args.start % total_available if total_available else 0
    sample = min(args.sample, total_available)

    # Take a contiguous slice (wraps around if needed)
    indices = [(start + i) % total_available for i in range(sample)]
    proxies = [(i, all_proxies[i]) for i in indices]

    print(f"[probe] {proxy_file.name}: {total_available} total proxies")
    print(f"[probe] testing {sample} proxies starting at index {start}")
    print(f"[probe] workers={args.workers}  geo_timeout={_GEO_TIMEOUT}s  tgt_timeout={_TGT_TIMEOUT}s")
    print(f"\n  {'[idx]':7} {'':4} {'CC':<4} {'Exit IP':<17} {'City':<18}")
    print(f"  {'-'*60}")

    t0 = time.time()
    results = []
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futures = {ex.submit(probe_one, proxy, idx): (idx, proxy)
                   for idx, proxy in proxies}
        for fut in as_completed(futures):
            try:
                results.append(fut.result())
            except Exception as e:
                idx, proxy = futures[fut]
                results.append({"proxy": proxy, "idx": idx,
                                 "country": "Unknown", "country_code": "??",
                                 "exit_ip": None, "city": "",
                                 "tunnel_ok": False, "no_challenge": False,
                                 "error": str(e)})

    elapsed = time.time() - t0
    print(f"\n  Completed in {elapsed:.1f}s")
    report(results)


if __name__ == "__main__":
    main()
