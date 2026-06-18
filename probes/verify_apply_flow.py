"""
verify_apply_flow.py — End-to-end test: signal → (302 re-warm) → apply → PDF.

Covers the exact path that runs in production when a slot signal fires:
  1. Load or create a warmed primp session for one test account
  2. Check Schedule.jsp status — log 200 (warm) or 302 (form expired)
  3. If --force-expire: sleep past form TTL (~7 min) then re-check (expect 302)
  4. Call _ensure_form_state() — should re-warm if 302, confirm 200 after
  5. POST /slots with CAPTCHA at posto 2032
  6. If slots found: POST SubmeterVistoCriaPDF → GET MostrarPdf → save PDF
  7. Print outcome summary

Usage:
  uv run python -m engine.verify_apply_flow --account silsou1254
  uv run python -m engine.verify_apply_flow --account silsou1254 --force-expire
  uv run python -m engine.verify_apply_flow --account silsou1254 --skip-login
"""

import argparse
import asyncio
import csv
import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

# sys.path bootstrap — requires project root on sys.path (run as `python -m probes.verify_apply_flow`
# or with the root on PYTHONPATH) so `import app` resolves and adds app/core to sys.path.
import app  # noqa: F401

_ROOT       = Path(__file__).parent.parent
_CORE_DIR   = _ROOT / "app" / "core"
_SESS_DIR   = _CORE_DIR / "sessions"
_PDF_DIR    = _CORE_DIR / "data" / "pdfs"
_SOAX_FILE  = _CORE_DIR / "data" / "proxies_soax.txt"

SCHED_JSP   = "https://pedidodevistos.mne.gov.pt/VistosOnline/Schedule.jsp"
POSTO_TEST  = "2032"
FORM_TTL    = 7 * 60   # seconds to sleep to expire form state (~5-7 min actual)


