"""
Per-account full apply workflow.

Step sequence (confirmed from HAR manual_full_trace_realdevice.har):
  1. GET  /VistosOnline/                          verify session alive
  2. GET  /VistosOnline/Questionario
  3. GET  /VistosOnline/QuestNextQuestion × N     (steps loaded from data/quest_steps.json)
  4. POST /VistosOnline/Formulario?copy=true      → returns HTML with __RequestVerificationToken
  5. POST /VistosOnline/ScheduleController?posto_id=POSTO   multipart f1-f46 + token
  6. GET  /VistosOnline/Schedule.jsp?posto_id=POSTO          (follow redirect from step 5)
  7. POST /VistosOnline/slots?posto_id=POSTO      captcha → JSON list of available slots
  8. [wait for SlotManager.request_slot()]
  9. POST /VistosOnline/SubmeterVistoCriaPDF?posto_id=POSTO_PDF
         f_date_c=<date>&cmbPeriodo=<period_id>

posto_id notes:
  - POSTO (5086) used in ScheduleController + slots
  - POSTO_PDF (5084) used in SubmeterVistoCriaPDF (from HAR + Schedule.jsp HTML)

Captcha note:
  - slots step uses SCHEDULE_EVISA action (v3 Enterprise)
  - proxy-aware task type for all CAPTCHA solves (ProxyLess is blocked by server)

Usage:
  uv run python batch_apply.py --account <username> --posto 5086
  uv run python batch_apply.py --nationality CPV --count 10 --concurrency 5
"""

import argparse
import asyncio
import csv
import json
import re
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from pathlib import Path

BASE = "https://pedidodevistos.mne.gov.pt"
QUEST_URL   = BASE + "/VistosOnline/Questionario"
QUEST_NEXT  = BASE + "/VistosOnline/QuestNextQuestion"
FORMULARIO  = BASE + "/VistosOnline/Formulario"
SCHED_CTRL  = BASE + "/VistosOnline/ScheduleController"
SCHED_JSP   = BASE + "/VistosOnline/Schedule.jsp"
SLOTS_URL   = BASE + "/VistosOnline/slots"
SUBMIT_URL  = BASE + "/VistosOnline/SubmeterVistoCriaPDF"

_ENV_FILE          = Path(__file__).parent / ".env"
_ACCOUNT_FILE      = Path(__file__).parent / "data" / "accounts.csv"
_QUEST_STEPS_FILE  = Path(__file__).parent / "data" / "quest_steps.json"
_FORM_DEFAULTS_FILE = Path(__file__).parent / "data" / "form_defaults.json"

_csv_lock   = threading.Lock()
_print_lock = threading.Lock()

# ── JSON config loaders ────────────────────────────────────────────────────────

def _load_quest_steps(nationality: str) -> list[dict]:
    """Load questionnaire steps for the given nationality from quest_steps.json."""
    data = json.loads(_QUEST_STEPS_FILE.read_text(encoding="utf-8"))
    steps = data.get(nationality) or data.get("CPV")
    return steps

def _load_form_defaults() -> dict:
    """Load form_defaults.json once; caller merges sections."""
    return json.loads(_FORM_DEFAULTS_FILE.read_text(encoding="utf-8"))

# ── Dynamic date resolver ──────────────────────────────────────────────────────

def _resolve_dynamic(expr: str) -> str:
    """Resolve dynamic date expressions: 'today+30d', 'today-3yr', 'today+5yr'."""
    today = date.today()
    m = re.fullmatch(r"today([+-])(\d+)(d|yr)", expr)
    if not m:
        return ""
    sign, n, unit = m.group(1), int(m.group(2)), m.group(3)
    if unit == "d":
        delta = timedelta(days=n)
    else:
        delta = timedelta(days=n * 365)
    result = today + delta if sign == "+" else today - delta
    return result.strftime("%Y/%m/%d")

# ── Payload builder ────────────────────────────────────────────────────────────

