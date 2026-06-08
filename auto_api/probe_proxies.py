"""
Probe each ISP proxy to find ones that still have CAPTCHA attempts remaining.

For each proxy:
  1. Get session (seeds Vistos_sid)
  2. Solve CAPTCHA with capsolver proxyless (fast/cheap — we only need a token to probe)
  3. POST /register
  4. Parse response:
     - "0 tentativas" / "0 vezes" / "bloqueado" / "blocked"  → proxy locked, skip
     - "ReCaptchaError" but NOT locked                        → proxy has attempts, stop
     - type=200 or already_exists                             → clean proxy + success/exists

Uses account hugaze1523 (new, never registered) to probe.
Token will likely be rejected (proxyless = lower score) but that's fine —
we're reading the counter, not trying to register yet.
"""

import json
import time
import sys
from pathlib import Path

import session as sess
import solver as solvermod
from email_pool import MailTM

BASE         = "https://pedidodevistos.mne.gov.pt"
REGISTER_URL = BASE + "/VistosOnline/register"

CAPSOLVER_KEY = "CAP-D09F9ADF91258845DBD724E3D8C3069F0E4AFD8666D9260F44D34891884DA409"

# All proxies from proxies_isp.txt
PROXIES = [
    "http://WHnPcVTJPqBz:le6VhGXHYF0UPR@82.23.148.76:6666",
    "http://WHnPcVTJPqBz:le6VhGXHYF0UPR@82.22.176.16:6666",
    "http://WHnPcVTJPqBz:le6VhGXHYF0UPR@82.22.176.65:6666",
    "http://WHnPcVTJPqBz:le6VhGXHYF0UPR@96.62.28.71:6666",
    "http://WHnPcVTJPqBz:le6VhGXHYF0UPR@96.62.28.127:6666",
    "http://WHnPcVTJPqBz:le6VhGXHYF0UPR@96.62.28.238:6666",
    "http://WHnPcVTJPqBz:le6VhGXHYF0UPR@149.87.144.233:6666",
    "http://WHnPcVTJPqBz:le6VhGXHYF0UPR@149.87.144.97:6666",
    "http://WHnPcVTJPqBz:le6VhGXHYF0UPR@149.87.144.247:6666",
    "http://WHnPcVTJPqBz:le6VhGXHYF0UPR@88.216.245.109:6666",
    "http://WHnPcVTJPqBz:le6VhGXHYF0UPR@178.93.228.70:6666",
    "http://WHnPcVTJPqBz:le6VhGXHYF0UPR@156.228.62.162:6666",
    "http://WHnPcVTJPqBz:le6VhGXHYF0UPR@95.134.150.4:6666",
    "http://WHnPcVTJPqBz:le6VhGXHYF0UPR@31.59.233.161:6666",
    "http://WHnPcVTJPqBz:le6VhGXHYF0UPR@95.134.150.217:6666",
    "http://WHnPcVTJPqBz:le6VhGXHYF0UPR@38.213.155.221:6666",
    "http://user_b7e7aae946b7:TurgzPn9@51.146.145.238:61234",
    "http://user_f6f7c25e621c:K4081MW4@51.146.54.25:61234",
    "http://user_1c39d4e5cd15:ArWtvmVq@51.146.55.253:61234",
    "http://user_1e3ebf76906e:QbHU6oXK@51.194.123.99:61234",
    "http://user_f4a6ca60106a:Ow8ytUPZ@51.194.89.167:61234",
]

# Account to probe with (new/unregistered)
ACCOUNT = {
    "username":    "hugaze1523",
    "password":    "8u8vp21n9okOg!5!",
    "first_name":  "Hugo",
    "last_name":   "Azevedo",
    "gender":      "M",
    "birthdate":   "1973/08/19",
    "nationality": "MOZ",
    "traveldoc":   "RZ595170",
    "lang":        "PT",
}


def _safe(s: str) -> str:
    return s.encode("ascii", errors="replace").decode("ascii")


def _proxy_host(proxy_url: str) -> str:
    return proxy_url.split("@")[-1]


def _classify_response(reg_type: str, desc: str) -> str:
    """
    Returns one of:
      'locked'   — proxy has 0 CAPTCHA attempts remaining
      'attempt'  — proxy had attempts; one was consumed (token rejected)
      'success'  — registration actually succeeded
      'exists'   — account already exists (counts as clean proxy)
      'error'    — non-CAPTCHA error (server error, secblock, etc.)
    """
    desc_lower = desc.lower()

    if reg_type == "200":
        return "success"

    if "already_exists" in reg_type or "already_exists" in desc_lower:
        return "exists"

    if reg_type == "secblock":
        return "error"

    if reg_type == "ReCaptchaError":
        # Locked messages contain explicit "0" remaining or "bloqueado"/"blocked"
        locked_signals = [
            "0 tentativas", "0 vezes", "bloqueado", "blocked",
            "acesso bloqueado", "acesso ? plataforma",
            # Server appends remaining count; "2 vezes" alone = first failure message
            # but if we also see something indicating 0 left:
            "nenhuma tentativa", "sem tentativas",
        ]
        for sig in locked_signals:
            if sig in desc_lower:
                return "locked"
        # If the description says "apenas permite falhar... 2 vezes" without a 0-count
        # signal, it's the standard rejection message — proxy has attempts remaining.
        if "2 vezes" in desc_lower or "recaptcha" in desc_lower:
            return "attempt"
        return "attempt"

    # Any other error type
    return "error"


