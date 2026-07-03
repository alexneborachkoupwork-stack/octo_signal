"""
Standalone probe: load dulrodri's saved session, navigate to gettime +
getPeriodosOcupados, and print the raw JSON responses to understand the new format.
Run:
    uv run python -m app.core.probes.probe_periodos_json
"""
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "app/core"))

SESSION_FILE = ROOT / "app/core/sessions/dulrodri852906.json"
POSTO_ID = "3088"
BASE = "https://pedidodevistos.mne.gov.pt/VistosOnline"


def main():
    sess_data = json.loads(SESSION_FILE.read_text())
    proxy = sess_data.get("proxy")
    cookies = sess_data.get("cookies", [])

    print(f"[probe] session proxy={proxy}  cookies={len(cookies)}")

    from proxy_bridge import start_bridge
    from playwright.sync_api import sync_playwright

    socks_url = proxy
    bridge_url = start_bridge(socks_url)
    print(f"[probe] bridge={bridge_url}")

    with sync_playwright() as p:
        browser = p.chromium.launch(
            proxy={"server": bridge_url},
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )
        ctx = browser.new_context()
        # Restore cookies — stored as {name: value} dict, convert to Playwright format
        if isinstance(cookies, dict):
            cookies = [
                {"name": k, "value": v, "domain": "pedidodevistos.mne.gov.pt", "path": "/"}
                for k, v in cookies.items()
            ]
        ctx.add_cookies(cookies)
        page = ctx.new_page()

        # --- Step 1: gettime ---
        gt_url = f"{BASE}/gettime?id_posto={POSTO_ID}"
        print(f"\n[probe] GET {gt_url}")
        try:
            page.goto(gt_url, timeout=20000, wait_until="domcontentloaded")
            body = page.content()
            def _safe(s): return s.encode('ascii', 'replace').decode('ascii')
            print(f"[probe] gettime len={len(body)}")
            print(f"[probe] gettime body[:500]:\n{_safe(body[:500])}\n")

            # Try JSON extract from <pre>
            pre_m = re.search(r"<pre[^>]*>([\s\S]*?)</pre>", body)
            if pre_m:
                try:
                    data = json.loads(pre_m.group(1).strip())
                    print(f"[probe] gettime JSON keys={list(data.keys())}")
                    print(f"[probe] gettime JSON={json.dumps(data, indent=2)[:800]}")
                except json.JSONDecodeError as e:
                    print(f"[probe] gettime pre content not JSON: {e}")
                    print(f"[probe] pre content[:300]: {pre_m.group(1)[:300]}")
        except Exception as e:
            print(f"[probe] gettime error: {e}")

        # --- Step 2: getPeriodosOcupados for a few dates ---
        test_dates = ["2026/06/25", "2026/06/26", "2026/07/01"]
        for ds in test_dates:
            enc = ds.replace("/", "%2F")
            po_url = f"{BASE}/getPeriodosOcupados?id_posto={POSTO_ID}&data_agendamento={enc}"
            print(f"\n[probe] GET {po_url}")
            try:
                page.goto(po_url, timeout=15000, wait_until="domcontentloaded")
                body = page.content()
                print(f"[probe] periodos {ds} len={len(body)}")
                print(f"[probe] periodos {ds} body[:500]:\n{_safe(body[:500])}\n")

                pre_m = re.search(r"<pre[^>]*>([\s\S]*?)</pre>", body)
                if pre_m:
                    try:
                        data = json.loads(pre_m.group(1).strip())
                        print(f"[probe] periodos {ds} JSON keys={list(data.keys())}")
                        print(f"[probe] periodos {ds} JSON={json.dumps(data, indent=2)[:800]}")
                    except json.JSONDecodeError as e:
                        print(f"[probe] periodos {ds} pre not JSON: {e}")
                        print(f"[probe] pre content[:300]: {pre_m.group(1)[:300]}")
            except Exception as e:
                print(f"[probe] periodos {ds} error: {e}")

        browser.close()
    print("\n[probe] DONE")


if __name__ == "__main__":
    main()