def _build_schedule_payload(acct: dict, posto_id: str, csrf: str,
                             nationality: str) -> dict:
    """Build the ScheduleController multipart payload from form_defaults.json + account data."""
    defaults = _load_form_defaults()

    # 1. Start with static values
    payload: dict[str, str] = dict(defaults["static"])

    # 2. Apply dynamic defaults (date expressions)
    for field, expr in defaults["dynamic"].items():
        payload[field] = _resolve_dynamic(expr)

    # 3. Override with from_account values (account CSV columns win over dynamic)
    gender_raw = acct.get("gender", "M").upper()
    gender_expanded = "MALE" if gender_raw in ("M", "MALE") else "FEMALE"
    account_map = {
        "email":           acct.get("email", ""),
        "last_name":       acct.get("last_name", ""),
        "first_name":      acct.get("first_name", ""),
        "birthdate":       acct.get("birthdate", ""),
        "gender_expanded": gender_expanded,
        "traveldoc":       acct.get("traveldoc", ""),
        "passport_issued": acct.get("passport_issued", "").strip(),
        "passport_expiry": acct.get("passport_expiry", "").strip(),
    }
    for field, col in defaults["from_account"].items():
        val = account_map.get(col, "")
        if val:  # non-empty account value wins over dynamic default
            payload[field] = val

    # 4. Apply nationality overrides
    nat = nationality or acct.get("nationality", "CPV")
    for field, tmpl in defaults["from_nationality"].items():
        payload[field] = tmpl.replace("{nat}", nat)

    # 5. Runtime injections
    payload["f0sf1"]                      = posto_id
    payload["__RequestVerificationToken"] = csrf

    return payload


def _log(account: str, msg: str) -> None:
    with _print_lock:
        print(f"[{account}] {msg}", flush=True)


def _load_dotenv() -> dict:
    env = {}
    if not _ENV_FILE.exists():
        return env
    for line in _ENV_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        env[k.strip()] = v.strip()
    return env


def _load_account(username: str) -> dict | None:
    if not _ACCOUNT_FILE.exists():
        return None
    rows = list(csv.DictReader(_ACCOUNT_FILE.read_text(encoding="utf-8").splitlines()))
    return next((r for r in rows if r["username"] == username), None)


def _csv_update(username: str, **fields) -> None:
    with _csv_lock:
        from account_pool import AccountPool
        pool = AccountPool(_ACCOUNT_FILE)
        pool.update(username, **fields)


def _extract_csrf(html: str) -> str | None:
    m = re.search(
        r'name=["\']__RequestVerificationToken["\'][^>]*value=["\']([^"\']+)["\']',
        html, re.I
    )
    if m:
        return m.group(1)
    m = re.search(
        r'value=["\']([^"\']+)["\'][^>]*name=["\']__RequestVerificationToken["\']',
        html, re.I
    )
    return m.group(1) if m else None


