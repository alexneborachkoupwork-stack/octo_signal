"""
Live probe: fresh login via the confirmed pipeline, run the REAL apply_warmup()
to reach an established Schedule.jsp session (same as a production worker), then
hit gettime + getPeriodosOcupados through that context — not a bare fetch, and
not a direct jump right after login, since both may be why every prior attempt
just got the generic "Request could not be processed" page regardless of method.

Usage:
  uv run python probe_gettime_live.py <username>[,<alt-username>...] [posto_id] [residence] [num_days]
"""
import asyncio
import re
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from pathlib import Path

_HERE = Path(__file__).parent
sys.path.insert(0, str(_HERE))
sys.path.insert(0, str(_HERE.parent / "app" / "core"))

import csv


def _tokenize_any(s: str, used: set | None = None) -> list | None:
    """Backtracking: fully tokenize s into distinct period numbers (1-14), 1-digit for
    periods 1-9 and 2-digit for 10-14, in whatever order the string presents them.
    Returns list of ints, or None if the string can't be fully consumed this way."""
    used = set(used or [])
    def rec(i, used):
        if i == len(s):
            return []
        for tok_len in (1, 2):
            if i + tok_len > len(s):
                continue
            chunk = s[i:i+tok_len]
            if chunk[0] == '0':
                continue
            cand = int(chunk)
            if not (1 <= cand <= 14) or cand in used:
                continue
            rest = rec(i + tok_len, used | {cand})
            if rest is not None:
                return [cand] + rest
        return None
    return rec(0, used)


def parse_periodos_ocupados(s: str) -> tuple[list[int], list[int]] | tuple[None, None]:
    """
    The `periodos_ocupados` string is simply the list of OCCUPIED period IDs (1-14),
    concatenated with no separator (1-digit for periods 1-9, 2-digit for 10-14) --
    exactly what the field name says. FREE periods are just whichever of 1-14 never
    appear in the string at all.
    Confirmed 2026-07-05 (posto 3059/FRA): "123467891011121314" (13 tokens, missing 5)
    -> free=[5]. An earlier "free-prefix + ascending-occupied-suffix" theory, based on
    3 examples that happened to all be fully-occupied (all 14 tokens present, so free=[]
    was the correct read), was a misinterpretation -- do not resurrect it.
    Returns (free_periods, occupied_periods) sorted ascending, or (None, None) if the
    string doesn't cleanly decompose into distinct periods 1-14.
    """
    occ = _tokenize_any(s)
    if occ is None:
        return None, None
    free = sorted(set(range(1, 15)) - set(occ))
    return free, sorted(occ)