def _print(msg: str) -> None:
    ts = time.strftime("%H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


_proxy_pool = None

def _next_proxy() -> str:
    global _proxy_pool
    from proxy_pool import PersistentProxyPool
    if _proxy_pool is None:
        _proxy_pool = PersistentProxyPool(_SOAX_FILE)
    return _proxy_pool.advance()


def _load_account(username: str) -> dict:
    acct_file = _AUTO_API / "data" / "test_accounts.csv"
    rows = list(csv.DictReader(acct_file.read_text(encoding="utf-8").splitlines()))
    acct = next((r for r in rows if r["username"] == username), None)
    if not acct:
        sys.exit(f"Account '{username}' not found in test_accounts.csv")
    return acct


def _load_env() -> dict:
    env_file = _AUTO_API / ".env"
    env: dict = {}
    if not env_file.exists():
        return env
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        env[k.strip()] = v.strip()
    return env


async def _do_login(acct: dict, executor: ThreadPoolExecutor, solver_keys: dict) -> tuple:
    """Login + return (PlaywrightSession, proxy). Raises on failure."""
    import session as sess

    loop = asyncio.get_event_loop()

    for attempt in range(8):
        proxy = _next_proxy()
        s = None
        try:
            _print(f"  get_session (attempt {attempt+1}) proxy=...{proxy[-20:]}")
            s = await loop.run_in_executor(executor, sess.get_session, proxy)
            result = await loop.run_in_executor(executor, lambda: s.browser_login(
                acct["username"], acct["password"],
                capsolver_keys=solver_keys.get("capsolver", []),
                anticaptcha_keys=solver_keys.get("anticaptcha", []),
                twocaptcha_keys=solver_keys.get("twocaptcha", []),
                capmonster_keys=solver_keys.get("capmonster", []),
                skip_checkbox=True, min_score=50, timeout=300,
            ))
            body = (result.get("body") or "").strip()
            if not body or body.startswith("<"):
                raise ValueError(f"non-JSON body len={len(body)}")
            resp = json.loads(body)
            rtype = resp.get("type", "")
            if rtype in ("", "200", "success"):
                _print(f"  login OK (attempt {attempt+1})")
                kept = s
                s = None  # prevent finally from closing it
                return kept, proxy
            _print(f"  login rejected: {resp.get('description','')[:80]}")
        except Exception as e:
            _print(f"  attempt {attempt+1} failed: {e}")
        finally:
            if s is not None:
                try:
                    s.close()
                except Exception:
                    pass

    raise RuntimeError("login exhausted after 8 attempts")


async def main_async(args: argparse.Namespace) -> None:
    import session as sess
    import session_store
    from batch_apply import _run_steps_2_to_6, apply_warmup, apply_book
    import solver as solvermod

    env          = _load_env()
    acct         = _load_account(args.account)
    username     = acct["username"]
    nat          = acct.get("nationality", "CPV")
    res          = acct.get("residence") or nat
    executor     = ThreadPoolExecutor(max_workers=8, thread_name_prefix="verify")
    loop         = asyncio.get_event_loop()

    def _keys(k: str) -> list[str]:
        return [x.strip() for x in env.get(k, "").split(",") if x.strip()]

    solver_keys = {
        "capsolver":   _keys("CAPSOLVER_KEYS"),
        "anticaptcha": _keys("ANTICAPTCHA_KEYS"),
        "twocaptcha":  _keys("TWOCAPTCHA_KEYS"),
        "capmonster":  _keys("CAPMONSTER_KEYS"),
    }

    client       = None
    proxy        = None
    posto_pdf    = POSTO_TEST
    sched_url    = SCHED_JSP + "?posto_id=" + POSTO_TEST

    # ── Step 1: load or create warmed session ──────────────────────────────────

    sess_file = _SESS_DIR / f"{username}.json"

    if args.skip_login and sess_file.exists():
        _print(f"[1] Loading saved session for {username}")
        meta = json.loads(sess_file.read_text(encoding="utf-8"))
        proxy = _next_proxy()
        import primp
        client = primp.Client(impersonate=sess.IMPERSONATE, proxy=proxy,
                              follow_redirects=True, verify=True)
        client.set_cookies(session_store.COOKIES_URL, meta.get("cookies", {}))
        posto_pdf = meta.get("posto_pdf", POSTO_TEST)
        sched_url = SCHED_JSP + "?posto_id=" + meta.get("posto_id", POSTO_TEST)
        alive = await loop.run_in_executor(executor, session_store.is_alive, client)
        if alive:
            _print(f"  session alive — skipping login+warmup")
        else:
            _print(f"  session dead — will do full login+warmup")
            client = None

    if client is None:
        _print(f"[1] Logging in as {username}")
        browser_sess, proxy = await _do_login(acct, executor, solver_keys)
        _print(f"[1] Warming up (steps 1-6) ...")
        warmup_result = await apply_warmup(
            acct, POSTO_TEST,
            solver_keys.get("capsolver", []),
            solver_keys.get("anticaptcha", []),
            solver_keys.get("twocaptcha", []),
            solver_keys.get("capmonster", []),
            executor,
            nationality=nat, residence=res,
            client=browser_sess, proxy=proxy,
        )
        if isinstance(warmup_result, str):
            sys.exit(f"Warmup failed: {warmup_result}")
        posto_pdf = warmup_result["posto_pdf"]
        sched_url = warmup_result.get("sched_url", sched_url)
        _print(f"  warmup done: posto_pdf={posto_pdf}")
        # Keep browser alive for browser_fetch on /slots (DataDome only passes real browser).
        # Use inner primp client for keepalive GETs.
        client = browser_sess.client
        _print(f"  browser kept alive — primp extracted for GETs, browser_fetch for /slots")

    # ── Step 2: check Schedule.jsp status ─────────────────────────────────────

    _print(f"[2] GET Schedule.jsp (form state check)")
    r = None
    for _attempt in range(3):
        try:
            r = await loop.run_in_executor(executor, lambda: client.get(
                sched_url, headers=sess.HEADERS_NAV, timeout=15, follow_redirects=False))
            break
        except Exception as _e:
            _print(f"  GET attempt {_attempt+1} failed: {_e!r} — retrying in 3s")
            await asyncio.sleep(3)
    if r is None:
        sys.exit("Step [2] GET failed after 3 attempts")
    _print(f"  status={r.status_code}  url={r.url}")

    # ── Step 3: optionally expire form state ───────────────────────────────────

    if args.force_expire:
        _print(f"[3] --force-expire: sleeping {FORM_TTL}s to expire form state ...")
        await asyncio.sleep(FORM_TTL)
        r2 = await loop.run_in_executor(executor, lambda: client.get(
            sched_url, headers=sess.HEADERS_NAV, timeout=15, follow_redirects=False))
        _print(f"  after sleep: status={r2.status_code}")
        if r2.status_code != 302:
            _print(f"  WARNING: expected 302 after {FORM_TTL}s but got {r2.status_code}")
    else:
        _print(f"[3] --force-expire not set — skipping expiry wait")

    # ── Step 4: _ensure_form_state equivalent (inline) ────────────────────────

    _print(f"[4] Ensuring form state (re-warm if 302)")
    r3 = await loop.run_in_executor(executor, lambda: client.get(
        sched_url, headers=sess.HEADERS_NAV, timeout=15, follow_redirects=False))
    _print(f"  pre-ensure status={r3.status_code}")

    form_ready = False
    if r3.status_code == 200:
        _print(f"  form state OK (200) — no re-warm needed")
        form_ready = True
    elif r3.status_code >= 500:
        _print(f"  portal down ({r3.status_code}) — cannot proceed")
    else:
        alive = await loop.run_in_executor(executor, session_store.is_alive, client)
        if not alive:
            _print(f"  session dead — cannot re-warm without full login (re-run without --skip-login)")
        else:
            _print(f"  session alive, form state expired — re-warming steps 2-6 (primp only)")
            t0 = time.time()
            try:
                info = await _run_steps_2_to_6(client, POSTO_TEST, acct, nat, res, executor)
                elapsed = time.time() - t0
                _print(f"  re-warm done in {elapsed:.1f}s: posto_pdf={info.get('posto_pdf')}")
                posto_pdf = info.get("posto_pdf", posto_pdf)
                sched_url = info.get("sched_url", sched_url)
                # Confirm form state is now 200
                r4 = await loop.run_in_executor(executor, lambda: client.get(
                    sched_url, headers=sess.HEADERS_NAV, timeout=15, follow_redirects=False))
                _print(f"  post-rewarm status={r4.status_code}")
                form_ready = (r4.status_code == 200)
                if not form_ready:
                    _print(f"  WARNING: form state still {r4.status_code} after re-warm")
            except Exception as e:
                _print(f"  re-warm failed: {e}")

    if not form_ready:
        _print(f"\nRESULT: form state unrecoverable — booking skipped")
        return

    # ── Step 5: CAPTCHA + POST /slots ─────────────────────────────────────────

    _print(f"[5] Solving CAPTCHA for SCHEDULE_EVISA ...")
    try:
        token = await loop.run_in_executor(executor, lambda: solvermod.race_all(
            solver_keys.get("capsolver", []),
            solver_keys.get("anticaptcha", []),
            solver_keys.get("twocaptcha", []),
            solver_keys.get("capmonster", []),
            "SCHEDULE_EVISA", proxy, min_score=50,
        ))
        _print(f"  CAPTCHA solved (token len={len(token)})")
    except Exception as e:
        _print(f"  CAPTCHA failed: {e}")
        _print(f"\nRESULT: captcha_failed")
        return

    import session as sess2
    _print(f"[5] POST /slots?posto_id={POSTO_TEST}")
    SLOTS_URL = "https://pedidodevistos.mne.gov.pt/VistosOnline/slots"
    if not args.skip_login and hasattr(browser_sess, "browser_fetch"):
        _print(f"  using browser_fetch (browser alive, DataDome clearance)")
        raw = await loop.run_in_executor(executor, lambda: browser_sess.browser_fetch(
            SLOTS_URL, {"posto_id": POSTO_TEST, "captcha": token},
            params={"posto_id": POSTO_TEST},
            headers={"X-Requested-With": "XMLHttpRequest", "Referer": sched_url},
            timeout=30))
        slots_text = raw["body"]
        slots_status = raw["status"]
    else:
        _print(f"  using primp POST (no browser / --skip-login)")
        r_slots = await loop.run_in_executor(executor, lambda: client.post(
            SLOTS_URL, params={"posto_id": POSTO_TEST},
            data={"posto_id": POSTO_TEST, "captcha": token},
            headers={**sess2.HEADERS_XHR, "Referer": sched_url}, timeout=30))
        slots_text = r_slots.text
        slots_status = r_slots.status_code
    _print(f"  /slots status={slots_status}  body_len={len(slots_text)}")
    _print(f"  /slots body: {slots_text[:400]}")

    try:
        slots_json = json.loads(slots_text)
    except Exception:
        _print(f"\nRESULT: /slots non-JSON response — error")
        return

    # Normalize
    if isinstance(slots_json, dict):
        data = slots_json.get("data") or {}
        if not data:
            _print(f"\nRESULT: no_slot (empty data)")
            return
        slots = [
            {"date": d, "periods": [{"id": p} if not isinstance(p, dict) else p
                                     for p in (ps if isinstance(ps, list) else [ps])]}
            for d, ps in data.items()
        ]
    elif isinstance(slots_json, list):
        slots = slots_json
    else:
        _print(f"\nRESULT: unexpected /slots format")
        return

    if not slots:
        _print(f"\nRESULT: no_slot")
        return

    _print(f"  {len(slots)} slot date(s) found: {[s['date'] for s in slots[:3]]}")

    # ── Step 6: POST SubmeterVistoCriaPDF ─────────────────────────────────────

    first      = slots[0]
    slot_date  = first["date"]
    periods    = first.get("periods", [])
    p0         = periods[0] if periods else {}
    slot_period = str(p0.get("id", p0) if isinstance(p0, dict) else p0)

    _print(f"[6] Booking slot: date={slot_date} period={slot_period} posto_pdf={posto_pdf}")
    SUBMIT_URL = "https://pedidodevistos.mne.gov.pt/VistosOnline/SubmeterVistoCriaPDF"
    r_submit = await loop.run_in_executor(executor, lambda: client.post(
        SUBMIT_URL, params={"posto_id": posto_pdf},
        data={"lang": "ENG", "txtHuman": "", "back": "",
              "f_date_c": slot_date, "cmbPeriodo": slot_period},
        headers={**sess2.HEADERS_XHR, "Referer": sched_url}, timeout=30))
    _print(f"  SubmeterVisto status={r_submit.status_code}  body={r_submit.text[:200]}")

    resp_text = r_submit.text.strip()
    _known_errors = ("indisponivel", "unavailable", "erro", "error", "bd_problm")
    has_error = any(kw in resp_text.lower() for kw in _known_errors)
    looks_ok  = (
        "PDF" in resp_text
        or "comprovativo" in resp_text.lower()
        or "agendamento" in resp_text.lower()
        or (len(resp_text) > 100 and not has_error)
    )

    if r_submit.status_code != 200 or not looks_ok:
        _print(f"\nRESULT: booking failed — status={r_submit.status_code}")
        return

    # ── Step 7: GET MostrarPdf ────────────────────────────────────────────────

    _print(f"[7] GET MostrarPdf ...")
    PDF_URL = "https://pedidodevistos.mne.gov.pt/VistosOnline/MostrarPdf?"
    r_pdf = await loop.run_in_executor(executor, lambda: client.get(
        PDF_URL, headers={**sess2.HEADERS_NAV, "Referer": sched_url}, timeout=30))
    ct = (getattr(r_pdf, "headers", {}) or {}).get("content-type", "")
    _print(f"  MostrarPdf status={r_pdf.status_code}  content-type={ct}  "
           f"len={len(getattr(r_pdf, 'content', r_pdf.text or ''))}")

    pdf_bytes = getattr(r_pdf, "content", None) or (r_pdf.text or "").encode()
    if r_pdf.status_code == 200 and ("pdf" in ct or len(pdf_bytes) > 1000):
        _PDF_DIR.mkdir(parents=True, exist_ok=True)
        ts = time.strftime("%Y%m%d_%H%M%S")
        pdf_path = _PDF_DIR / f"{username}_verify_{ts}.pdf"
        pdf_path.write_bytes(pdf_bytes)
        _print(f"  PDF saved: {pdf_path}  ({len(pdf_bytes)} bytes)")
        _print(f"\nRESULT: applied  slot={slot_date}/{slot_period}  pdf={pdf_path.name}")
    else:
        _print(f"  PDF download failed (status={r_pdf.status_code} ct={ct[:60]})")
        _print(f"\nRESULT: applied (booking confirmed) but PDF download failed")


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(description="Verify signal→re-warm→apply→PDF flow")
    parser.add_argument("--account",       required=True, help="Test account username")
    parser.add_argument("--skip-login",    action="store_true", dest="skip_login",
                        help="Load saved session instead of logging in (faster, session may be dead)")
    parser.add_argument("--force-expire",  action="store_true", dest="force_expire",
                        help=f"Sleep {FORM_TTL}s after warmup to expire form state, then test re-warm path")
    args = parser.parse_args()
    asyncio.run(main_async(args))


if __name__ == "__main__":
    main()
