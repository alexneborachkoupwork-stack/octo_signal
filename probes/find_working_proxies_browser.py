"""
Find Webshare proxies that pass DataDome + portal using real Chrome (Playwright).
Uses proxy_bridge.py (same as the worker) to route HTTP proxies through Chrome.

Tests proxies in parallel batches. Writes working ones to proxies_working.txt.
"""
import sys, random, threading, time, queue
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

# Add app to path so proxy_bridge imports work
ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "app" / "core"))

from proxy_bridge import start_bridge

HOME = "https://pedidodevistos.mne.gov.pt/VistosOnline/"
AUTH = "https://pedidodevistos.mne.gov.pt/VistosOnline/Authentication.jsp"

DATA_DIR = ROOT / "app" / "core" / "data"
OUT_FILE = DATA_DIR / "proxies_working.txt"

_stop = threading.Event()


def probe_via_browser(proxy_http: str, label: str, timeout_ms: int = 20000) -> str:
    """Returns 'OK', 'BLOCKED', 'DATADOME', 'ERR:...'"""
    if _stop.is_set():
        return "SKIP"
    try:
        from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
        bridge = start_bridge(proxy_http)
        with sync_playwright() as pw:
            browser = pw.chromium.launch(
                channel="chrome",
                headless=True,
                args=["--no-sandbox", "--disable-dev-shm-usage"],
            )
            ctx = browser.new_context(
                proxy={"server": bridge},
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36",
                ignore_https_errors=True,
            )
            page = ctx.new_page()
            try:
                # Navigate home first (establishes session, passes DataDome)
                page.goto(HOME, timeout=timeout_ms, wait_until="domcontentloaded")
                time.sleep(1)

                # Check for DataDome challenge
                body = page.content()
                if "/ch/bd.js" in body:
                    return "DATADOME"

                # Navigate to auth page
                page.goto(AUTH, timeout=timeout_ms, wait_until="domcontentloaded")
                time.sleep(0.5)

                body2 = page.content()
                if 'name="username"' in body2 or 'type="password"' in body2:
                    return "OK"
                if "Não foi possível" in body2 or "could not be processed" in body2.lower():
                    return "BLOCKED"
                if "/ch/bd.js" in body2:
                    return "DATADOME"

                title = page.title()
                return f"UNKNOWN(title={title!r})"
            except PWTimeout:
                return "TIMEOUT"
            finally:
                try:
                    browser.close()
                except Exception:
                    pass
    except Exception as e:
        return f"ERR({type(e).__name__}: {str(e)[:80]})"


def load_proxies(path: Path, limit: int | None = None) -> list[tuple[str, str]]:
    """Load proxies from file, normalise to http://, return (label, url) list."""
    lines = [l.strip() for l in path.read_text(encoding="utf-8-sig").splitlines()
             if l.strip() and not l.startswith("#")]
    lines = [l.replace("socks5://", "http://").replace("socks4://", "http://") for l in lines]
    random.shuffle(lines)
    if limit:
        lines = lines[:limit]
    labels = []
    for i, l in enumerate(lines):
        from urllib.parse import urlparse
        p = urlparse(l)
        u = p.username or ""
        parts = u.split("-")
        geo = parts[-2] if len(parts) >= 2 else "??"
        labels.append((f"{geo}-{i+1}", l))
    return labels


def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--file", default=str(DATA_DIR / "proxies_webshare.txt"))
    ap.add_argument("--workers", type=int, default=5,
                    help="Parallel Chrome browsers (each uses ~200MB RAM)")
    ap.add_argument("--limit", type=int, default=100,
                    help="Max proxies to test (shuffled from file)")
    ap.add_argument("--stop-after", type=int, default=5,
                    help="Stop after finding this many OK proxies")
    args = ap.parse_args()

    proxies = load_proxies(Path(args.file), limit=args.limit)
    print(f"Testing {len(proxies)} proxies  |  {args.workers} parallel Chrome browsers")
    print(f"Stop-after: {args.stop_after} working\n")

    ok, blocked, datadome, errors = [], 0, 0, 0

    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = {ex.submit(probe_via_browser, url, lbl): (lbl, url)
                for lbl, url in proxies}
        for fut in as_completed(futs):
            lbl, url = futs[fut]
            result = fut.result()
            if result == "SKIP":
                continue
            if result == "OK":
                ok.append(url)
                print(f"  PASS     {lbl:12s}  {url}")
                sys.stdout.flush()
                if args.stop_after and len(ok) >= args.stop_after:
                    _stop.set()
            elif result == "BLOCKED":
                blocked += 1
                print(f"  BLOCKED  {lbl:12s}")
                sys.stdout.flush()
            elif result == "DATADOME":
                datadome += 1
                print(f"  DATADOME {lbl:12s}")
                sys.stdout.flush()
            elif result == "TIMEOUT":
                errors += 1
                print(f"  TIMEOUT  {lbl:12s}")
                sys.stdout.flush()
            else:
                errors += 1
                print(f"  ERR      {lbl:12s}  {result}")
                sys.stdout.flush()

    print(f"\n{'='*60}")
    print(f"OK={len(ok)}  blocked={blocked}  datadome={datadome}  err/timeout={errors}")

    if ok:
        existing = []
        if OUT_FILE.exists():
            existing = [l for l in OUT_FILE.read_text().splitlines() if l.strip()]
        combined = list(dict.fromkeys(existing + ok))
        OUT_FILE.write_text("\n".join(combined) + "\n", encoding="utf-8")
        print(f"\nWritten {len(ok)} working proxies -> {OUT_FILE}")
    else:
        print("\nNo working proxies found.")


if __name__ == "__main__":
    main()