async def apply_one(acct: dict, posto_id: str,
                    slot_manager,  # SlotManager | None
                    capsolver_keys: list[str], anticaptcha_keys: list[str],
                    twocaptcha_keys: list[str], capmonster_keys: list[str],
                    executor: ThreadPoolExecutor,
                    nationality: str = "CPV",
                    client=None,   # pre-warmed primp Client | None
                    proxy: str | None = None) -> str:
    """
    Run the full apply workflow for one account.
    Returns: "applied" | "no_slot" | "error" | "captcha_failed" | "session_dead"
    """
    username = acct["username"]
    loop = asyncio.get_event_loop()

    import session as sess
    import solver as solvermod
    import session_store

    # Resolve effective nationality: per-account value wins over run-level flag
    nat = acct.get("nationality", "").strip() or nationality

    # Load questionnaire steps for this nationality
    quest_steps = _load_quest_steps(nat)

    # Use pre-warmed client or create a fresh one
    if client is None:
        loaded = await loop.run_in_executor(executor, session_store.load, username)
        if loaded:
            client, proxy = loaded
            alive = await loop.run_in_executor(executor, session_store.is_alive, client)
            if not alive:
                _log(username, "session expired — re-logging in")
                client = None
        if client is None:
            try:
                client = await loop.run_in_executor(executor, sess.get_session, proxy)
            except Exception as e:
                _log(username, f"get_session failed: {e}")
                return "error"

    def _get(url, **kw):
        return client.get(url, headers=sess.HEADERS_NAV, timeout=20, **kw)

    def _post(url, **kw):
        return client.post(url, headers=sess.HEADERS_XHR, timeout=20, **kw)

    # HAR-matched header sets for each step
    _HDR_QUEST_NEXT = {                      # cors XHR (not navigate)
        **sess.HEADERS_XHR,
        "Accept":        "text/plain, */*; q=0.01",
        "Referer":       QUEST_URL,
        "Sec-Fetch-Dest": "empty",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "same-origin",
    }
    _HDR_FORMULARIO = {                      # navigate (browser form submit)
        **sess.HEADERS_NAV,
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        "Origin":        BASE,
        "Referer":       QUEST_URL,
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "same-origin",
    }
    _HDR_SCHED_CTRL = {                      # navigate (browser form submit)
        **sess.HEADERS_NAV,
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        "Origin":        BASE,
        "Referer":       FORMULARIO + "?copy=true",
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "same-origin",
    }

    try:
        # Step 1: Verify alive
        _log(username, "GET /VistosOnline/")
        r = await loop.run_in_executor(executor, lambda: client.get(
            session_store.COOKIES_URL, headers=sess.HEADERS_NAV, timeout=15, follow_redirects=False))
        if r.status_code == 302:
            return "session_dead"

        # Step 2: Questionnaire
        _log(username, "GET /Questionario")
        await loop.run_in_executor(executor, lambda: _get(QUEST_URL))

        # Step 3: Questionnaire steps (HAR: cors mode, Referer: /Questionario)
        base_qs = {"lang": "ENG", "nacionalidade": nat}
        for step in quest_steps:
            params = {**base_qs, **step}
            await loop.run_in_executor(executor, lambda p=params: client.get(
                QUEST_NEXT, params=p, headers=_HDR_QUEST_NEXT, timeout=20))
        _log(username, f"questionnaire: {len(quest_steps)} steps done (nat={nat})")

        # Step 4: Formulario → get CSRF token (HAR: navigate mode, not XHR)
        formulario_payload = {
            "lang":                   "ENG",
            "nacionalidade":          nat,
            "pais_residencia":        nat,
            "tipo_passaporte":        "01",
            "copia_pedido":           "",
            "cb_pais_residencia":     nat,
            "cb_tipo_passaporte":     "01",
            "cb_qt_dias":             "SCH",
            "cb_trab_sazonal":        "O",
            "cb_motivo_estada_sch":   "10",
            "cb_viaja_reune_turismo": "FAM_N",
            "tipo_visto":             "C",
            "tipo_visto_desc":        "SHORT STAY VISA (SCHENGEN)",
            "class_visto":            "SCH",
            "cod_estada":             "10",
            "id_visto_doc":           "36",
        }
        _log(username, "POST /Formulario")
        r = await loop.run_in_executor(executor, lambda: client.post(
            FORMULARIO, params={"copy": "true"},
            data=formulario_payload,
            headers=_HDR_FORMULARIO, timeout=30))
        csrf = _extract_csrf(r.text)
        if not csrf:
            _log(username, f"WARN: CSRF token not found in Formulario (HTTP {r.status_code}, len={len(r.text)})")
            _log(username, f"  first 300: {r.text[:300]}")
            csrf = ""
        else:
            _log(username, f"CSRF extracted (len={len(csrf)})")

        # Step 5: ScheduleController (HAR: navigate mode, Referer: /Formulario?copy=true)
        payload = _build_schedule_payload(acct, posto_id, csrf, nat)
        _log(username, f"POST /ScheduleController?posto_id={posto_id}")
        r = await loop.run_in_executor(executor, lambda: client.post(
            SCHED_CTRL, params={"posto_id": posto_id},
            data=payload,
            headers=_HDR_SCHED_CTRL,
            timeout=30, follow_redirects=True))
        _log(username, f"ScheduleController → {r.status_code}")

        # Step 6: Schedule.jsp (already followed by redirect above)
        sched_jsp_url = SCHED_JSP + "?posto_id=" + posto_id
        if "Schedule.jsp" not in r.url:
            _log(username, f"GET /Schedule.jsp?posto_id={posto_id}")
            r = await loop.run_in_executor(executor, lambda: client.get(
                SCHED_JSP, params={"posto_id": posto_id},
                headers={**sess.HEADERS_NAV, "Referer": SCHED_CTRL + "?posto_id=" + posto_id},
                timeout=20))
        _log(username, f"Schedule.jsp HTTP {r.status_code}")

        # Step 7: POST /slots with CAPTCHA (HAR: cors mode, Referer: Schedule.jsp)
        _log(username, f"CAPTCHA solve for SCHEDULE_EVISA (proxy={proxy})")
        try:
            token = await loop.run_in_executor(
                executor, solvermod.race_all,
                capsolver_keys, anticaptcha_keys, twocaptcha_keys, capmonster_keys,
                "SCHEDULE_EVISA", proxy,
            )
        except Exception as e:
            _log(username, f"CAPTCHA failed: {e}")
            return "captcha_failed"

        _log(username, f"POST /slots?posto_id={posto_id}")
        r = await loop.run_in_executor(executor, lambda: client.post(
            SLOTS_URL, params={"posto_id": posto_id},
            data={"posto_id": posto_id, "captcha": token},
            headers={**sess.HEADERS_XHR, "Referer": sched_jsp_url}, timeout=30))

        try:
            slots_json = r.json()
        except Exception:
            _log(username, f"slots: non-JSON response: {r.text[:200]}")
            return "error"

        if not slots_json:
            _log(username, "slots: empty — no appointments available")
            return "no_slot"

        _log(username, f"slots: {len(slots_json)} dates available")

        # Step 8: Get assigned slot from SlotManager (if coordinator provided)
        visible_slots = slots_json
        lease = None
        if slot_manager:
            slot_manager.update_pool(slots_json)
            lease = slot_manager.request_slot(username, visible_slots)
            if not lease:
                _log(username, "no slot assigned (all leased/taken)")
                return "no_slot"
            slot_date = lease.date
            slot_period = lease.period_id
        else:
            # No coordinator — pick first available slot
            first = slots_json[0]
            slot_date = first["date"]
            slot_period = str(first["periods"][0]["id"])

        _log(username, f"booking slot: date={slot_date} period={slot_period}")

        # Step 9: SubmeterVistoCriaPDF (HAR: XHR cors, Referer: Schedule.jsp)
        r = await loop.run_in_executor(executor, lambda: client.post(
            SUBMIT_URL, params={"posto_id": posto_id},
            data={"lang": "ENG", "txtHuman": "", "back": "",
                  "f_date_c": slot_date, "cmbPeriodo": slot_period},
            headers={**sess.HEADERS_XHR, "Referer": sched_jsp_url}, timeout=30))

        resp_text = r.text.strip()
        _log(username, f"SubmeterVisto → {r.status_code}  body={resp_text[:150]}")

        if r.status_code == 200 and (
            "PDF" in resp_text or "comprovativo" in resp_text.lower()
            or "agendamento" in resp_text.lower()
        ):
            if lease:
                slot_manager.confirm(lease)
            _csv_update(username, status="applied",
                        appointment_ref=f"{slot_date}_{slot_period}",
                        notes=f"booked:{slot_date} period:{slot_period}")
            _log(username, f"APPLIED  slot={slot_date} period={slot_period}")
            return "applied"

        # Check if slot was already taken
        if "indisponivel" in resp_text.lower() or "unavailable" in resp_text.lower():
            if lease:
                slot_manager.release(lease, "already_taken")
            _log(username, "slot already taken — release and retry later")
            return "no_slot"

        if lease:
            slot_manager.release(lease, f"http_{r.status_code}")
        _log(username, f"SubmeterVisto unexpected response: {resp_text[:200]}")
        return "error"

    except Exception as e:
        _log(username, f"EXCEPTION in apply_one: {e}")
        return "error"


