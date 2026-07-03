import json, urllib.parse, sys

har_file = sys.argv[1] if len(sys.argv) > 1 else "app/core/har/apply.har"
with open(har_file, encoding="utf-8") as f:
    har = json.load(f)

BASE = "https://pedidodevistos.mne.gov.pt"
entries = har["log"]["entries"]

for e in entries:
    url = e["request"]["url"]
    targets = ["Questionario", "QuestNext", "Formulario", "ScheduleController", "/slots", "Schedule.jsp"]
    if not any(t in url for t in targets):
        continue
    short_url = url.replace(BASE, "")
    method = e["request"]["method"]
    print(f"\n=== {method} {short_url} ===")
    body = e["request"].get("postData", {})
    params = body.get("params", [])
    text = body.get("text", "")
    if params:
        for p in params:
            print(f"  {p['name']}={p['value']!r}")
    elif text:
        parsed = urllib.parse.parse_qs(text, keep_blank_values=True)
        for k, vs in sorted(parsed.items()):
            print(f"  {k}={vs[0]!r}")
    if "/slots" in url:
        resp = e.get("response", {}).get("content", {}).get("text", "")
        print(f"  RESPONSE: {resp[:600]}")
