"""
Watch a run log for 'warmup done' and fire POST /signal automatically.
Usage: uv run python -m app.core.probes.auto_signal <log_file> [--port 8989]
"""
import sys, time, urllib.request, argparse

ap = argparse.ArgumentParser()
ap.add_argument("log", help="Path to .log.err file to watch")
ap.add_argument("--port", type=int, default=8989)
ap.add_argument("--delay", type=float, default=2.0, help="Seconds to wait after warmup before firing")
args = ap.parse_args()

print(f"[auto_signal] watching {args.log}  signal_port={args.port}  delay={args.delay}s")

fired = False
with open(args.log, "r", encoding="utf-8", errors="replace") as f:
    f.seek(0, 2)  # start at end
    while True:
        line = f.readline()
        if not line:
            time.sleep(0.2)
            continue
        line = line.rstrip()
        if "warmup done" in line or "awaiting_signal" in line.lower():
            print(f"[auto_signal] TRIGGER: {line}")
            if not fired:
                time.sleep(args.delay)
                try:
                    url = f"http://127.0.0.1:{args.port}/signal"
                    req = urllib.request.Request(url, data=b"{}", method="POST",
                                                 headers={"Content-Type": "application/json"})
                    resp = urllib.request.urlopen(req, timeout=5)
                    print(f"[auto_signal] signal fired -> HTTP {resp.status}")
                    fired = True
                except Exception as e:
                    print(f"[auto_signal] fire error: {e}")
            else:
                print("[auto_signal] signal already fired, skipping")
        if line:
            print(f"  {line}")