async def main_async(args: argparse.Namespace,
                     capsolver_keys: list[str], anticaptcha_keys: list[str],
                     twocaptcha_keys: list[str], capmonster_keys: list[str]) -> None:
    from account_pool import AccountPool

    pool = AccountPool(_ACCOUNT_FILE)
    if args.account:
        acct = pool.get_by_username(args.account)
        if not acct:
            print(f"[batch_apply] account '{args.account}' not found")
            return
        accounts = [acct]
    else:
        accounts = [a for a in pool.all() if a["status"] == "active"]
        if args.count > 0:
            accounts = accounts[:args.count]

    if not accounts:
        print("[batch_apply] no active accounts to apply")
        return

    posto_id = args.posto
    nat      = args.nationality

    print(f"\n[batch_apply] {len(accounts)} accounts  posto={posto_id}"
          f"  nationality={nat}  concurrency={args.concurrency}")

    sem      = asyncio.Semaphore(args.concurrency)
    executor = ThreadPoolExecutor(max_workers=args.concurrency + 4)

    async def bounded(acct):
        async with sem:
            return await apply_one(
                acct, posto_id,
                None,   # no slot manager in standalone mode
                capsolver_keys, anticaptcha_keys, twocaptcha_keys, capmonster_keys,
                executor,
                nationality=nat,
            )

    results = await asyncio.gather(*[bounded(a) for a in accounts], return_exceptions=True)
    counts: dict[str, int] = {}
    for r in results:
        k = str(r) if isinstance(r, Exception) else r
        counts[k] = counts.get(k, 0) + 1

    print("\n" + "="*50)
    print("[batch_apply] SUMMARY")
    for k, v in sorted(counts.items()):
        print(f"  {k:<16}: {v}")
    print("="*50)


