"""
Live probe: fresh login via the confirmed pipeline, then hit gettime +
getPeriodosOcupados through the browser context (WAF-cleared), not raw primp.

Usage:
  uv run python probe_gettime_live.py <username> [posto_id]
"""
import re
import sys
from datetime import date, timedelta
from pathlib import Path

_HERE = Path(__file__).parent
sys.path.insert(0, str(_HERE))
sys.path.insert(0, str(_HERE.parent / "app" / "core"))

import csv


def _load_account(username: str) -> dict:
    with open(_HERE.parent / "app" / "core" / "data" / "test_accounts.csv", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    for r in rows:
        if r["username"] == username:
            return r
    raise RuntimeError(f"account {username} not found")


def _load_env() -> dict:
    env = {}
    env_file = _HERE / ".env"
    if not env_file.exists():
        env_file = _HERE.parent / "app" / "core" / ".env"
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        env[k.strip()] = v.strip()
    return env


def main():
    username = sys.argv[1] if len(sys.argv) > 1 else "clafer8430"
    posto_id = sys.argv[2] if len(sys.argv) > 2 else "2032"

    import session as sess
    from proxy_pool import PersistentProxyPool

    env = _load_env()
    capsolver_keys = [k.strip() for k in env.get("CAPSOLVER_KEYS", "").split(",") if k.strip()]
    if not capsolver_keys:
        print("ERROR: no CAPSOLVER_KEYS in .env")
        sys.exit(1)

    acct = _load_account(username)
    pool = PersistentProxyPool(str(_HERE.parent / "app" / "core" / "data" / "proxies_soax.txt"))

    print(f"[probe] logging in as {username} posto={posto_id}")
    proxy_url = None
    s = None
    for attempt in range(10):
        proxy_url = pool.advance()
        try:
            s = sess.get_session(proxy_url, headless=True)
        except Exception as e:
            print(f"  attempt {attempt+1}: get_session failed: {e}")
            continue
        try:
            result = s.browser_login(
                username, acct["password"], lang="ENG",
                capsolver_keys=capsolver_keys,
                skip_checkbox=True, min_score=50, timeout=120,
            )
        except Exception as e:
            print(f"  attempt {attempt+1}: login failed: {e}")
            try: s.close_browser()
            except Exception: pass
            s = None
            continue
        import json as _json
        body = (result.get("body") or "").strip()
        try:
            resp = _json.loads(body)
        except Exception:
            resp = {}
        if resp.get("type") in ("", "200", "success"):
            print(f"  login OK (attempt={attempt+1})  proxy={proxy_url.split('@')[-1]}")
            break
        print(f"  attempt {attempt+1}: login rejected: {body[:150]}")
        try: s.close_browser()
        except Exception: pass
        s = None
    else:
        print("ERROR: all login attempts failed")
        sys.exit(1)

    # --- gettime ---
    gt_url = f"https://pedidodevistos.mne.gov.pt/VistosOnline/gettime?id_posto={posto_id}"
    print(f"\n[probe] browser_fetch GET {gt_url}")
    try:
        raw = s.browser_fetch(gt_url, {}, method="GET", encode="none", timeout=30)
        gt_status, gt_body = raw["status"], raw["body"]
        print(f"  status={gt_status}  len={len(gt_body)}")
        print(f"  snippet={gt_body[:500]!r}")
    except Exception as e:
        print(f"  gettime browser_fetch failed: {e}")
        gt_body = ""

    # Try both old JS-array format and new JSON <pre> format
    special_days = {}
    for year, month, days_str in re.findall(
        r"SPECIAL_DAYS\[(\d{4})\]\[(\d+)\]\s*=\s*new Array\((.*?)\);", gt_body
    ):
        days = {int(d.strip("'\"")) for d in days_str.split(",") if d.strip()}
        special_days.setdefault(int(year), {})[int(month)] = days
    if special_days:
        print(f"  parsed SPECIAL_DAYS (old JS-array format): {special_days}")
    else:
        pre_m = re.search(r"<pre[^>]*>([\s\S]*?)</pre>", gt_body)
        if pre_m:
            import json as _json
            try:
                data = _json.loads(pre_m.group(1).strip())
                print(f"  parsed as JSON (new format): keys={list(data.keys()) if isinstance(data, dict) else type(data)}")
                print(f"  {_json.dumps(data, indent=2)[:800]}")
            except Exception as e:
                print(f"  <pre> content not JSON: {e}  content[:300]={pre_m.group(1)[:300]!r}")
        else:
            print("  no SPECIAL_DAYS array and no <pre> JSON found -- unknown format")

    # --- getPeriodosOcupados for next 5 weekdays ---
    today = date.today()
    candidates = []
    cur = today + timedelta(days=1)
    while len(candidates) < 5:
        y, m, d = cur.year, cur.month, cur.day
        if cur.weekday() < 5 and d not in special_days.get(y, {}).get(m, set()):
            candidates.append(cur)
        cur += timedelta(days=1)
    print(f"\n[probe] candidate days to check: {[str(d) for d in candidates]}")

    EXPECTED = {str(i) for i in range(1, 15)}
    for d in candidates:
        date_str = d.strftime("%Y/%m/%d")
        po_url = (f"https://pedidodevistos.mne.gov.pt/VistosOnline/"
                  f"getPeriodosOcupados?id_posto={posto_id}&data_agendamento={date_str}")
        print(f"\n[probe] browser_fetch GET {po_url}")
        try:
            raw = s.browser_fetch(po_url, {}, method="GET", encode="none", timeout=30)
            po_status, po_body = raw["status"], raw["body"]
            print(f"  status={po_status}  len={len(po_body)}")
            print(f"  body={po_body[:500]!r}")
            m = re.search(r"periodos_ocupados='([^']*)'", po_body)
            if m:
                occupied = set(m.group(1).split(",")) if m.group(1) else set()
                free = EXPECTED - occupied
                print(f"  occupied={sorted(occupied)}  FREE={sorted(free, key=int)}")
            else:
                pre_m = re.search(r"<pre[^>]*>([\s\S]*?)</pre>", po_body)
                if pre_m:
                    import json as _json
                    try:
                        data = _json.loads(pre_m.group(1).strip())
                        print(f"  parsed as JSON: {data}")
                    except Exception as e:
                        print(f"  <pre> not JSON: {e}")
        except Exception as e:
            print(f"  getPeriodosOcupados failed: {e}")

    try:
        s.close_browser()
    except Exception:
        pass
    print("\n[probe] DONE")


if __name__ == "__main__":
    main()
