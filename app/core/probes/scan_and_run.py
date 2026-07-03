"""
Fast proxy scanner + auto-launcher.

Probes every Nth proxy using primp (browser JA3) through the SOCKS5 bridge.
Python ssl JA3 is blocked by DataDome even when proxy IPs are clean — primp's
rquest TLS fingerprint is much closer to real Chrome and gives accurate tier results.

Tiers:
  CLEAN   — primp got 200 with login page content (launch Playwright immediately)
  BDJS    — primp got 200 with bd.js challenge (Playwright might pass JS check)
  403     — primp got 403 (DataDome application block, TLS completed)
  TLS_DROP— primp got connection error after CONNECT (DataDome drops TLS)
  FAIL    — CONNECT itself failed (502 / timeout)

Usage:
    uv run python -m app.core.probes.scan_and_run [--step 100] [--parallel 30] [--workers 3]
"""
import json
import os
import sys
import time
import subprocess
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "app/core"))

import primp
from proxy_bridge import start_bridge

PROXY_FILE = ROOT / "app/core/data/proxies_webshare.txt"
STATE_FILE = ROOT / "app/core/data/proxies_webshare.proxy_state.json"
LOG_DIR    = ROOT / "app/core/data/logs"
LOG_DIR.mkdir(exist_ok=True)

TIER_CLEAN = "CLEAN"
TIER_BDJS  = "BDJS"
TIER_403   = "403"
TIER_DROP  = "TLS_DROP"
TIER_FAIL  = "FAIL"


def probe_one(offset: int, socks_url: str) -> tuple[int, str, str]:
    try:
        bridge = start_bridge(socks_url)
        client = primp.Client(
            impersonate="chrome_131",
            proxy=bridge,
            verify=True,
            timeout=18,
            follow_redirects=False,
        )
        r = client.get("https://pedidodevistos.mne.gov.pt/VistosOnline/login")
        status = r.status_code
        text = r.text

        if status == 200:
            if "bd.js" in text:
                return offset, TIER_BDJS, "bd.js JS challenge"
            if "login" in text.lower() or "recaptcha" in text.lower() or "password" in text.lower():
                return offset, TIER_CLEAN, "login page — NO DataDome block"
            return offset, TIER_BDJS, f"200 unknown content: {text[:60]}"
        if status in (301, 302, 303):
            loc = r.headers.get("location", "")
            return offset, TIER_CLEAN, f"redirect → {loc[:60]}"
        if status == 403:
            return offset, TIER_403, "403 DataDome block"
        return offset, TIER_403, f"HTTP {status}"

    except Exception as e:
        msg = str(e)[:80]
        if "tls" in msg.lower() or "eof" in msg.lower() or "connect" in msg.lower():
            return offset, TIER_DROP, msg
        return offset, TIER_FAIL, msg


_ACCOUNT_OFFSET = 10  # module-level; advances by count each launch, wraps at 47
_ACCOUNT_TOTAL  = 47  # test_accounts.csv row count (excluding header)


def launch_workers(cursor: int, count: int = 3):
    global _ACCOUNT_OFFSET
    state = {"cursor": cursor, "used_ips": [], "ip_cache": {}, "daily_burns": {}}
    STATE_FILE.write_text(json.dumps(state))
    ts = time.strftime("%Y%m%d_%H%M%S")
    log_err = LOG_DIR / f"run_3088_scan_{ts}.log.err"
    log_out = LOG_DIR / f"run_3088_scan_{ts}.log"
    cmd = ["uv", "run", "python", "-m", "app.cli",
           "--mode", "test", "--posto", "3088",
           "--count", str(count), "--no-isp", "--max-rehab", "2",
           "--account-offset", str(_ACCOUNT_OFFSET)]
    print(f"[launcher] account_offset={_ACCOUNT_OFFSET} (accounts {_ACCOUNT_OFFSET}–{_ACCOUNT_OFFSET+count-1})", flush=True)
    _ACCOUNT_OFFSET = (_ACCOUNT_OFFSET + count) % _ACCOUNT_TOTAL
    proc = subprocess.Popen(
        cmd, stderr=open(log_err, "w"), stdout=open(log_out, "w"), cwd=ROOT,
    )
    print(f"[launcher] {count} workers started pid={proc.pid}  log={log_err.name}", flush=True)
    return proc, log_err


