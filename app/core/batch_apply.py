"""
Per-account full apply workflow.

Step sequence (confirmed from HAR manual_full_trace_realdevice.har):
  1. GET  /VistosOnline/                          verify session alive
  2. GET  /VistosOnline/Questionario
  3. GET  /VistosOnline/QuestNextQuestion x N     (steps loaded from data/quest_steps.json)
  4. POST /VistosOnline/Formulario?copy=true      -> returns HTML with __RequestVerificationToken
  5. POST /VistosOnline/ScheduleController?posto_id=POSTO   multipart f1-f46 + token
  6. GET  /VistosOnline/Schedule.jsp?posto_id=POSTO          (follow redirect from step 5)
  7. POST /VistosOnline/slots?posto_id=POSTO      captcha -> JSON list of available slots
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
import json as _json_mod
import json
import re
import sys
import threading
import time as _time
import urllib.parse as _urllib_parse
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

BASE = "https://pedidodevistos.mne.gov.pt"
QUEST_URL   = BASE + "/VistosOnline/Questionario"
QUEST_NEXT  = BASE + "/VistosOnline/QuestNextQuestion"
PROFILE_URL = BASE + "/VistosOnline/profile.jsp"
FORMULARIO  = BASE + "/VistosOnline/Formulario"
SCHED_CTRL  = BASE + "/VistosOnline/ScheduleController"
SCHED_JSP   = BASE + "/VistosOnline/Schedule.jsp"
SLOTS_URL   = BASE + "/VistosOnline/slots"
SUBMIT_URL  = BASE + "/VistosOnline/SubmeterVistoCriaPDF"

_ENV_FILE           = Path(__file__).parent / ".env"
_ACCOUNT_FILE       = Path(__file__).parent / "data" / "accounts.csv"
_QUEST_STEPS_FILE   = Path(__file__).parent / "data" / "quest_steps.json"
_FORM_DEFAULTS_FILE = Path(__file__).parent / "data" / "form_defaults.json"
_SOAX_FILE          = Path(__file__).parent / "data" / "proxies_soax.txt"

_csv_lock   = threading.Lock()
_print_lock = threading.Lock()

# Singleton proxy pool used for /slots proxy rotation (separate cursor from login pool)
_slots_proxy_pool = None
_slots_pool_lock  = threading.Lock()
_SLOTS_PROXY_RETRIES = 5  # max proxy rotations per /slots attempt


def _next_slots_proxy() -> str:
    global _slots_proxy_pool
    with _slots_pool_lock:
        if _slots_proxy_pool is None:
            from proxy_pool import PersistentProxyPool
            _slots_state_file = _SOAX_FILE.with_name(_SOAX_FILE.stem + "_slots.proxy_state.json")
            _slots_proxy_pool = PersistentProxyPool(_SOAX_FILE, state_file=_slots_state_file)
        return _slots_proxy_pool.advance()


class SessionExpired(Exception):
    """Raised by poll_slots_once when Vistos_sid is dead."""


# -- JSON config loaders --------------------------------------------------------

def _load_quest_steps(nationality: str) -> list[dict]:
    data = json.loads(_QUEST_STEPS_FILE.read_text(encoding="utf-8"))
    steps = data.get(nationality) or data.get("CPV")
    return steps

def _load_form_defaults() -> dict:
    return json.loads(_FORM_DEFAULTS_FILE.read_text(encoding="utf-8"))

# -- Dynamic date resolver ------------------------------------------------------

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

# -- Payload builder ------------------------------------------------------------

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


# -- State-evidence screenshots ---------------------------------------------
# Off by default (screenshots cost ~100-300ms + disk space per call, and a
# browser isn't always attached). Enable with EVIDENCE_SCREENSHOTS=1 in .env
# or the environment for verification runs. Uses the same client.screenshot()
# plumbing already proven at login (app/core/session.py _take_screenshot).
import os as _os
_EVIDENCE_SCREENSHOTS = _os.environ.get("EVIDENCE_SCREENSHOTS", "") == "1"


async def _evidence(client, username: str, state: str, executor: ThreadPoolExecutor) -> None:
    """Best-effort state screenshot for visual verification evidence. Never raises,
    never blocks the workflow on failure — a missed screenshot is not a workflow error."""
    if not _EVIDENCE_SCREENSHOTS or not hasattr(client, "screenshot"):
        return
    try:
        loop = asyncio.get_event_loop()
        path = await loop.run_in_executor(executor, lambda: client.screenshot(username, state))
        ts = _time.strftime("%Y-%m-%d %H:%M:%S")
        if path:
            _log(username, f"EVIDENCE state={state} screenshot={path} ts={ts}")
        else:
            _log(username, f"EVIDENCE state={state} screenshot=FAILED ts={ts}")
    except Exception as _ee:
        _log(username, f"EVIDENCE state={state} screenshot=ERROR ({_ee})")


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


# -- Steps 2-6: questionnaire -> Formulario -> ScheduleController -> Schedule.jsp -

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
        "Referer":                   FORMULARIO,
        "Sec-Fetch-Dest":            "document",
        "Sec-Fetch-Mode":            "navigate",
        "Sec-Fetch-Site":            "same-origin",
        "Sec-Fetch-User":            "?1",
        "Upgrade-Insecure-Requests": "1",
    }

    _has_browser = hasattr(client, "browser_nav")
    _t0 = _time.time()
    def _elapsed(label: str) -> None:
        _log(username, f"  [t+{_time.time()-_t0:.1f}s] {label}")

    # Steps 2-3: questionnaire.
    base_qs = {"lang": "PT", "nacionalidade": nat}
    if _has_browser:
        # Inject primp's authenticated cookies into the browser (post-login primp sync gives
        # primp the valid session; HOME nav propagates those cookies into the browser context).
        # This is necessary because the browser login may land back at Authentication.jsp on
        # certain proxies, leaving the browser unauthenticated.
        _home_url = BASE + "/VistosOnline/"
        _log(username, "GET /VistosOnline/ (browser_nav, WITH cookie-sync) — authenticate browser via primp cookies")
        try:
            _home_nav_url = await loop.run_in_executor(executor, lambda: client.browser_nav(_home_url, timeout=30))
            _log(username, f"HOME nav: final_url={_home_nav_url}")
            await _evidence(client, username, "authenticated_index", executor)
            # Dump the authenticated HOME dashboard content
            _home_diag_js = """(() => {
                // 1. Page visible text (what user sees)
                const bodyText = document.body.innerText.replace(/\s+/g, ' ').trim().slice(0, 1000);
                // 2. All anchor tags with hrefs (internal only)
                const anchors = Array.from(document.querySelectorAll('a[href]'))
                    .filter(a => a.href && (a.href.includes('pedidodevistos') || a.href.startsWith('/')))
                    .map(a => a.href.replace('https://pedidodevistos.mne.gov.pt','') + ' [' + a.textContent.trim().replace(/\s+/g,' ').slice(0,40) + ']');
                // 3. All forms and their actions
                const forms = Array.from(document.querySelectorAll('form')).map(f => ({
                    action: f.action.replace('https://pedidodevistos.mne.gov.pt',''),
                    method: f.method,
                    id: f.id,
                    fields: Array.from(f.elements).map(e => e.name).filter(Boolean).slice(0,10)
                }));
                // 4. All buttons/inputs with onclick
                const btns = Array.from(document.querySelectorAll('button,input[type=button],input[type=submit],a'))
                    .filter(e => e.getAttribute('onclick') || e.getAttribute('href'))
                    .map(e => ({tag:e.tagName, text:e.textContent.trim().slice(0,40), onclick:e.getAttribute('onclick')||'', href:(e.getAttribute('href')||'')}))
                    .slice(0, 20);
                return {bodyText, anchors: anchors.slice(0,30), forms, btns};
            })()"""
            _home_diag = await loop.run_in_executor(executor, lambda: client.browser_eval(_home_diag_js))
            _log(username, f"HOME dashboard diagnostic: {_home_diag}")
        except Exception as _he:
            _log(username, f"HOME nav failed (non-fatal): {_he}")

        # HAR-confirmed page_2 transition: POST /VistosOnline/ with lang=ENG&lang=PT
        # This is mandatory — it moves the server into application-workflow state.
        # Without it, Formulario returns "Session lost" (1905 bytes) regardless of auth state.
        _log(username, "POST /VistosOnline/ (lang transition, page_2)")
        try:
            _lang_post_js = """(async () => {
                const r = await fetch('https://pedidodevistos.mne.gov.pt/VistosOnline/', {
                    method: 'POST', credentials: 'include',
                    headers: {'Content-Type': 'application/x-www-form-urlencoded'},
                    body: 'lang=ENG&lang=PT'
                });
                return {status: r.status, url: r.url, len: (await r.text()).length};
            })()"""
            _lang_result = await loop.run_in_executor(executor, lambda: client.browser_eval(_lang_post_js))
            _log(username, f"lang POST result: {_lang_result}")
        except Exception as _lpe:
            _log(username, f"lang POST failed (non-fatal): {_lpe}")

        _log(username, "GET /Questionario (browser_nav, skip_cookie_sync)")
        _q0_url = await loop.run_in_executor(executor, lambda: client.browser_nav(QUEST_URL, timeout=35, skip_cookie_sync=True))
        _elapsed("Questionario nav done")
        _log(username, f"Questionario: final_url={_q0_url}")
        await _evidence(client, username, "questionario", executor)
        _last_quest_fields: dict = {}
        for _bi, _bstep in enumerate(quest_steps):
            _bparams = {**base_qs, **_bstep}
            _burl = QUEST_NEXT + "?" + _urllib_parse.urlencode(_bparams)
            _burl_js = _json_mod.dumps(_burl)
            _quest_url_js = _json_mod.dumps(QUEST_URL)
            _fetch_js = (
                f"(async () => {{"
                f"  const r = await fetch({_burl_js}, {{"
                f"    method: 'GET', credentials: 'include',"
                f"    headers: {{"
                f"      'Accept': 'text/plain, */*; q=0.01',"
                f"      'X-Requested-With': 'XMLHttpRequest',"
                f"      'Sec-Fetch-Mode': 'cors',"
                f"      'Sec-Fetch-Dest': 'empty',"
                f"      'Sec-Fetch-Site': 'same-origin',"
                f"      'Referer': {_quest_url_js}"
                f"    }}"
                f"  }});"
                f"  const body = await r.text();"
                f"  const isLast = {_json_mod.dumps(_bi == len(quest_steps) - 1)};"
                f"  if (isLast) {{"
                f"    const _tmp = document.createElement('div'); _tmp.innerHTML = body;"
                f"    const _flds = {{}};"
                f"    _tmp.querySelectorAll('input').forEach(function(inp) {{ if(inp.name) _flds[inp.name] = inp.value; }});"
                f"    return {{status: r.status, len: body.length, snippet: body.substring(0, 1200), fields: _flds}};"
                f"  }}"
                f"  return {{status: r.status, len: body.length, snippet: body.substring(0, 60)}};"
                f"}})()"
            )
            # A quest step can silently fail to register server-side without a browser
            # exception — same flakiness a human sees as the page "getting stuck" and
            # needing a manual refresh to reveal the next question. Verify status==200
            # per step (not just log it) and retry THIS step a few times before moving
            # on, instead of only discovering the breakage once the last step comes
            # back with empty fields (by which point we don't know which step broke).
            _step_result = None
            for _step_attempt in range(1, 4):
                try:
                    _step_result = await loop.run_in_executor(executor, lambda j=_fetch_js: client.browser_eval(j))
                except Exception as _qe:
                    _log(username, f"  quest step {_bi+1} attempt {_step_attempt}/3 failed: {_qe}")
                    if _step_attempt == 3:
                        raise RuntimeError(f"quest step {_bi+1} failed: {_qe}") from _qe
                    await asyncio.sleep(1.5)
                    continue
                _status = _step_result.get('status') if isinstance(_step_result, dict) else None
                _log(username, f"  quest step {_bi+1}/{len(quest_steps)} id={_bstep.get('id_pergunta')} "
                     f"-> status={_status} len={_step_result.get('len')} attempt={_step_attempt}/3 "
                     f"snippet={str(_step_result.get('snippet',''))[:60]}")
                if _status == 200:
                    break
                if _step_attempt == 3:
                    _log(username, f"  quest step {_bi+1} never returned 200 after 3 attempts — continuing anyway")
                else:
                    _log(username, f"  quest step {_bi+1} status={_status} -- retrying (like a manual page refresh)")
                    await asyncio.sleep(1.5)
            if _bi == len(quest_steps) - 1 and isinstance(_step_result, dict):
                _last_quest_fields = _step_result.get('fields') or {}
                _log(username, f"  last quest fields ({len(_last_quest_fields)}): {list(_last_quest_fields.keys())}")
        _elapsed(f"quest {len(quest_steps)} steps done")
        _log(username, f"questionnaire: {len(quest_steps)} steps done (nat={nat}) [browser_eval XHR]")
        # Fast-fail: if the last quest step didn't yield fields, the browser's cookie jar
        # was desynced from the in-progress questionnaire session (or the proxy got blocked
        # mid-flow, e.g. 403s on every step) — every downstream Formulario fallback depends
        # on these same fields/session and is deterministically doomed too. Raise immediately
        # instead of walking all 4 cascading fallbacks (~40-100s each) before hitting csrf_missing.
        if not _last_quest_fields:
            raise RuntimeError("quest_state_stale")
        # Log browser's current session cookie after quest steps (diagnostic)
        try:
            _diag_cookie = await loop.run_in_executor(executor, lambda: client.browser_eval("document.cookie"))
            _log(username, f"  browser cookies after quest: {str(_diag_cookie)[:200]}")
        except Exception:
            pass
    else:
        _log(username, "GET /Questionario")
        await loop.run_in_executor(executor, lambda: client.get(
            QUEST_URL, headers=sess.HEADERS_NAV, timeout=20))
        for step in quest_steps:
            params = {**base_qs, **step}
            await loop.run_in_executor(executor, lambda p=params: client.get(
                QUEST_NEXT, params=p, headers=_HDR_QUEST_NEXT, timeout=20))
        _log(username, f"questionnaire: {len(quest_steps)} steps done (nat={nat})")
        _last_quest_fields: dict = {}


    # Steps 4-5: primp handles Formulario POST + ScheduleController POST.
    # These create the server-side form state (tied to Vistos_sid).
    # Step 6: browser page.goto() for Schedule.jsp -- bypasses DataDome while
    # the server sees the same Vistos_sid that primp used for steps 4-5.
    formulario_payload = {
        "lang":                   "ENG",  # DOM form hidden field value (page renders in English)
        "nacionalidade":          nat,
        "pais_residencia":        res,
        "tipo_passaporte":        "01",
        "copia_pedido":           "",
        "cb_paises":              nat,    # nationality select (name=cb_paises from HAR HTML)
        "cb_pais_residencia":     res,
        "cb_tipo_passaporte":     "01",
        "cb_qt_dias":             "SCH",
        "cb_trab_sazonal":        "O",
        "cb_motivo_estada_sch":   "10",
        "cb_viaja_reune_turismo": "FAM_N",
        "tipo_visto":             "C",
        "tipo_visto_desc":        "VISTO DE CURTA DURAÇÃO",
        "class_visto":            "SCH",
        "cod_estada":             "10",
        "id_visto_doc":           "36",
    }

    # Step 4: Formulario — POST with server-derived fields from last quest step.
    # The last QuestNextQuestion response (id=16) returns HTML with hidden <input>
    # elements containing the exact payload the server expects at /Formulario.
    # We extract those fields via DOMParser in the browser context (JS injected during
    # quest step), then submit them via DOM form.submit() to bypass DataDome.
    _log(username, f"formulario_payload (fallback): {_urllib_parse.urlencode(formulario_payload)}")
    _log(username, f"last quest fields ({len(_last_quest_fields)}): {list(_last_quest_fields.keys())}")
    csrf = ""
    _form_html = ""
    # Merge: formulario_payload (base with lang=PT + account fields) + quest-derived overrides
    _merged_payload = {**formulario_payload, **_last_quest_fields}

    # Extract the actual Formulario POST URL from the Questionario page DOM.
    # The form action is server-rendered: may be /Formulario?copy=true or /Formulario
    # depending on whether this is a fresh application or a copy of a previous one.
    _formulario_post_url = FORMULARIO + "?copy=true"  # default fallback
    if _has_browser:
        try:
            _action_js = """(() => {
                const forms = Array.from(document.querySelectorAll('form'));
                const f = forms.find(x => x.action && x.action.toLowerCase().includes('formulario'));
                return f ? f.action : null;
            })()"""
            _extracted = await loop.run_in_executor(executor, lambda: client.browser_eval(_action_js))
            if _extracted and 'formulario' in _extracted.lower():
                _formulario_post_url = _extracted
                _log(username, f"Formulario action extracted from Questionario DOM: {_formulario_post_url}")
            else:
                _log(username, f"Formulario action not found in DOM, using default: {_formulario_post_url}")
        except Exception as _ax:
            _log(username, f"Formulario action extraction failed: {_ax} — using default")

    if _has_browser:
        # Primary: browser_form_post with full merged payload (all 16+ fields, HAR-confirmed).
        # DOM form (submit_page_form) only has 6 fields with default/wrong values because
        # the XHR quest steps update server session state but not the DOM form.
        _log(username, f"POST {_formulario_post_url} (browser_form_post, merged payload={list(_merged_payload.keys())})")
        try:
            _form_final_url, _form_html = await loop.run_in_executor(
                executor, lambda: client.browser_form_post(_formulario_post_url, _merged_payload, timeout=100))
            _elapsed(f"Formulario POST done (len={len(_form_html)})")
            _log(username, f"Formulario browser_form_post -> {_form_final_url}  len={len(_form_html)}")
            _form_snip = _form_html[:2000].encode('ascii', 'replace').decode()
            _log(username, f"Formulario POST snippet: {_form_snip}")
            csrf = _extract_csrf(_form_html) or ""
            if csrf:
                _log(username, f"CSRF extracted (browser_form_post): {csrf[:20]}...  (len={len(csrf)})")
            else:
                _log(username, f"Formulario browser_form_post: no CSRF (len={len(_form_html)})")
        except Exception as _be:
            _log(username, f"Formulario browser_form_post failed: {_be}")
    if not csrf and _has_browser:
        # Retry with the alternate Formulario URL variant (toggle ?copy=true)
        _alt_formulario = FORMULARIO if "copy=true" in _formulario_post_url else FORMULARIO + "?copy=true"
        _log(username, f"POST {_alt_formulario} (browser_form_post alternate variant)")
        try:
            _form_final_url, _form_html = await loop.run_in_executor(
                executor, lambda: client.browser_form_post(_alt_formulario, _merged_payload, timeout=100))
            _elapsed(f"Formulario POST (no copy) done (len={len(_form_html)})")
            _log(username, f"Formulario browser_form_post (no copy) -> {_form_final_url}  len={len(_form_html)}")
            _form_snip = _form_html[:2000].encode('ascii', 'replace').decode()
            _log(username, f"Formulario no-copy snippet: {_form_snip}")
            csrf = _extract_csrf(_form_html) or ""
            if csrf:
                _log(username, f"CSRF extracted (no-copy POST): {csrf[:20]}...  (len={len(csrf)})")
            else:
                _log(username, f"Formulario no-copy POST: no CSRF (len={len(_form_html)})")
        except Exception as _be:
            _log(username, f"Formulario no-copy browser_form_post failed: {_be}")
    _has_submit_page_form = hasattr(client, "submit_page_form")
    if not csrf and _has_browser and _has_submit_page_form:
        # Fallback: submit the existing DOM form on the Questionario page.
        _log(username, "POST /Formulario (submit_page_form fallback — existing Questionario form)")
        try:
            _form_final_url, _form_html = await loop.run_in_executor(
                executor, lambda: client.submit_page_form(action_contains="Formulario", timeout=100))
            _elapsed(f"Formulario submit_page_form done (len={len(_form_html)})")
            _log(username, f"Formulario submit_page_form -> {_form_final_url}  len={len(_form_html)}")
            _form_snip = _form_html[:2000].encode('ascii', 'replace').decode()
            _log(username, f"Formulario submit_page_form snippet: {_form_snip}")
            csrf = _extract_csrf(_form_html) or ""
            if csrf:
                _log(username, f"CSRF extracted (submit_page_form): {csrf[:20]}...  (len={len(csrf)})")
            else:
                _log(username, f"submit_page_form: no CSRF (len={len(_form_html)})")
        except Exception as _be:
            _log(username, f"submit_page_form failed: {_be}")
    if not csrf and _has_browser:
        # Fallback: try GET /Formulario via browser_nav
        _log(username, "GET /Formulario (browser_nav fallback)")
        try:
            _form_nav_url = await loop.run_in_executor(
                executor, lambda: client.browser_nav(FORMULARIO, timeout=40))
            _form_html = await loop.run_in_executor(
                executor, lambda: client.browser_eval("document.documentElement.outerHTML")) or ""
            _elapsed(f"Formulario GET done (len={len(_form_html)})")
            _form_snip = _form_html[:2000].encode('ascii', 'replace').decode()
            _log(username, f"Formulario GET snippet: {_form_snip}")
            csrf = _extract_csrf(_form_html) or ""
            if csrf:
                _log(username, f"CSRF extracted (browser GET): {csrf[:20]}...  (len={len(csrf)})")
        except Exception as _be2:
            _log(username, f"Formulario browser_nav failed: {_be2}")
    if not csrf:
        _log(username, "ERROR: CSRF token not found — cannot submit ScheduleController")
        raise RuntimeError("csrf_missing")
    await _evidence(client, username, "formulario", executor)

    # Step 5: ScheduleController
    payload = _build_schedule_payload(acct, posto_id, csrf, nat, residence=res)
    _log(username, f"ScheduleController key fields: f0sf1={payload.get('f0sf1')}  "
         f"f1={payload.get('f1')}  f3={payload.get('f3')}  f4={payload.get('f4')}  "
         f"f14={payload.get('f14')}  csrf_len={len(csrf)}")

    # Use browser_fetch multipart when we have a browser (same DataDome clearance context)
    if _has_browser and csrf:
        _log(username, f"POST /ScheduleController?posto_id={posto_id} (browser_fetch multipart)")
        _sched_posted = False
        try:
            _file_names = ["foto", "file1", "file2", "file3", "file4"]
            raw = await loop.run_in_executor(executor, lambda: client.browser_fetch(
                SCHED_CTRL, payload,
                params={"posto_id": posto_id},
                headers={"Referer": FORMULARIO},
                encode="multipart",
                file_fields=_file_names,
                timeout=45))
            sc_status, sc_url = raw["status"], SCHED_CTRL
            _log(username, f"ScheduleController -> {sc_status} (browser)")
            _sched_posted = True  # POST reached the server — do not re-POST via primp
        except Exception as _be:
            _log(username, f"browser ScheduleController failed: {_be} - falling back to primp")
        if _sched_posted:
            # ScheduleController was POSTed successfully; navigate Schedule.jsp to read
            # the resulting form state. Failure here does NOT fall back to primp POST.
            sched_jsp_url = SCHED_JSP + "?posto_id=" + posto_id
            sched_html = ""
            posto_pdf   = posto_id
            try:
                _log(username, f"NAV /Schedule.jsp?posto_id={posto_id} (browser page.goto)")
                await loop.run_in_executor(executor, lambda: client.browser_nav(sched_jsp_url, timeout=40))
                sched_html = await loop.run_in_executor(executor, lambda: client.browser_eval(
                    "document.documentElement.outerHTML")) or ""
                _log(username, f"Schedule.jsp browser len={len(sched_html)}")
                _log(username, f"Schedule.jsp body snippet: {sched_html[:400].encode('ascii', 'replace').decode()}")
                await _evidence(client, username, "schedule_controller", executor)
                m_pdf = re.search(r"SubmeterVistoCriaPDF\?posto_id=(\d+)", sched_html)
                if m_pdf:
                    posto_pdf = m_pdf.group(1)
                    if posto_pdf != posto_id:
                        _log(username, f"POSTO_PDF={posto_pdf} (differs from POSTO={posto_id})")
            except Exception as _nav_e:
                _log(username, f"browser Schedule.jsp nav failed: {_nav_e} — using posto_id as fallback")
            # Guard: ScheduleController POST can return 200 yet the server silently
            # fails to set up form state, causing Schedule.jsp to redirect to
            # authenticated_index. Detect by looking for scheduling-page markers.
            # Absence means ScheduleController was rejected (bad CSRF or bad session
            # state) — must re-warm rather than returning a false "warmed".
            _sched_markers = ("getSlots", "captchaDiv", "SubmeterVistoCriaPDF", "f_date_c", "cmbPeriodo")
            if sched_html and not any(m in sched_html for m in _sched_markers):
                _log(username, f"Schedule.jsp wrong page (len={len(sched_html)}, no scheduling markers) — likely redirected to auth; raising to trigger re-warm")
                raise RuntimeError("schedule_jsp_redirect")
            return {"posto_pdf": posto_pdf, "sched_url": sched_jsp_url}

    _primp_client = getattr(client, 'client', client)
    _boundary  = "----WebKitFormBoundary" + _uuid.uuid4().hex[:16]
    _fnames_set = {"foto", "file1", "file2", "file3", "file4"}
    _mp_body = b""
    for k, v in payload.items():
        if k in _fnames_set:
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
    _log(username, f"POST /ScheduleController?posto_id={posto_id} (primp multipart)")
    r_raw = await loop.run_in_executor(executor, lambda: _primp_client.post(
        SCHED_CTRL, params={"posto_id": posto_id},
        content=_mp_body,
        headers={**_HDR_SCHED_CTRL, "Content-Type": _mp_ct},
        timeout=30))
    sc_status, sc_url = r_raw.status_code, str(r_raw.url)
    sc_body = r_raw.text or ""
    _log(username, f"ScheduleController -> {sc_status}  url={sc_url}  len={len(sc_body)}")
    _log(username, f"ScheduleController body snippet: {sc_body[:300].encode('ascii', 'replace').decode()}")

    # Step 6: Schedule.jsp
    sched_jsp_url = SCHED_JSP + "?posto_id=" + posto_id
    if _has_browser and "Schedule.jsp" not in sc_url:
        # Browser page.goto() bypasses DataDome. Cookie sync in _CMD_NAV injects
        # primp's current Vistos_sid so the server sees the same session state.
        _log(username, f"NAV /Schedule.jsp?posto_id={posto_id} (browser page.goto)")
        await loop.run_in_executor(executor, lambda: client.browser_nav(sched_jsp_url, timeout=35))
        sched_html = await loop.run_in_executor(executor, lambda: client.browser_eval(
            "document.documentElement.outerHTML")) or ""
        _log(username, f"Schedule.jsp browser len={len(sched_html)}")
        _log(username, f"Schedule.jsp body snippet: {sched_html[:400].encode('ascii', 'replace').decode()}")
        await _evidence(client, username, "schedule_controller", executor)
    else:
        if "Schedule.jsp" not in sc_url:
            _log(username, f"GET /Schedule.jsp?posto_id={posto_id} (primp)")
            r = await loop.run_in_executor(executor, lambda: client.get(
                SCHED_JSP, params={"posto_id": posto_id},
                headers={**sess.HEADERS_NAV, "Referer": SCHED_CTRL + "?posto_id=" + posto_id},
                timeout=20))
            sched_html = r.text or ""
        else:
            sched_html = r_raw.text or ""
        _log(username, f"Schedule.jsp primp len={len(sched_html)}")
        _log(username, f"Schedule.jsp body snippet: {sched_html[:400].encode('ascii', 'replace').decode()}")

    posto_pdf = posto_id
    m_pdf = re.search(r"SubmeterVistoCriaPDF\?posto_id=(\d+)", sched_html)
    if m_pdf:
        posto_pdf = m_pdf.group(1)
        if posto_pdf != posto_id:
            _log(username, f"POSTO_PDF={posto_pdf} (differs from POSTO={posto_id})")

    _sched_markers = ("getSlots", "captchaDiv", "SubmeterVistoCriaPDF", "f_date_c", "cmbPeriodo")
    if sched_html and not any(m in sched_html for m in _sched_markers):
        _log(username, f"Schedule.jsp wrong page (len={len(sched_html)}, no scheduling markers) — likely auth redirect; raising to trigger re-warm")
        raise RuntimeError("schedule_jsp_redirect")

    return {"posto_pdf": posto_pdf, "sched_url": sched_jsp_url}


# -- apply_warmup: steps 1-6 + checkpoint save ---------------------------------

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
        import traceback, logging as _logging
        _log(username, f"warmup steps 2-6 failed: {e}")
        _logging.getLogger("engine.warmup").error(
            f"[{username}] warmup steps 2-6 exception: {e}\n{traceback.format_exc()}")
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


# -- apply_book: steps 7-9 -----------------------------------------------------

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
        _has_browser = hasattr(client, "browser_fetch")

        # Extract session cookies for proxy-rotation retries
        import session_store as _ss
        _primp_c = getattr(client, "client", client)
        try:
            _session_cookies = _primp_c.get_cookies(_ss.COOKIES_URL) or {}
        except Exception:
            _session_cookies = {}

        slots_text = ""
        slots_status = 0
        _dd_unblock_tried = False
        for _slot_attempt in range(_SLOTS_PROXY_RETRIES + 1):
            _is_first = (_slot_attempt == 0)
            _method = ""
            try:
                if _is_first and _has_browser and hasattr(client, "browser_form_post"):
                    # Try a real navigation (form.submit()) first — the same trick that
                    # already bypasses DataDome for Formulario/login. fetch()/XHR calls to
                    # this endpoint get blocked because bd.js never runs against them; a
                    # real navigation runs bd.js and is generally trusted, regardless of
                    # whether the body it returns is the raw JSON or an HTML-wrapped page.
                    try:
                        _fp_url, _fp_html = await loop.run_in_executor(
                            executor, lambda: client.browser_form_post(
                                SLOTS_URL + "?posto_id=" + posto_id,
                                {"posto_id": posto_id, "captcha": token}, timeout=100))
                        _fp_snip = _fp_html[:300]
                        _fp_blank = _fp_html.strip() in (
                            "", "<html><head></head><body></body></html>",
                        )
                        if _fp_blank:
                            # Real navigation landed on chrome-error://chromewebdata/ (a proxy
                            # connection failure mid-navigation, e.g. ERR_TUNNEL_CONNECTION_FAILED)
                            # — not a DataDome block, not a real empty-slots answer. Must rotate
                            # like any other failed attempt, not be accepted as a final response.
                            _log(username, "slots: browser_form_post landed on blank/chrome-error page (proxy connection failure) — falling back to fetch")
                            slots_text, slots_status, _method = "", 0, ""
                        elif not (
                            "body{margin:0;background:#fff}" in _fp_snip
                            or "datadome" in _fp_snip.lower()
                        ):
                            slots_text = _fp_html
                            slots_status = 200
                            _method = "browser_form_post"
                        else:
                            _log(username, "slots: browser_form_post also DataDome-blocked — falling back to fetch")
                            slots_text, slots_status, _method = "", 0, ""
                    except Exception as _fpe:
                        _log(username, f"slots: browser_form_post failed: {_fpe} — falling back to fetch")
                        slots_text, slots_status, _method = "", 0, ""

                if _is_first and _has_browser and not _method:
                    raw = await loop.run_in_executor(executor, lambda: client.browser_fetch(
                        SLOTS_URL, {"posto_id": posto_id, "captcha": token},
                        params={"posto_id": posto_id},
                        headers={"X-Requested-With": "XMLHttpRequest", "Referer": sched_jsp_url},
                        timeout=30))
                    slots_text = raw["body"]
                    slots_status = raw["status"]
                    _method = "browser_fetch"
                if not _is_first or not _has_browser:
                    import primp as _primp
                    _rot_proxy = _next_slots_proxy()
                    _log(username, f"slots: rotating to fresh proxy (attempt {_slot_attempt}), re-solving CAPTCHA")
                    # Re-solve CAPTCHA — tokens are single-use; the token solved before
                    # the loop is expired by the time any retry runs.
                    try:
                        token = await loop.run_in_executor(
                            executor,
                            lambda: solvermod.race_all(
                                capsolver_keys, anticaptcha_keys, twocaptcha_keys, capmonster_keys,
                                "SCHEDULE_EVISA", _rot_proxy, min_score=50,
                            ),
                        )
                    except Exception as _ce:
                        _log(username, f"slots: CAPTCHA re-solve failed (attempt {_slot_attempt}): {_ce}")
                        if _slot_attempt >= _SLOTS_PROXY_RETRIES:
                            return "captcha_failed"
                        continue
                    _tmp = _primp.Client(
                        proxy=_rot_proxy, impersonate="chrome_131",
                        verify=True, follow_redirects=False)
                    if _session_cookies:
                        _tmp.set_cookies(_ss.COOKIES_URL, _session_cookies)
                    r = await loop.run_in_executor(executor, lambda: _tmp.post(
                        SLOTS_URL, params={"posto_id": posto_id},
                        data={"posto_id": posto_id, "captcha": token},
                        headers={**sess.HEADERS_XHR, "Referer": sched_jsp_url}, timeout=30))
                    slots_text, slots_status = r.text, r.status_code
                    _method = f"primp(proxy#{_slot_attempt})"
            except Exception as _e:
                _log(username, f"slots: fetch error (attempt {_slot_attempt}): {_e}")
                if _slot_attempt >= _SLOTS_PROXY_RETRIES:
                    return "error"
                continue

            _body_snip = slots_text[:300]
            if slots_status == 302:
                _log(username, f"slots: 302 redirect via {_method} (session expired or WAF) (attempt {_slot_attempt})")
                if _slot_attempt >= _SLOTS_PROXY_RETRIES:
                    return "proxy_blocked"
                continue  # rotate proxy and retry
            if "body{margin:0;background:#fff}" in _body_snip or "datadome" in _body_snip.lower():
                _log(username, f"slots: DataDome block via {_method} (attempt {_slot_attempt})")
                if _is_first and _has_browser and not _dd_unblock_tried:
                    _dd_unblock_tried = True
                    # This is DataDome's lightweight "tag" challenge (bd.js + location.reload()),
                    # the same one session.py already handles elsewhere by waiting then re-GETting
                    # the same URL — NOT the interactive slider/CAPTCHA challenge login hits. Try
                    # the cheap fix first: wait for bd.js's timing check to pass, then retry the
                    # exact same request on the same proxy/session before spending a proxy rotation.
                    _log(username, "slots: tag-challenge — waiting 4s then retrying same proxy")
                    await asyncio.sleep(4)
                    try:
                        raw = await loop.run_in_executor(executor, lambda: client.browser_fetch(
                            SLOTS_URL, {"posto_id": posto_id, "captcha": token},
                            params={"posto_id": posto_id},
                            headers={"X-Requested-With": "XMLHttpRequest", "Referer": sched_jsp_url},
                            timeout=30))
                        slots_text = raw["body"]
                        slots_status = raw["status"]
                        _body_snip = slots_text[:300]
                        if slots_status != 302 and not (
                            "body{margin:0;background:#fff}" in _body_snip
                            or "datadome" in _body_snip.lower()
                        ):
                            break  # wait-and-retry worked — got a real response
                        _log(username, "slots: still blocked after wait-and-retry — trying interactive unblock")
                    except Exception as _we:
                        _log(username, f"slots: wait-and-retry failed: {_we}")

                    # Fallback: in case this WAS the heavier interactive challenge, try the
                    # iframe-based DatadomeSliderTask solve (same mechanism as the login path).
                    _challenge_url = SLOTS_URL + "?posto_id=" + posto_id
                    _unblocked = await loop.run_in_executor(
                        executor, lambda: client.unblock_datadome(_challenge_url, capsolver_keys))
                    if _unblocked:
                        _log(username, "slots: datadome unblocked (interactive) — retrying same proxy")
                        try:
                            raw = await loop.run_in_executor(executor, lambda: client.browser_fetch(
                                SLOTS_URL, {"posto_id": posto_id, "captcha": token},
                                params={"posto_id": posto_id},
                                headers={"X-Requested-With": "XMLHttpRequest", "Referer": sched_jsp_url},
                                timeout=30))
                            slots_text = raw["body"]
                            slots_status = raw["status"]
                            _body_snip = slots_text[:300]
                            if slots_status != 302 and not (
                                "body{margin:0;background:#fff}" in _body_snip
                                or "datadome" in _body_snip.lower()
                            ):
                                break  # unblock worked — got a real response
                            _log(username, "slots: still blocked after interactive unblock")
                        except Exception as _ue:
                            _log(username, f"slots: retry after unblock failed: {_ue}")
                if _slot_attempt >= _SLOTS_PROXY_RETRIES:
                    return "proxy_blocked"
                continue  # rotate proxy and retry
            break  # got a real response

        try:
            slots_json = json.loads(slots_text)
        except Exception:
            # When the response comes via a real browser navigation (browser_form_post),
            # Chrome wraps a raw JSON response in its built-in viewer HTML: <pre>{...}</pre>.
            # Unwrap that before giving up.
            _pre_m = re.search(r"<pre[^>]*>(.*?)</pre>", slots_text, re.DOTALL)
            if _pre_m:
                try:
                    slots_json = json.loads(_pre_m.group(1).strip())
                except Exception:
                    _log(username, f"slots: non-JSON response (pre-unwrap failed): {slots_text[:300]}")
                    return "error"
            else:
                _log(username, f"slots: non-JSON response: {slots_text[:300]}")
                return "error"

        _log(username, f"slots raw: {json.dumps(slots_json)[:600]}")
        await _evidence(client, username, "slots", executor)

        if isinstance(slots_json, dict):
            if slots_json.get("type") == "error":
                _log(username, f"slots: server error: {slots_json.get('description', '')}")
                return "error"
            slots_data = slots_json.get("data") or {}
            if not slots_data:
                _log(username, "slots: empty data -- no appointments available")
                return "no_slot"
            slots_json = [
                {"date": d, "periods": [{"id": p} if not isinstance(p, dict) else p
                                        for p in (ps if isinstance(ps, list) else [ps])]}
                for d, ps in slots_data.items()
            ]
            _log(username, f"slots normalized: {json.dumps(slots_json)[:300]}")

        if not slots_json:
            _log(username, "slots: empty -- no appointments available")
            return "no_slot"

        _log(username, f"slots: {len(slots_json)} dates available")

        # Step 8: Assign slot
        visible_slots = slots_json
        lease = None
        _lease_confirmed = False
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
                    _log(username, f"slot format unknown -- first={first}")
                    return "error"
            except Exception as e:
                _log(username, f"slot parse error: {e}")
                return "error"

        # Slots JSON uses YYYY-MM-DD (dashes); SubmeterVistoCriaPDF form expects YYYY/MM/DD (slashes).
        # HAR confirms: f_date_c=2026%2F07%2F02. Convert before any submission path.
        slot_date_form = slot_date.replace("-", "/")
        _log(username, f"booking slot: date={slot_date} period={slot_period}  (form date={slot_date_form})")
        # No distinct visual state to screenshot here yet — slot selection in this
        # headless flow is a value choice (date/period from the JSON), not a calendar
        # click, so the live page is unchanged from the "slots" evidence shot above.

        # Step 9: SubmeterVistoCriaPDF
        # Same DataDome risk /slots had: a bare primp POST (XHR-equivalent) can be blocked
        # regardless of proxy quality, while a real navigation (form.submit()) is trusted.
        # Try the real-navigation path first; fall back to primp POST if unavailable/blocked.
        resp_text = ""
        _submit_status = 0
        _submit_method = ""
        _submit_url_full = SUBMIT_URL + "?posto_id=" + posto_pdf
        if hasattr(client, "browser_form_post"):
            try:
                _sp_url, _sp_html = await loop.run_in_executor(
                    executor, lambda: client.browser_form_post(
                        _submit_url_full,
                        {"lang": "ENG", "txtHuman": "", "back": "",
                         "f_date_c": slot_date_form, "cmbPeriodo": slot_period},
                        timeout=100))
                _sp_snip = _sp_html[:300]
                _sp_blank = _sp_html.strip() in ("", "<html><head></head><body></body></html>")
                if _sp_blank:
                    _log(username, "SubmeterVisto: browser_form_post landed on blank/chrome-error page — falling back to primp POST")
                elif "body{margin:0;background:#fff}" in _sp_snip or "datadome" in _sp_snip.lower():
                    _log(username, "SubmeterVisto: browser_form_post DataDome-blocked — falling back to primp POST")
                else:
                    resp_text = _sp_html
                    _submit_status = 200
                    _submit_method = "browser_form_post"
            except Exception as _spe:
                _log(username, f"SubmeterVisto: browser_form_post failed: {_spe} — falling back to primp POST")

        if not _submit_method:
            r = await loop.run_in_executor(executor, lambda: client.post(
                SUBMIT_URL, params={"posto_id": posto_pdf},
                data={"lang": "ENG", "txtHuman": "", "back": "",
                      "f_date_c": slot_date_form, "cmbPeriodo": slot_period},
                headers={**sess.HEADERS_XHR, "Referer": sched_jsp_url}, timeout=30))
            resp_text = r.text.strip()
            _submit_status = r.status_code
            _submit_method = "primp"

        _log(username, f"SubmeterVisto -> {_submit_status} via {_submit_method}  body={resp_text[:150]}")
        # Best-effort only: when via primp, this screenshot reflects whatever page the
        # browser last navigated to (the slots/Schedule.jsp page), not the submission
        # result. When via browser_form_post, it IS the actual submission result page.
        await _evidence(client, username, "pdf_submission", executor)

        _known_errors = ("indisponivel", "unavailable", "erro", "error", "bd_problm")
        _has_error_kw = any(kw in resp_text.lower() for kw in _known_errors)
        _looks_ok = (
            "PDF" in resp_text
            or "comprovativo" in resp_text.lower()
            or "agendamento" in resp_text.lower()
            or (len(resp_text) > 100 and not _has_error_kw)
        )

        if _submit_status == 200 and _looks_ok:
            if lease:
                slot_manager.confirm(lease)
                _lease_confirmed = True
            pdf_saved = False
            try:
                # SubmeterVistoCriaPDF's response page has <form name="frm_1"
                # action="MostrarPdf"></form>, auto-submitted via JS (document.frm_1.submit())
                # after ~1.5s -- a real DOM form submission (browser navigation), which is
                # what bypasses DataDome. A raw client.get() to MostrarPdf gets 403-blocked
                # regardless of session health (confirmed live 2026-07-05) even with a
                # correct Referer -- it must go through the browser as a real navigation.
                # page.content() can't be used for the browser path either: Chromium's
                # built-in PDF viewer intercepts application/pdf responses into its own
                # viewer shell, not raw bytes, so submit_form_binary() captures the response
                # via Playwright's response listener instead.
                _pdf_bytes_raw = b""
                _pdf_status = 0
                ct = ""
                if _has_browser and hasattr(client, "submit_form_binary"):
                    try:
                        _pdf_result = await loop.run_in_executor(
                            executor, lambda: client.submit_form_binary(action_contains="MostrarPdf", timeout=45))
                        _pdf_status   = _pdf_result["status"]
                        ct            = _pdf_result["content_type"]
                        _pdf_bytes_raw = _pdf_result["body"]
                        _log(username, f"MostrarPdf via submit_form_binary: status={_pdf_status} ct={ct} len={len(_pdf_bytes_raw)}")
                    except Exception as _sfe:
                        _log(username, f"submit_form_binary failed: {_sfe} — falling back to raw GET")
                if not _pdf_bytes_raw:
                    # Fallback: extract a MostrarPdf URL from the response HTML and GET it
                    # directly. Known to be 403-blocked by DataDome in the common case, but
                    # kept as a last resort for restored/browser-less sessions.
                    _pdf_url: str | None = None
                    _mostrar_patterns = [
                        r'(?:src|href|data)=["\']([^"\']*MostrarPdf[^"\']*)["\']',
                        r'(?:src|href|data)=["\']([^"\']*\.pdf[^"\']*)["\']',
                    ]
                    for _pat in _mostrar_patterns:
                        _m = re.search(_pat, resp_text, re.IGNORECASE)
                        if _m:
                            _raw = _m.group(1)
                            _pdf_url = _raw if _raw.startswith("http") else BASE + (_raw if _raw.startswith("/") else "/" + _raw)
                            _log(username, f"MostrarPdf URL extracted from SubmeterVisto response: {_pdf_url}")
                            break
                    if not _pdf_url:
                        _pdf_url = BASE + "/VistosOnline/MostrarPdf"
                        _log(username, f"MostrarPdf URL not found in response, using fallback: {_pdf_url}")
                    r_pdf = await loop.run_in_executor(executor, lambda u=_pdf_url: client.get(
                        u,
                        headers={**sess.HEADERS_NAV, "Referer": sched_jsp_url},
                        timeout=30))
                    ct = (getattr(r_pdf, "headers", {}) or {}).get("content-type", "")
                    _pdf_bytes_raw = r_pdf.content if hasattr(r_pdf, "content") else r_pdf.text.encode()
                    _pdf_status = r_pdf.status_code
                if _pdf_status == 200 and (
                    "application/pdf" in ct
                    or _pdf_bytes_raw[:4] == b'%PDF'
                    or len(_pdf_bytes_raw) > 5000
                ):
                    pdf_dir = Path(__file__).parent / "data" / "pdfs"
                    pdf_dir.mkdir(exist_ok=True)
                    pdf_path = pdf_dir / f"{username}.pdf"
                    pdf_path.write_bytes(_pdf_bytes_raw)
                    _log(username, f"PDF saved -> {pdf_path}  ({len(_pdf_bytes_raw)} bytes)")
                    pdf_saved = True
                else:
                    _log(username, f"PDF download failed: status={_pdf_status} ct={ct[:60]} len={len(_pdf_bytes_raw)}")
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
            _log(username, "slot already taken -- release and retry later")
            return "no_slot"

        if lease:
            slot_manager.release(lease, f"http_{_submit_status}")
        _log(username, f"SubmeterVisto unexpected response: {resp_text[:200]}")
        return "error"

    except Exception as e:
        _log(username, f"EXCEPTION in apply_book: {e}")
        if slot_manager and lease and not _lease_confirmed:
            try:
                slot_manager.release(lease, f"exception:{type(e).__name__}")
            except Exception:
                pass
        return "error"


# -- poll_slots_once: scout polling (lazy re-warm + /slots) --------------------

async def poll_slots_once(client, proxy: str | None, posto_id: str,
                          acct: dict, nat: str, res: str,
                          capsolver_keys: list[str], anticaptcha_keys: list[str],
                          twocaptcha_keys: list[str], capmonster_keys: list[str],
                          executor: ThreadPoolExecutor) -> list[dict]:
    """
    One scout poll cycle: check alive -> lazy re-warm if form state expired ->
    solve CAPTCHA -> POST /slots -> return normalized slot list.
    Raises SessionExpired if Vistos_sid is dead or re-warm fails.
    """
    import session as sess
    import solver as solvermod
    import session_store

    username  = acct.get("username", "?")
    loop      = asyncio.get_event_loop()
    sched_url = SCHED_JSP + "?posto_id=" + posto_id

    _has_browser = hasattr(client, "browser_nav")

    # Check session alive + Schedule.jsp form state
    if _has_browser:
        # Browser path: page.goto(Schedule.jsp) -- Playwright handles DataDome challenge.
        try:
            await loop.run_in_executor(executor, lambda: client.browser_nav(sched_url, timeout=25))
            sched_body = await loop.run_in_executor(executor, lambda: client.browser_eval(
                "document.documentElement.outerHTML")) or ""
        except Exception as e:
            raise SessionExpired(f"{username}: Schedule.jsp browser_nav failed: {e}")
        if "/ch/bd.js" in sched_body:
            raise SessionExpired(f"{username}: DataDome challenge on Schedule.jsp -- browser session stale")
        at_auth = "Authentication.jsp" in sched_body or 'action="/VistosOnline/login"' in sched_body
        if at_auth:
            raise SessionExpired(f"{username}: redirected to auth on Schedule.jsp")
        schedule_ok = "SubmeterVistoCriaPDF" in sched_body or "posto_id" in sched_body
        if not schedule_ok:
            _log(username, "poll: form state expired -- re-warming steps 2-6 (browser)")
            try:
                await _run_steps_2_to_6(client, posto_id, acct, nat, res, executor)
            except Exception as e:
                raise SessionExpired(f"{username}: re-warm failed: {e}")
    else:
        # primp path (fallback for restored sessions)
        alive = await loop.run_in_executor(executor, session_store.is_alive, client)
        if not alive:
            raise SessionExpired(f"{username}: Vistos_sid dead")
        try:
            r_check = await loop.run_in_executor(executor, lambda: client.get(
                sched_url, headers=sess.HEADERS_NAV, timeout=15, follow_redirects=False))
            schedule_ok = r_check.status_code == 200
            _log(username, f"poll: Schedule.jsp status={r_check.status_code} url={r_check.url}")
        except Exception as e:
            schedule_ok = False
            _log(username, f"poll: Schedule.jsp error: {e}")
        if not schedule_ok:
            _log(username, "poll: form state expired -- re-warming steps 2-6 (primp)")
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

    # POST /slots -- browser path passes DataDome; primp fallback for restored sessions
    try:
        if _has_browser:
            raw = await loop.run_in_executor(executor, lambda: client.browser_fetch(
                SLOTS_URL, {"posto_id": posto_id, "captcha": token},
                params={"posto_id": posto_id},
                headers={"X-Requested-With": "XMLHttpRequest", "Referer": sched_url},
                timeout=30))
            slots_text = raw["body"]
            slots_status = raw["status"]
        else:
            r = await loop.run_in_executor(executor, lambda: client.post(
                SLOTS_URL, params={"posto_id": posto_id},
                data={"posto_id": posto_id, "captcha": token},
                headers={**sess.HEADERS_XHR, "Referer": sched_url}, timeout=30))
            slots_text, slots_status = r.text, r.status_code
        _log(username, f"poll: /slots status={slots_status} body_len={len(slots_text)}")
        import json as _json
        slots_json = _json.loads(slots_text)
    except Exception as e:
        _log(username, f"poll: /slots error: {e}")
        return []

    if isinstance(slots_json, dict):
        if slots_json.get("type") == "error":
            _log(username, f"poll: slots server error: {slots_json.get('description', '')}")
            return []
        slots_data = slots_json.get("data") or {}
        if not slots_data:
            return []
        return [
            {"date": d, "periods": [{"id": p} if not isinstance(p, dict) else p
                                    for p in (ps if isinstance(ps, list) else [ps])]}
            for d, ps in slots_data.items()
        ]
    return slots_json if isinstance(slots_json, list) else []


# -- apply_one: thin wrapper (warmup + book) ------------------------------------

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
                _log(username, "session expired -- re-logging in")
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