def main() -> None:
    import sys
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    env = _load_dotenv()

    parser = argparse.ArgumentParser(description="Full apply workflow for active accounts")
    parser.add_argument("--account",      default="", help="Single account username")
    parser.add_argument("--count",        type=int, default=0)
    parser.add_argument("--concurrency",  type=int, default=5)
    parser.add_argument("--posto",        default=env.get("POSTO_ID", "5086"),
                        help="Consular post ID (ScheduleController + slots)")
    parser.add_argument("--nationality",  default=env.get("NATIONALITY", "CPV"),
                        help="ISO 3166-1 alpha-3 nationality code (default: NATIONALITY from .env)")
    args = parser.parse_args()

    def _keys(k: str) -> list[str]:
        return [x.strip() for x in env.get(k, "").split(",") if x.strip()]

    capsolver_keys   = _keys("CAPSOLVER_KEYS")
    anticaptcha_keys = _keys("ANTICAPTCHA_KEYS")
    twocaptcha_keys  = _keys("TWOCAPTCHA_KEYS")
    capmonster_keys  = _keys("CAPMONSTER_KEYS")

    if not any([capsolver_keys, anticaptcha_keys, twocaptcha_keys, capmonster_keys]):
        print("ERROR: no CAPTCHA solver keys in .env")
        sys.exit(1)

    asyncio.run(main_async(args, capsolver_keys, anticaptcha_keys, twocaptcha_keys, capmonster_keys))


if __name__ == "__main__":
    main()
