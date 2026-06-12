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

_ENV_FILE           = Path(__file__).parent / ".env"
_ACCOUNT_FILE       = Path(__file__).parent / "data" / "accounts.csv"
_QUEST_STEPS_FILE   = Path(__file__).parent / "data" / "quest_steps.json"
_FORM_DEFAULTS_FILE = Path(__file__).parent / "data" / "form_defaults.json"

_csv_lock   = threading.Lock()
_print_lock = threading.Lock()


class SessionExpired(Exception):
    """Raised by poll_slots_once when Vistos_sid is dead."""


# ── JSON config loaders ────────────────────────────────────────────────────────

def _load_quest_steps(nationality: str) -> list[dict]:
    data = json.loads(_QUEST_STEPS_FILE.read_text(encoding="utf-8"))
    steps = data.get(nationality) or data.get("CPV")
    return steps

def _load_form_defaults() -> dict:
    return json.loads(_FORM_DEFAULTS_FILE.read_text(encoding="utf-8"))

# ── Dynamic date resolver ──────────────────────────────────────────────────────

def _resolve_dynamic(expr: str) -> str:
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
                             nationality: str, residence: str = "") -> dict:
    defaults = _load_form_defaults()
    payload: dict[str, str] = dict(defaults["static"])
    for field, expr in defaults["dynamic"].items():
        payload[field] = _resolve_dynamic(expr)
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
        if val:
            payload[field] = val
    nat = nationality or acct.get("nationality", "CPV")
    res = residence or nat
    for field, tmpl in defaults["from_nationality"].items():
        if "pais_residencia" in field or field in ("pais_residencia",):
            payload[field] = tmpl.replace("{nat}", res)
        else:
            payload[field] = tmpl.replace("{nat}", nat)
    payload["pais_residencia"] = res
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


# ── Steps 2-6: questionnaire → Formulario → ScheduleController → Schedule.jsp ─

