"""
Quick test: primp with HTTP bridge URL for HTTPS requests through Webshare SOCKS5.
No login needed — just tests the networking layer.

Usage:
  python probes/diag_primp_bridge.py
"""
import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "app" / "core"))

from proxy_bridge import start_bridge
import primp

SOCKS5 = "socks5://Mylist1234-AL-52:Saulo12345@p.webshare.io:80"
BASE   = "https://pedidodevistos.mne.gov.pt"

print(f"[1] Starting HTTP bridge for {SOCKS5}")
bridge_url = start_bridge(SOCKS5)
print(f"[1] Bridge URL: {bridge_url}")

print("[2] Testing primp with bridge URL (impersonate=None)...")
try:
    pc = primp.Client(impersonate=None, proxy=bridge_url, verify=False, follow_redirects=True, timeout=30)
    r = pc.get(BASE + "/VistosOnline/", headers={"Accept": "text/html"})
    print(f"[2] HTTP bridge + impersonate=None: status={r.status_code}  len={len(r.text)}  ok")
except Exception as e:
    print(f"[2] HTTP bridge + impersonate=None: FAILED: {e}")

print("[3] Testing primp with SOCKS5 URL directly (impersonate=None)...")
try:
    pc2 = primp.Client(impersonate=None, proxy=SOCKS5, verify=False, follow_redirects=True, timeout=30)
    r2 = pc2.get(BASE + "/VistosOnline/", headers={"Accept": "text/html"})
    print(f"[3] SOCKS5 direct + impersonate=None: status={r2.status_code}  len={len(r2.text)}  ok")
except Exception as e:
    print(f"[3] SOCKS5 direct + impersonate=None: FAILED: {e}")
