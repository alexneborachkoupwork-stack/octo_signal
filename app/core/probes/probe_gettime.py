"""
Probe: test gettime and getPeriodosOcupados endpoints using a live session.
Usage: uv run python -m app.core.probes.probe_gettime <posto_id>
"""
import sys, json, re
from pathlib import Path
from datetime import date, timedelta
import primp

ROOT = Path(__file__).resolve().parents[3]
SESSIONS_DIR = ROOT / "app" / "core" / "sessions"
COOKIES_URL  = "https://pedidodevistos.mne.gov.pt/VistosOnline/"

def load_session(username: str):
    for f in SESSIONS_DIR.glob("*.json"):
        data = json.loads(f.read_text())
        if data.get("checkpoint") == "schedule_jsp" and data.get("cookies"):
            print(f"[probe] using session: {f.name}  proxy={data.get('proxy','?')}")
            return data
    raise RuntimeError("No schedule_jsp session found")

def make_client(session: dict) -> primp.Client:
    import session as sess_mod
    proxy = session.get("proxy")
    client = primp.Client(
        impersonate=sess_mod.IMPERSONATE,
        proxy=proxy,
        follow_redirects=True,
        verify=True,
    )
    client.set_cookies(COOKIES_URL, session["cookies"])
    return client

def probe_gettime(client: primp.Client, posto_id: str):
    url = f"https://pedidodevistos.mne.gov.pt/VistosOnline/gettime?id_posto={posto_id}"
    print(f"\n[probe] GET {url}")
    r = client.get(url, timeout=30)
    print(f"  status={r.status_code}  len={len(r.text)}")
    print(f"  snippet={r.text[:300]!r}")
    return r.text

def parse_special_days(js_text: str) -> dict:
    """Extract SPECIAL_DAYS[year][month] = new Array(...) from JS response."""
    special = {}
    for year, month, days_str in re.findall(
        r"SPECIAL_DAYS\[(\d{4})\]\[(\d+)\]\s*=\s*new Array\((.*?)\);", js_text
    ):
        days = {int(d.strip("'\"")) for d in days_str.split(",") if d.strip()}
        special.setdefault(int(year), {})[int(month)] = days
    return special

def probe_periodos(client: primp.Client, posto_id: str, check_date: str):
    url = (f"https://pedidodevistos.mne.gov.pt/VistosOnline/"
           f"getPeriodosOcupados?id_posto={posto_id}&data_agendamento={check_date}")
    print(f"\n[probe] GET {url}")
    r = client.get(url, timeout=30)
    print(f"  status={r.status_code}  len={len(r.text)}")
    print(f"  body={r.text!r}")
    return r.text

def main():
    posto_id = sys.argv[1] if len(sys.argv) > 1 else "3088"
    print(f"[probe] posto_id={posto_id}")

    session = load_session(None)
    client  = make_client(session)

    # 1. gettime — calendar of special/blocked days
    gt_text = probe_gettime(client, posto_id)

    # 2. Parse special days and find next 5 non-weekend, non-special days
    special_days = parse_special_days(gt_text)
    today = date.today()
    open_days = []
    cur = today + timedelta(days=1)
    while len(open_days) < 5:
        y, m, d = cur.year, cur.month, cur.day
        if cur.weekday() < 5 and d not in special_days.get(y, {}).get(m, set()):
            open_days.append(cur)
        cur += timedelta(days=1)

    print(f"\n[probe] next 5 open days: {[str(d) for d in open_days]}")

    # 3. getPeriodosOcupados for each open day
    EXPECTED = {'1','2','3','4','5','6','7','8','9','10','11','12','13','14'}
    PERIODS  = {'1':'7-8','2':'8-9','3':'9-10','4':'10-11','5':'11-12',
                '6':'12-13','7':'13-14','8':'14-15','9':'15-16','10':'16-17',
                '11':'17-18','12':'18-19','13':'19-20','14':'20-21'}
    found_slots = []
    for d in open_days:
        date_str = d.strftime("%Y/%m/%d")
        body = probe_periodos(client, posto_id, date_str)
        m = re.search(r"periodos_ocupados='([^']*)'", body)
        if m:
            occupied = set(m.group(1).split(",")) if m.group(1) else set()
            free = EXPECTED - occupied
            if free:
                for p in sorted(free, key=int):
                    print(f"  OPEN SLOT: {date_str} period={p} ({PERIODS.get(p,'?')})")
                    found_slots.append((date_str, p))
            else:
                print(f"  {date_str}: all periods occupied")
        else:
            print(f"  {date_str}: unexpected response — no periodos_ocupados match")

    print(f"\n[probe] SUMMARY: {len(found_slots)} open slots found for posto {posto_id}")

if __name__ == "__main__":
    main()