async def _run_steps_2_to_6(client, posto_id: str, acct: dict,
                              nat: str, res: str,
                              executor: ThreadPoolExecutor) -> dict:
    """
    Execute steps 2-6 on a live primp session.
    Returns {"posto_pdf": str, "sched_url": str}. Raises on failure.
    """
    import session as sess
    import uuid as _uuid

    username   = acct.get("username", "?")
    loop       = asyncio.get_event_loop()
    quest_key  = res if res != nat else nat
    quest_steps = _load_quest_steps(quest_key)

    _HDR_QUEST_NEXT = {
        **sess.HEADERS_XHR,
        "Accept":         "text/plain, */*; q=0.01",
        "Referer":        QUEST_URL,
        "Sec-Fetch-Dest": "empty",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "same-origin",
    }
    _HDR_FORMULARIO = {
        **sess.HEADERS_NAV,
        "Content-Type":   "application/x-www-form-urlencoded; charset=UTF-8",
        "Origin":          BASE,
        "Referer":         QUEST_URL,
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "same-origin",
    }
    _HDR_SCHED_CTRL = {
        **{k: v for k, v in sess.HEADERS_NAV.items() if k.lower() != "content-type"},
        "Origin":                    BASE,
        "Referer":                   FORMULARIO + "?copy=true",
        "Sec-Fetch-Dest":            "document",
        "Sec-Fetch-Mode":            "navigate",
        "Sec-Fetch-Site":            "same-origin",
        "Sec-Fetch-User":            "?1",
        "Upgrade-Insecure-Requests": "1",
    }

    # Step 2
    _log(username, "GET /Questionario")
    await loop.run_in_executor(executor, lambda: client.get(
        QUEST_URL, headers=sess.HEADERS_NAV, timeout=20))

    # Step 3
    base_qs = {"lang": "ENG", "nacionalidade": nat}
    for step in quest_steps:
        params = {**base_qs, **step}
        await loop.run_in_executor(executor, lambda p=params: client.get(
            QUEST_NEXT, params=p, headers=_HDR_QUEST_NEXT, timeout=20))
    _log(username, f"questionnaire: {len(quest_steps)} steps done (nat={nat})")

    # Step 4: Formulario → CSRF token
    formulario_payload = {
        "lang":                   "ENG",
        "nacionalidade":          nat,
        "pais_residencia":        res,
        "tipo_passaporte":        "01",
        "copia_pedido":           "",
        "cb_pais_residencia":     res,
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
    _log(username, f"Formulario HTTP {r.status_code}  url={r.url}  len={len(r.text or '')}")
    _log(username, f"Formulario body snippet: {(r.text or '')[:600]}")
    if not csrf:
        _log(username, "WARN: CSRF token not found")
        csrf = ""
    else:
        _log(username, f"CSRF extracted: {csrf[:20]}...  (len={len(csrf)})")

    # Step 5: ScheduleController (multipart/form-data)
    payload = _build_schedule_payload(acct, posto_id, csrf, nat, residence=res)
    _log(username, f"ScheduleController key fields: f0sf1={payload.get('f0sf1')}  "
         f"f1={payload.get('f1')}  f3={payload.get('f3')}  f4={payload.get('f4')}  "
         f"f14={payload.get('f14')}  nacionalidade={payload.get('nacionalidade')}  "
         f"pais_residencia={payload.get('pais_residencia')}  csrf_len={len(csrf)}")

    _primp_client = getattr(client, 'client', client)
    _boundary  = "----WebKitFormBoundary" + _uuid.uuid4().hex[:16]
    _file_names = {"foto", "file1", "file2", "file3", "file4"}
    _mp_body = b""
    for k, v in payload.items():
        if k in _file_names:
            _mp_body += (
                f"--{_boundary}\r\n"
                f'Content-Disposition: form-data; name="{k}"; filename=""\r\n'
                f"Content-Type: application/octet-stream\r\n\r\n"
            ).encode() + b"\r\n"
        else:
            _mp_body += (
                f"--{_boundary}\r\n"
                f'Content-Disposition: form-data; name="{k}"\r\n\r\n'
                f"{v}\r\n"
            ).encode("utf-8")
    _mp_body += f"--{_boundary}--\r\n".encode()
    _mp_ct = f"multipart/form-data; boundary={_boundary}"

    _log(username, f"POST /ScheduleController?posto_id={posto_id} (multipart/form-data)")
    r_raw = await loop.run_in_executor(executor, lambda: _primp_client.post(
        SCHED_CTRL, params={"posto_id": posto_id},
        content=_mp_body,
        headers={**_HDR_SCHED_CTRL, "Content-Type": _mp_ct},
        timeout=30))

    class _R:
        def __init__(self, raw):
            self.status_code = raw.status_code
            self.text = raw.text
            self.url = str(raw.url)
    r = _R(r_raw)
    _log(username, f"ScheduleController → {r.status_code}  url={r.url}")
    if r.status_code not in (200, 302):
        _log(username, f"ScheduleController body (first 500): {(r.text or '')[:500]}")

    # Step 6: Schedule.jsp
    sched_jsp_url = SCHED_JSP + "?posto_id=" + posto_id
    if "Schedule.jsp" not in r.url:
        _log(username, f"GET /Schedule.jsp?posto_id={posto_id}")
        r = await loop.run_in_executor(executor, lambda: client.get(
            SCHED_JSP, params={"posto_id": posto_id},
            headers={**sess.HEADERS_NAV, "Referer": SCHED_CTRL + "?posto_id=" + posto_id},
            timeout=20))
    _log(username, f"Schedule.jsp HTTP {r.status_code}  url={r.url}")
    sched_html = r.text or ""
    _log(username, f"Schedule.jsp body snippet: {sched_html[:600]}")

    posto_pdf = posto_id
    m_pdf = re.search(r"SubmeterVistoCriaPDF\?posto_id=(\d+)", sched_html)
    if m_pdf:
        posto_pdf = m_pdf.group(1)
        if posto_pdf != posto_id:
            _log(username, f"POSTO_PDF={posto_pdf} (differs from POSTO={posto_id})")

    return {"posto_pdf": posto_pdf, "sched_url": sched_jsp_url}


# ── apply_warmup: steps 1-6 + checkpoint save ─────────────────────────────────

async def apply_warmup(acct: dict, posto_id: str,
                       capsolver_keys: list[str], anticaptcha_keys: list[str],
                       twocaptcha_keys: list[str], capmonster_keys: list[str],
                       executor: ThreadPoolExecutor,
                       nationality: str = "CPV",
                       residence: str = "",
                       client=None,
                       proxy: str | None = None) -> dict | str:
    """
    Run steps 1-6 on a pre-authenticated client. Save schedule_jsp checkpoint.
    Returns {"posto_pdf", "posto_id", "nat", "res", "sched_url"} or "session_dead" | "error".
    client must not be None; caller is responsible for provisioning it.
    """
    import session as sess
    import session_store

    username = acct["username"]
    loop     = asyncio.get_event_loop()
    nat = acct.get("nationality", "").strip() or nationality
    res = residence.strip() or nat

    if client is None:
        return "session_dead"

    # Step 1: alive check
    _log(username, "GET /VistosOnline/")
    try:
        r = await loop.run_in_executor(executor, lambda: client.get(
            session_store.COOKIES_URL, headers=sess.HEADERS_NAV,
            timeout=15, follow_redirects=False))
        if r.status_code == 302:
            return "session_dead"
    except Exception as e:
        _log(username, f"alive check failed: {e}")
        return "session_dead"

    # Steps 2-6
    try:
        info = await _run_steps_2_to_6(client, posto_id, acct, nat, res, executor)
    except Exception as e:
        _log(username, f"warmup steps 2-6 failed: {e}")
        return "error"

    posto_pdf = info["posto_pdf"]
    sched_url = info["sched_url"]

    try:
        _save_client = getattr(client, 'client', client)
        _save_proxy  = getattr(client, 'proxy', None) or proxy
        session_store.save(username, _save_client, _save_proxy,
                           checkpoint="schedule_jsp",
                           posto_id=posto_id,
                           posto_pdf=posto_pdf,
                           nationality=nat,
                           residence=res)
        _log(username, f"checkpoint saved: schedule_jsp  posto_id={posto_id}  "
             f"posto_pdf={posto_pdf}  proxy={'yes' if _save_proxy else 'none'}")
    except Exception as se:
        _log(username, f"checkpoint save failed (non-fatal): {se}")

    return {"posto_pdf": posto_pdf, "posto_id": posto_id, "nat": nat, "res": res,
            "sched_url": sched_url}


# ── apply_book: steps 7-9 ─────────────────────────────────────────────────────

async def apply_book(acct: dict, posto_id: str, posto_pdf: str,
                     slot_manager,
                     capsolver_keys: list[str], anticaptcha_keys: list[str],
                     twocaptcha_keys: list[str], capmonster_keys: list[str],
                     executor: ThreadPoolExecutor,
                     client=None,
                     proxy: str | None = None,
                     nat: str = "CPV",
                     res: str = "") -> str:
    """
    Run steps 7-9 on a warmed session (Schedule.jsp already reachable).
    Returns "applied" | "no_slot" | "error" | "captcha_failed".
    """
    import session as sess
    import solver as solvermod

    username      = acct["username"]
    loop          = asyncio.get_event_loop()
    sched_jsp_url = SCHED_JSP + "?posto_id=" + posto_id

    try:
        # Step 7: CAPTCHA + /slots
        _log(username, f"CAPTCHA solve for SCHEDULE_EVISA (proxy={proxy})")
        try:
            token = await loop.run_in_executor(
                executor,
                lambda: solvermod.race_all(
                    capsolver_keys, anticaptcha_keys, twocaptcha_keys, capmonster_keys,
                    "SCHEDULE_EVISA", proxy, min_score=50,
                ),
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

        _log(username, f"slots raw: {json.dumps(slots_json)[:600]}")

        if isinstance(slots_json, dict):
            slots_data = slots_json.get("data") or {}
            if not slots_data:
                _log(username, "slots: empty data — no appointments available")
                return "no_slot"
            slots_json = [
                {"date": d, "periods": [{"id": p} if not isinstance(p, dict) else p
                                        for p in (ps if isinstance(ps, list) else [ps])]}
                for d, ps in slots_data.items()
            ]
            _log(username, f"slots normalized: {json.dumps(slots_json)[:300]}")

        if not slots_json:
            _log(username, "slots: empty — no appointments available")
            return "no_slot"

        _log(username, f"slots: {len(slots_json)} dates available")

        # Step 8: Assign slot
        visible_slots = slots_json
        lease = None
        if slot_manager:
            slot_manager.update_pool(slots_json)
            lease = slot_manager.request_slot(username, visible_slots)
            if not lease:
                _log(username, "no slot assigned (all leased/taken)")
                return "no_slot"
            slot_date   = lease.date
            slot_period = lease.period_id
        else:
            try:
                first = slots_json[0]
                slot_date = first["date"]
                periods = first.get("periods") or first.get("cmbPeriodo") or []
                if isinstance(periods, list) and periods:
                    p0 = periods[0]
                    slot_period = str(p0["id"] if isinstance(p0, dict) else p0)
                elif isinstance(periods, (str, int)):
                    slot_period = str(periods)
                else:
                    _log(username, f"slot format unknown — first={first}")
                    return "error"
            except Exception as e:
                _log(username, f"slot parse error: {e}")
                return "error"

        _log(username, f"booking slot: date={slot_date} period={slot_period}")

        # Step 9: SubmeterVistoCriaPDF
        r = await loop.run_in_executor(executor, lambda: client.post(
            SUBMIT_URL, params={"posto_id": posto_pdf},
            data={"lang": "ENG", "txtHuman": "", "back": "",
                  "f_date_c": slot_date, "cmbPeriodo": slot_period},
            headers={**sess.HEADERS_XHR, "Referer": sched_jsp_url}, timeout=30))

        resp_text = r.text.strip()
        _log(username, f"SubmeterVisto → {r.status_code}  body={resp_text[:150]}")

        _known_errors = ("indisponivel", "unavailable", "erro", "error", "bd_problm")
        _has_error_kw = any(kw in resp_text.lower() for kw in _known_errors)
        _looks_ok = (
            "PDF" in resp_text
            or "comprovativo" in resp_text.lower()
            or "agendamento" in resp_text.lower()
            or (len(resp_text) > 100 and not _has_error_kw)
        )

        if r.status_code == 200 and _looks_ok:
            if lease:
                slot_manager.confirm(lease)
            pdf_saved = False
            try:
                pdf_url = BASE + "/VistosOnline/MostrarPdf?"
                r_pdf = await loop.run_in_executor(executor, lambda: client.get(
                    pdf_url,
                    headers={**sess.HEADERS_NAV, "Referer": sched_jsp_url},
                    timeout=30))
                ct = (getattr(r_pdf, "headers", {}) or {}).get("content-type", "")
                if r_pdf.status_code == 200 and (
                    "application/pdf" in ct
                    or (hasattr(r_pdf, "content") and len(r_pdf.content) > 1000)
                ):
                    pdf_dir = Path(__file__).parent / "data" / "pdfs"
                    pdf_dir.mkdir(exist_ok=True)
                    pdf_path = pdf_dir / f"{username}.pdf"
                    pdf_bytes = r_pdf.content if hasattr(r_pdf, "content") else r_pdf.text.encode()
                    pdf_path.write_bytes(pdf_bytes)
                    _log(username, f"PDF saved → {pdf_path}  ({len(pdf_bytes)} bytes)")
                    pdf_saved = True
                else:
                    _log(username, f"PDF download failed: status={r_pdf.status_code} ct={ct[:60]}")
            except Exception as pdf_err:
                _log(username, f"PDF download error: {pdf_err}")

            _csv_update(username, status="applied",
                        appointment_ref=f"{slot_date}_{slot_period}",
                        notes=f"booked:{slot_date} period:{slot_period}"
                              + (" pdf_saved" if pdf_saved else ""))
            _log(username, f"APPLIED  slot={slot_date} period={slot_period}  pdf={pdf_saved}")
            return "applied"

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
        _log(username, f"EXCEPTION in apply_book: {e}")
        return "error"


# ── poll_slots_once: scout polling (lazy re-warm + /slots) ────────────────────

async def poll_slots_once(client, proxy: str | None, posto_id: str,
                          acct: dict, nat: str, res: str,
                          capsolver_keys: list[str], anticaptcha_keys: list[str],
                          twocaptcha_keys: list[str], capmonster_keys: list[str],
                          executor: ThreadPoolExecutor) -> list[dict]:
    """
    One scout poll cycle: check alive → lazy re-warm if form state expired →
    solve CAPTCHA → POST /slots → return normalized slot list.
    Raises SessionExpired if Vistos_sid is dead or re-warm fails.
    """
    import session as sess
    import solver as solvermod
    import session_store

    username  = acct.get("username", "?")
    loop      = asyncio.get_event_loop()
    sched_url = SCHED_JSP + "?posto_id=" + posto_id

    # Check session alive
    alive = await loop.run_in_executor(executor, session_store.is_alive, client)
    if not alive:
        raise SessionExpired(f"{username}: Vistos_sid dead")

    # Lazy re-warm: form draft expires ~5-7 min after ScheduleController POST
    try:
        r_check = await loop.run_in_executor(executor, lambda: client.get(
            sched_url, headers=sess.HEADERS_NAV, timeout=15, follow_redirects=False))
        schedule_ok = r_check.status_code == 200
    except Exception:
        schedule_ok = False

    if not schedule_ok:
        _log(username, "poll: form state expired — re-warming steps 2-6")
        try:
            await _run_steps_2_to_6(client, posto_id, acct, nat, res, executor)
        except Exception as e:
            raise SessionExpired(f"{username}: re-warm failed: {e}")

    # Solve CAPTCHA
    try:
        token = await loop.run_in_executor(
            executor,
            lambda: solvermod.race_all(
                capsolver_keys, anticaptcha_keys, twocaptcha_keys, capmonster_keys,
                "SCHEDULE_EVISA", proxy, min_score=50,
            ),
        )
    except Exception as e:
        _log(username, f"poll: CAPTCHA failed: {e}")
        return []

    # POST /slots
    try:
        r = await loop.run_in_executor(executor, lambda: client.post(
            SLOTS_URL, params={"posto_id": posto_id},
            data={"posto_id": posto_id, "captcha": token},
            headers={**sess.HEADERS_XHR, "Referer": sched_url}, timeout=30))
        slots_json = r.json()
    except Exception as e:
        _log(username, f"poll: /slots error: {e}")
        return []

    if isinstance(slots_json, dict):
        slots_data = slots_json.get("data") or {}
        if not slots_data:
            return []
        return [
            {"date": d, "periods": [{"id": p} if not isinstance(p, dict) else p
                                    for p in (ps if isinstance(ps, list) else [ps])]}
            for d, ps in slots_data.items()
        ]
    return slots_json if isinstance(slots_json, list) else []


# ── apply_one: thin wrapper (warmup + book) ────────────────────────────────────

async def apply_one(acct: dict, posto_id: str,
                    slot_manager,
                    capsolver_keys: list[str], anticaptcha_keys: list[str],
                    twocaptcha_keys: list[str], capmonster_keys: list[str],
                    executor: ThreadPoolExecutor,
                    nationality: str = "CPV",
                    residence: str = "",
                    client=None,
                    proxy: str | None = None) -> str:
    """
    Run the full apply workflow for one account.
    Returns: "applied" | "no_slot" | "error" | "captcha_failed" | "session_dead"
    """
    username = acct["username"]
    nat = acct.get("nationality", "").strip() or nationality
    res = residence.strip() or nat

    import session as sess
    import session_store
    loop = asyncio.get_event_loop()

    # Provision client if not provided
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

    # Steps 1-6
    warmup = await apply_warmup(
        acct, posto_id,
        capsolver_keys, anticaptcha_keys, twocaptcha_keys, capmonster_keys,
        executor, nationality=nat, residence=res,
        client=client, proxy=proxy,
    )
    if isinstance(warmup, str):
        return warmup

    # Steps 7-9
    return await apply_book(
        acct, posto_id, warmup["posto_pdf"],
        slot_manager,
        capsolver_keys, anticaptcha_keys, twocaptcha_keys, capmonster_keys,
        executor, client=client, proxy=proxy,
        nat=warmup["nat"], res=warmup["res"],
    )


async def main_async(args: argparse.Namespace,
                     capsolver_keys: list[str], anticaptcha_keys: list[str],
                     twocaptcha_keys: list[str], capmonster_keys: list[str]) -> None:
    from account_pool import AccountPool

    acct_file = Path(args.accounts_file) if getattr(args, "accounts_file", "") else _ACCOUNT_FILE
    pool = AccountPool(acct_file)
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
    res      = getattr(args, "residence", "") or nat

    print(f"\n[batch_apply] {len(accounts)} accounts  posto={posto_id}"
          f"  nationality={nat}  residence={res}  concurrency={args.concurrency}")

    sem      = asyncio.Semaphore(args.concurrency)
    executor = ThreadPoolExecutor(max_workers=args.concurrency + 4)

    async def bounded(acct):
        async with sem:
            return await apply_one(
                acct, posto_id,
                None,
                capsolver_keys, anticaptcha_keys, twocaptcha_keys, capmonster_keys,
                executor,
                nationality=nat,
                residence=res,
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
    parser.add_argument("--posto",        default="",
                        help="Consular post ID (default: driven by --mode)")
    parser.add_argument("--nationality",  default="",
                        help="ISO 3166-1 alpha-3 passport nationality (default: driven by --mode)")
    parser.add_argument("--residence",    default="",
                        help="Country of residence ISO code (default: driven by --mode)")
    parser.add_argument("--accounts-file", default="",
                        dest="accounts_file",
                        help="Path to accounts CSV (default: driven by --mode)")
    parser.add_argument("--mode", default="",
                        help="Run mode: test or real (default: real; or MODE from .env)")
    args = parser.parse_args()

    from mode_config import get_mode_cfg
    cfg = get_mode_cfg(env, args.mode)
    args.posto       = args.posto       or cfg["posto_id"]
    args.nationality = args.nationality or cfg["nationality"]
    args.residence   = args.residence   or cfg["residence"]
    if not args.accounts_file:
        args.accounts_file = cfg["accounts_file"]

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