def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--step",     type=int, default=100, help="offset jump between probes")
    ap.add_argument("--parallel", type=int, default=25,  help="concurrent probe threads")
    ap.add_argument("--workers",  type=int, default=3,   help="Playwright workers on hit")
    ap.add_argument("--start",    type=int, default=0)
    args = ap.parse_args()

    lines = PROXY_FILE.read_text(encoding="utf-8").splitlines()
    total = len(lines)
    probes_per_round = total // args.step
    print(f"[scan] {total} proxies  step={args.step}  parallel={args.parallel}  "
          f"~{probes_per_round} probes/round", flush=True)

    round_num    = 0
    start_offset = args.start % args.step
    active_proc  = None

    while True:
        round_num += 1
        offsets = list(range(start_offset, total, args.step))
        t0 = time.time()
        print(f"\n[scan] === Round {round_num}  start_offset={start_offset}  "
              f"probes={len(offsets)} ===", flush=True)

        results: list[tuple[int, str, str]] = []
        with ThreadPoolExecutor(max_workers=args.parallel) as pool:
            futs = {
                pool.submit(probe_one, off, lines[off].strip()): off
                for off in offsets if lines[off].strip()
            }
            for fut in as_completed(futs):
                try:
                    results.append(fut.result())
                except Exception as e:
                    results.append((futs[fut], TIER_FAIL, str(e)[:60]))

        clean = [(o, t, m) for o, t, m in results if t == TIER_CLEAN]
        bdjs  = [(o, t, m) for o, t, m in results if t == TIER_BDJS]
        ok403 = [(o, t, m) for o, t, m in results if t == TIER_403]
        drops = [(o, t, m) for o, t, m in results if t == TIER_DROP]
        fails = [(o, t, m) for o, t, m in results if t == TIER_FAIL]

        elapsed = time.time() - t0
        print(f"[scan] Round {round_num} done in {elapsed:.1f}s — "
              f"CLEAN={len(clean)} BDJS={len(bdjs)} 403={len(ok403)} "
              f"TLS_DROP={len(drops)} FAIL={len(fails)}", flush=True)

        def _prefer_gb(hits):
            """Sort hits so GB-section offsets (36500-40499) come first."""
            gb = [h for h in hits if 36500 <= h[0] <= 40499]
            other = [h for h in hits if not (36500 <= h[0] <= 40499)]
            return gb + other

        if clean:
            best = _prefer_gb(clean)[0]
            print(f"[scan] *** CLEAN at offset={best[0]}: {best[2]} ***", flush=True)
            if active_proc and active_proc.poll() is None:
                active_proc.terminate()
            active_proc, _ = launch_workers(best[0], args.workers)
            active_proc.wait()
            print("[scan] Workers done. Continuing scan...", flush=True)

        elif bdjs:
            # BDJS = primp got 200+bd.js challenge. Playwright has a full JS engine and
            # passes the bd.js fingerprint check on these IPs — confirmed in prior runs.
            # GB-section IPs (36500-40499) have better DataDome POST pass rate than FR.
            best = _prefer_gb(bdjs)[0]
            print(f"[scan] *** {len(bdjs)} BDJS hits — launching workers at offset={best[0]} (GB={36500<=best[0]<=40499}) ***", flush=True)
            if active_proc and active_proc.poll() is None:
                active_proc.terminate()
            active_proc, _ = launch_workers(best[0], args.workers)
            active_proc.wait()
            print("[scan] Workers done. Continuing scan...", flush=True)

        else:
            best_tier = "403" if ok403 else ("TLS_DROP" if drops else "FAIL")
            print(f"[scan] No usable IPs. Best={best_tier}. Sample 403s:", flush=True)
            for o, _, m in ok403[:3]:
                print(f"  offset={o}: {m}", flush=True)

        # Rotate start_offset so each round samples different IPs within each bucket
        start_offset = (start_offset + 1) % args.step
        print(f"[scan] Sleeping 120s before round {round_num + 1} (letting block breathe) ...", flush=True)
        time.sleep(120)


if __name__ == "__main__":
    main()