def probe_proxy(proxy: str, email_addr: str, email_pass: str) -> tuple[str, str]:
    """
    Probe a single proxy. Returns (status, description).
    status: 'locked' | 'attempt' | 'success' | 'exists' | 'error' | 'session_fail'
    """
    host = _proxy_host(proxy)
    print(f"\n[probe] {host}")

    # Session
    try:
        s = sess.get_session(proxy=proxy)
    except Exception as e:
        print(f"  session error: {_safe(str(e))}")
        return "session_fail", str(e)

    # CAPTCHA (proxyless — fast, cheap, good enough to read the counter)
    try:
        token = solvermod._capsolver(CAPSOLVER_KEY, "REGISTER_EVISA", proxy_info=None)
        print(f"  token_len={len(token)}")
    except Exception as e:
        print(f"  solver error: {_safe(str(e))}")
        return "session_fail", str(e)

    # POST /register
    payload = {
        "name":            ACCOUNT["first_name"],
        "surname":         ACCOUNT["last_name"],
        "username":        ACCOUNT["username"],
        "password":        ACCOUNT["password"],
        "valPassword":     ACCOUNT["password"],
        "gender":          ACCOUNT["gender"],
        "bday":            ACCOUNT["birthdate"],
        "nationality":     ACCOUNT["nationality"],
        "traveldoc":       ACCOUNT["traveldoc"],
        "email":           email_addr,
        "language":        ACCOUNT["lang"],
        "rgpd":            "Y",
        "captchaResponse": token,
    }
    try:
        resp = s.post(REGISTER_URL, data=payload, headers=sess.HEADERS_XHR, timeout=30)
        body = resp.text.strip()
        print(f"  HTTP {resp.status_code}  {_safe(body[:200])}")
    except Exception as e:
        print(f"  POST error: {_safe(str(e))}")
        return "session_fail", str(e)

    try:
        data = json.loads(body)
        reg_type = data.get("type", "")
        desc     = _safe(data.get("description", ""))
    except Exception:
        reg_type = ""
        desc     = _safe(body[:200])

    status = _classify_response(reg_type, desc)
    print(f"  -> {status}")
    return status, desc


def main():
    # Create one email inbox for all probes (reused — if account is never created the
    # email is never sent, so we can safely reuse)
    print("[probe] creating mail.tm inbox for probing...")
    mail = MailTM()
    email_addr, email_pass = mail.create()
    print(f"[probe] email: {email_addr}\n")

    results = []
    clean_proxy = None

    for proxy in PROXIES:
        status, desc = probe_proxy(proxy, email_addr, email_pass)
        results.append((proxy, status))

        if status in ("attempt", "success", "exists"):
            clean_proxy = proxy
            print(f"\n{'='*60}")
            print(f"  CLEAN PROXY FOUND: {_proxy_host(proxy)}")
            print(f"  status={status}")
            print(f"{'='*60}")
            if status == "success":
                print("  Registration succeeded! Account is registered.")
            elif status == "exists":
                print("  Account already exists on server — proxy is clean.")
            else:
                print("  Proxy had attempts remaining (1 attempt consumed).")
                print(f"  Use this proxy for the REAL run with a good CAPTCHA solver.")
            break

        if status == "locked":
            print(f"  skip (locked)")
        elif status in ("error", "session_fail"):
            print(f"  skip ({status})")

        time.sleep(2)

    print(f"\n{'='*60}")
    print("  SUMMARY")
    print(f"{'='*60}")
    for proxy, st in results:
        mark = "OK " if st in ("attempt", "success", "exists") else "   "
        print(f"  {mark}{_proxy_host(proxy):30s}  {st}")

    if not clean_proxy:
        print("\n  No clean proxy found. All proxies appear locked or unreachable.")
        print("  Wait for the rate limit to reset (~24h from last failure on each IP).")
        sys.exit(1)
    else:
        print(f"\n  Best proxy: {_proxy_host(clean_proxy)}")
        print(f"  Run registration with:")
        print(f'  python test_register_api.py --service capsolver --account hugaze1523 \\')
        print(f'    --proxy "{clean_proxy}"')
        sys.exit(0)


if __name__ == "__main__":
    main()
