"""Map Webshare country codes to cursor positions and test SOAX."""
import re, socket, base64
from urllib.parse import urlparse
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]

# --- Webshare country map ---
country_map = {}
with open(ROOT / "app/core/data/proxies_webshare.txt") as f:
    for i, line in enumerate(f):
        m = re.search(r"Mylist1234-([A-Z]+)-", line)
        if m:
            c = m.group(1)
            if c not in country_map:
                country_map[c] = {"start": i, "count": 0}
            country_map[c]["count"] += 1

print("Webshare countries:")
for c, v in sorted(country_map.items(), key=lambda x: x[1]["start"]):
    print(f"  {c}: start={v['start']:>6}  count={v['count']:>5}")

# --- SOAX tunnel check ---
print("\nSOAX tunnel test:")
lines = open(ROOT / "app/core/data/proxies_soax.txt").readlines()
for offset in [5000, 10000, 15000]:
    proxy = lines[offset].strip()
    p = urlparse(proxy)
    creds = base64.b64encode(f"{p.username}:{p.password}".encode()).decode()
    try:
        s = socket.create_connection((p.hostname, p.port), timeout=8)
        s.send(f"CONNECT pedidodevistos.mne.gov.pt:443 HTTP/1.1\r\nHost: pedidodevistos.mne.gov.pt:443\r\nProxy-Authorization: Basic {creds}\r\n\r\n".encode())
        resp = s.recv(256).decode()[:80].strip()
        print(f"  offset={offset}: {resp}")
        s.close()
    except Exception as e:
        print(f"  offset={offset}: ERROR {e}")