def _load_account(username: str) -> dict:
    for fname in ("test_accounts.csv", "test_accounts3.csv"):
        with open(_HERE.parent / "app" / "core" / "data" / fname, encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
        for r in rows:
            if r["username"] == username:
                return r
    raise RuntimeError(f"account {username} not found")


def _load_env() -> dict:
    env = {}
    env_file = _HERE / ".env"
    if not env_file.exists():
        env_file = _HERE.parent / "app" / "core" / ".env"
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        env[k.strip()] = v.strip()
    return env


def main():
    # First arg: one username, or comma-separated candidates to try in order
    # (sticky/rotating model — STICKY_RETRY_MAX attempts each, then rotate,
    # matching app/worker.py's real retry policy instead of grinding one account).
    usernames = (sys.argv[1] if len(sys.argv) > 1 else "clafer8430").split(",")
    posto_id  = sys.argv[2] if len(sys.argv) > 2 else "2032"
    residence = sys.argv[3] if len(sys.argv) > 3 else "IRL"
    num_days  = int(sys.argv[4]) if len(sys.argv) > 4 else 5
    STICKY_RETRY_MAX = 2

    import session as sess
    from proxy_pool import PersistentProxyPool

    env = _load_env()
    capsolver_keys = [k.strip() for k in env.get("CAPSOLVER_KEYS", "").split(",") if k.strip()]
    if not capsolver_keys:
        print("ERROR: no CAPSOLVER_KEYS in .env")
        sys.exit(1)

    pool = PersistentProxyPool(str(_HERE.parent / "app" / "core" / "data" / "proxies_soax.txt"))

    print(f"[probe] candidates={usernames}  posto={posto_id}  residence={residence}  "
          f"num_days={num_days}  sticky_retry_max={STICKY_RETRY_MAX}")
    proxy_url = None
    s = None
    username = None
    for cand in usernames:
        acct = _load_account(cand)
        if not acct:
            print(f"  {cand}: not found in test_accounts.csv/test_accounts3.csv — skipping")
            continue
        for attempt in range(1, STICKY_RETRY_MAX + 1):
            proxy_url = pool.advance()
            try:
                s = sess.get_session(proxy_url, headless=True)
            except Exception as e:
                print(f"  {cand} attempt {attempt}/{STICKY_RETRY_MAX}: get_session failed: {e}")
                continue
            try:
                result = s.browser_login(
                    cand, acct["password"], lang="PT",
                    capsolver_keys=capsolver_keys,
                    skip_checkbox=True, min_score=50, timeout=120,
                )
            except Exception as e:
                print(f"  {cand} attempt {attempt}/{STICKY_RETRY_MAX}: login failed: {e}")
                try: s.close_browser()
                except Exception: pass
                s = None
                continue
            import json as _json
            body = (result.get("body") or "").strip()
            try:
                resp = _json.loads(body)
            except Exception:
                resp = {}
            if resp.get("type") in ("", "200", "success"):
                print(f"  {cand}: login OK (attempt={attempt})  proxy={proxy_url.split('@')[-1]}")
                username = cand
                break
            print(f"  {cand} attempt {attempt}/{STICKY_RETRY_MAX}: login rejected: {body[:150]}")
            try: s.close_browser()
            except Exception: pass
            s = None
        if username:
            break
        print(f"  {cand}: exhausted {STICKY_RETRY_MAX} sticky attempts — rotating to next candidate")
    if not username:
        print("ERROR: all candidate accounts exhausted")
        sys.exit(1)

    def _safe(txt: str) -> str:
        return txt.encode("ascii", "replace").decode("ascii")

    # --- Real warmup: reach Schedule.jsp exactly like a production worker does,
    # instead of jumping straight from login to gettime out of context. Every prior
    # attempt (bare fetch AND real navigation) got the same generic blocked page
    # regardless of method — this isolates whether missing Schedule.jsp context,
    # not the fetch/nav distinction, is what's actually gating these endpoints.
    from batch_apply import apply_warmup
    acct = _load_account(username)
    executor = ThreadPoolExecutor(max_workers=4)
    print(f"\n[probe] running real apply_warmup() for {username} to reach Schedule.jsp...")
    warmup_result = asyncio.run(apply_warmup(
        acct, posto_id, capsolver_keys, [], [], [], executor,
        nationality=acct.get("nationality", "CPV"),
        residence=residence,
        client=s, proxy=proxy_url,
    ))
    if isinstance(warmup_result, dict):
        print(f"  warmup OK: posto_pdf={warmup_result.get('posto_pdf')}  sched_url={warmup_result.get('sched_url')}")
    else:
        print(f"  warmup did NOT reach Schedule.jsp: {warmup_result!r} -- gettime/getPeriodosOcupados will still be tested, but without proper context")

    # --- gettime ---
    # Real navigation (browser_nav = page.goto), not browser_fetch/XHR: a bare fetch()
    # gets DataDome-blocked regardless of proxy/session health (confirmed live: 403 on
    # every attempt). browser_form_post/browser_nav run bd.js as a trusted top-level
    # navigation, which is how /slots and Formulario bypass the same wall.
    gt_url = f"https://pedidodevistos.mne.gov.pt/VistosOnline/gettime?id_posto={posto_id}"
    print(f"\n[probe] browser_nav GET {gt_url}")
    try:
        final_url = s.browser_nav(gt_url, timeout=40)
        gt_body = s.browser_eval("document.documentElement.outerHTML") or ""
        print(f"  final_url={final_url}  len={len(gt_body)}")
        print(f"  snippet={_safe(gt_body[:500])!r}")
    except Exception as e:
        print(f"  gettime browser_nav failed: {e}")
        gt_body = ""

    # Try both old JS-array format and new JSON <pre> format
    special_days = {}
    for year, month, days_str in re.findall(
        r"SPECIAL_DAYS\[(\d{4})\]\[(\d+)\]\s*=\s*new Array\((.*?)\);", gt_body
    ):
        days = {int(d.strip("'\"")) for d in days_str.split(",") if d.strip()}
        special_days.setdefault(int(year), {})[int(month)] = days
    if special_days:
        print(f"  parsed SPECIAL_DAYS (old JS-array format): {special_days}")
    else:
        pre_m = re.search(r"<pre[^>]*>([\s\S]*?)</pre>", gt_body)
        if pre_m:
            import json as _json
            try:
                data = _json.loads(pre_m.group(1).strip())
                print(f"  parsed as JSON (new format): keys={list(data.keys()) if isinstance(data, dict) else type(data)}")
                print(f"  {_json.dumps(data, indent=2)[:800]}")
            except Exception as e:
                print(f"  <pre> content not JSON: {e}  content[:300]={pre_m.group(1)[:300]!r}")
        else:
            print("  no SPECIAL_DAYS array and no <pre> JSON found -- unknown format")

    # --- getPeriodosOcupados for next 5 weekdays ---
    today = date.today()
    candidates = []
    cur = today + timedelta(days=1)
    while len(candidates) < num_days:
        y, m, d = cur.year, cur.month, cur.day
        if cur.weekday() < 5 and d not in special_days.get(y, {}).get(m, set()):
            candidates.append(cur)
        cur += timedelta(days=1)
    print(f"\n[probe] candidate days to check: {[str(d) for d in candidates]}")

    found_slot = None  # (date_obj, period_int) -- first free slot seen, used for the live apply test below
    for d in candidates:
        date_str = d.strftime("%Y/%m/%d")
        po_url = (f"https://pedidodevistos.mne.gov.pt/VistosOnline/"
                  f"getPeriodosOcupados?id_posto={posto_id}&data_agendamento={date_str}")
        print(f"\n[probe] browser_nav GET {po_url}")
        try:
            po_final_url = s.browser_nav(po_url, timeout=30)
            po_body = s.browser_eval("document.documentElement.outerHTML") or ""
            print(f"  final_url={po_final_url}  len={len(po_body)}")
            print(f"  body={_safe(po_body[:500])!r}")
            m = re.search(r"periodos_ocupados='(\d*)", po_body)
            if m:
                free, occ = parse_periodos_ocupados(m.group(1))
                if free is not None:
                    print(f"  occupied={occ}  FREE={free}")
                    if free and found_slot is None:
                        found_slot = (d, free[0])
                else:
                    print(f"  could not decompose periodos_ocupados string: {m.group(1)!r}")
            else:
                pre_m = re.search(r"<pre[^>]*>([\s\S]*?)</pre>", po_body)
                if pre_m:
                    import json as _json
                    try:
                        data = _json.loads(pre_m.group(1).strip())
                        print(f"  parsed as JSON: {data}")
                    except Exception as e:
                        print(f"  <pre> not JSON: {e}")
        except Exception as e:
            print(f"  getPeriodosOcupados failed: {e}")

    # --- Cross-check: does /slots agree, in this SAME warmed session? ---
    # getPeriodosOcupados just reported periods 3/4/5 free on several dates while every
    # prior /slots check this session returned empty -- confirm that's a real
    # discrepancy within one session, not an artifact of comparing across separate runs.
    print(f"\n[probe] cross-check: /slots?posto_id={posto_id} in the SAME session")
    try:
        import solver as solvermod
        token = solvermod.race_all(capsolver_keys, [], [], [], "SCHEDULE_EVISA", proxy_url, min_score=50)
        print(f"  captcha token obtained  len={len(token)}")
        slots_url = f"https://pedidodevistos.mne.gov.pt/VistosOnline/slots?posto_id={posto_id}"
        sp_url, sp_html = s.browser_form_post(slots_url, {"posto_id": posto_id, "captcha": token}, timeout=100)
        print(f"  browser_form_post -> {sp_url}  len={len(sp_html)}")
        print(f"  body={_safe(sp_html[:500])!r}")
    except Exception as e:
        print(f"  /slots cross-check failed: {e}")

    # --- Live apply test: submit SubmeterVistoCriaPDF against the slot getPeriodosOcupados
    # found free (server-side is the only real arbiter of whether it's genuinely bookable --
    # /slots disagreeing doesn't settle it either way). Mirrors app/core/batch_apply.py's
    # confirmed-correct Step 9 payload/URL, reverse-engineered from login2pdf.har.
    if found_slot:
        slot_date, slot_period = found_slot
        slot_date_form = slot_date.strftime("%Y/%m/%d")
        print(f"\n[probe] LIVE APPLY TEST: date={slot_date}  period={slot_period}  posto_pdf={warmup_result.get('posto_pdf')}")
        try:
            submit_url = (f"https://pedidodevistos.mne.gov.pt/VistosOnline/"
                          f"SubmeterVistoCriaPDF?posto_id={warmup_result.get('posto_pdf', posto_id)}")
            sub_url, sub_html = s.browser_form_post(
                submit_url,
                {"lang": "ENG", "txtHuman": "", "back": "",
                 "f_date_c": slot_date_form, "cmbPeriodo": str(slot_period)},
                timeout=100)
            print(f"  SubmeterVistoCriaPDF -> {sub_url}  len={len(sub_html)}")
            print(f"  body={_safe(sub_html[:1500])!r}")

            # The confirmation page has <form name="frm_1" action="MostrarPdf"></form>,
            # auto-submitted via JS after ~1.5s (document.frm_1.submit()). That's a real
            # DOM form submission (browser navigation), which is what bypasses DataDome --
            # a raw primp/requests GET to MostrarPdf gets 403-blocked regardless of session
            # health (confirmed live). submit_form_binary() does the same DOM submit, but
            # captures the raw response bytes via Playwright's response listener instead of
            # page.content() (which would only return Chromium's PDF-viewer shell, not the
            # actual file, since the response is application/pdf not text/html).
            pdf_result = s.submit_form_binary(action_contains="MostrarPdf", timeout=45)
            status, ct, pdf_bytes = pdf_result["status"], pdf_result["content_type"], pdf_result["body"]
            print(f"  MostrarPdf (submit_form_binary) -> {pdf_result['url']}  status={status}  content-type={ct}  len={len(pdf_bytes)}")
            if status == 200 and ("application/pdf" in ct or pdf_bytes[:4] == b"%PDF"):
                out_path = _HERE / f"probe_result_{username}.pdf"
                out_path.write_bytes(pdf_bytes)
                print(f"  *** PDF SAVED: {out_path} ({len(pdf_bytes)} bytes) ***")
            else:
                print(f"  PDF download did not look like a real PDF: {_safe(str(pdf_bytes[:300]))!r}")
        except Exception as e:
            print(f"  LIVE APPLY TEST failed: {e}")
    else:
        print("\n[probe] no free slot found -- skipping live apply test")

    try:
        s.close_browser()
    except Exception:
        pass
    print("\n[probe] DONE")


if __name__ == "__main__":
    main()
