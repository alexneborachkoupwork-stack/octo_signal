# Confirmed Booking Pipeline — 2026-07-06

**Status: END-TO-END CONFIRMED.** Two real appointments booked successfully through this exact
path (see "Confirmed live bookings" below). This supersedes the "Booking + PDF: Untested" line in
`PROJECT_STRUCTURE.md` and `STATUS_REPORT.md`'s BLOCKER 3 — both are now resolved.

## The confirmed path

```
signal fires
  → worker already warmed (login → warmup → Schedule.jsp, unchanged cornerstone flow)
  → gettime?id_posto=X            (real navigation — browser_nav, NOT fetch/primp)
  → getPeriodosOcupados per date  (real navigation — browser_nav, NOT fetch/primp)
      → parse occupied-periods string (see algorithm below) → find a FREE period
  → [optional] /slots?posto_id=X  (secondary cross-check / session-state validation only —
                                    NOT trusted as the source of truth for availability)
  → SubmeterVistoCriaPDF POST (browser_form_post — real navigation, f_date_c + cmbPeriodo)
  → confirmation page auto-submits <form name="frm_1" action="MostrarPdf">
  → MostrarPdf via submit_form_binary() (page.route() interception — real navigation,
                                          captures raw PDF bytes reliably)
  → PDF saved
```

### Why `/slots` alone is not sufficient

`/slots` was found to under-report availability. In a same-session, same-account cross-check,
`getPeriodosOcupados` reported free periods while `/slots` returned `{"data":{}}` for the identical
posto/date/session. Confirmed twice with real bookings (see below) — the server accepted dates that
`/slots` claimed had no availability. **Use `gettime` + `getPeriodosOcupados` as the primary
discovery mechanism; keep `/slots` only as a secondary session-state check**, not a source of truth.

### Why every one of these calls must be a real browser navigation

DataDome/WAF blocks bare `fetch()`/XHR calls and raw `primp`/`requests` GETs on every endpoint in
this list, **regardless of session health or proxy quality** — confirmed repeatedly this session.
Only real top-level navigations (`browser_nav`, `browser_form_post`, or a DOM `form.submit()` via
`submit_page_form`/`submit_form_binary`) run `bd.js` and get trusted. A raw GET to `MostrarPdf`, for
example, gets a 403 every time even from a fully warmed, otherwise-successful session.

## The `periodos_ocupados` parsing algorithm (get this wrong and you silently mis-book)

The field name means "occupied periods" and that's literally what it is — **do not** believe the
earlier "free-prefix + ascending-occupied-suffix" theory that briefly circulated this session; it
was a misread of examples that happened to be fully-occupied strings. The real rule:

> The string is the list of OCCUPIED period IDs (1-14), concatenated with no separator
> (1 digit for periods 1-9, 2 digits for 10-14), in whatever order the server emits them.
> **Any period number from 1-14 that never appears in the string at all is FREE.**
> An empty string (`periodos_ocupados=''`) means ALL 14 periods are free.

```python
# probes/probe_gettime_live.py — parse_periodos_ocupados()
"123467891011121314"   # 13 tokens: 1,2,3,4,6,7,8,9,10,11,12,13,14 -> missing 5 -> FREE=[5]
"3451267891011121314"  # 14 tokens, all of 1-14 present            -> FREE=[]  (fully booked)
""                      # 0 tokens                                  -> FREE=[1..14] (wide open)
```

Tokenizing requires backtracking (not naive greedy 2-digit-first), since e.g. `"12"` at the start of
a token stream could be the single number 12, or the two numbers 1 then 2 — greedy-2-digit-first
gets this wrong and silently drops valid parses. See `_tokenize_any()` in the probe for the working
implementation.

## Confirmed live bookings (2026-07-05/06)

These are **real** appointments in the production government system, not simulated:

| Account | Posto | Date | Period | Time |
|---|---|---|---|---|
| `learod3888` | 3059 | 2026-07-06 | 5 | 11:00-12:00 |
| `siltav4922` | 2032 | 2026-07-16 | 4 | 10:00-11:00 |

Both used test identities. Track these if the appointments ever need to be cancelled or followed up.

## Bug fixes landed this session

1. **`browser_fetch()` was silently broken, always** — `_do_browser_fetch()` in `session.py` called
   `page.evaluate(js, timeout=25000)`; Playwright's `Page.evaluate()` has no `timeout` kwarg at all.
   Every call unconditionally raised, silently falling back to a weaker `primp` POST (e.g. every
   `ScheduleController` submission this whole time went through the untrusted fallback path, not the
   intended trusted-browser path). Fixed: removed the invalid kwarg (outer queue timeout already
   enforces the real timeout).
2. **Login `lang` mismatch** — a diagnostic probe hardcoded `lang="ENG"` where the working
   reference (`test_login_soax.py`) defaults to `lang="PT"`. Caused near-total login failure in
   the probe alone; not a pipeline bug, but a reminder that `lang` must be `"PT"` for login.
3. **Sticky/rotating retry model replaces flat 10-15 attempt grinding** — the portal only tolerates
   ~2 login attempts per account before a cooldown. `STICKY_RETRY_MAX=2` in `app/worker.py`, with
   accounts grouped by `identity_id` (new CSV column, defaults to `username` for 1:1 backward compat)
   so a `Worker` can rotate to an alternate account for the same identity instead of grinding the same
   one. See "Identity architecture" below.
4. **`_restore()` always redid the full warmup, even when a checkpoint already had `posto_pdf`** —
   `_restore_cookies()` already restores `posto_pdf`/`sched_url` from the saved `schedule_jsp`
   checkpoint, but `_restore()` called `_phase_warmup()` unconditionally afterward anyway, redoing
   the entire Questionario→Formulario→ScheduleController chain from scratch. Fixed: skip re-warmup
   when the checkpoint already gave us `posto_pdf`; `_ensure_form_state()` re-verifies real
   server-side readiness before every apply attempt regardless, so this is safe.
5. **Quest-step per-step retry** — `_run_steps_2_to_6` in `batch_apply.py` now actually checks each
   quest step's HTTP status (previously only logged it) and retries the individual step up to 3x
   before moving on — the automated equivalent of the manual "page got stuck, had to refresh"
   behavior observed on the real portal UI.
6. **PDF download via `page.route()` interception, not `expect_response().body()`** — Chrome/CDP can
   evict a main-frame navigation's response body before Playwright retrieves it, especially for a
   navigation to a binary (`application/pdf`) resource (`Protocol error (Network.getResponseBody): No
   resource with given identifier found`). `submit_form_binary()` in `session.py` now uses
   `page.route()` + `route.fetch()` + `route.fulfill()`, which captures the body reliably during the
   request itself rather than after the fact.

## Identity architecture (accounts CSV)

`identity_id` column added to `test_accounts.csv`/`accounts.csv` (backward-compatible: defaults to
`username` if the column or value is missing, so 1 account = 1 identity unless populated otherwise).
`app/cli.py`/`app/scout.py`'s `_load_accounts()` groups rows by `identity_id` into one `Worker` per
identity holding a list of candidate accounts, tried in row order. See `app/worker.py`'s
`_advance_candidate()` / `STICKY_RETRY_MAX` docstrings for the full retry semantics.

## What to avoid

- **Do not trust `/slots` alone for availability** — confirmed to under-report; treat it as a
  secondary check only.
- **Do not use `browser_fetch`/raw `primp`/`requests` for any of these endpoints** — always a real
  navigation (`browser_nav`, `browser_form_post`, `submit_page_form`, `submit_form_binary`).
- **Do not resurrect the "free-prefix ascending-suffix" `periodos_ocupados` parsing theory** — it was
  wrong; use the "missing number = free" rule above.
- **Do not grind 10-15 login attempts on one account** — the portal cooldowns after ~2; use the
  sticky/rotating model.
- **Do not run this probe/pipeline against a proxy pool with a stale cursor position** — the SOAX
  pool was scaled to 10,000 unique sessions (`sessionlength=3600`) on 2026-07-04/05 specifically
  because repeat-lapping a small pool degrades success rate; regenerate if the cursor approaches the
  pool size again.
- **Live-apply testing has real side effects** — `SubmeterVistoCriaPDF` submissions book real
  appointments. Treat `probes/probe_gettime_live.py`'s live-apply step as a real transaction, not a
  dry run (Claude Code's safety classifier will block autonomous execution of this step for exactly
  this reason — a human must run it directly).

## Reusable diagnostic tool

`probes/probe_gettime_live.py` — logs in (sticky/rotating), runs real `apply_warmup()` to reach
Schedule.jsp, checks `gettime`/`getPeriodosOcupados` with the corrected parser, cross-checks
`/slots` in the same session, and (if a free slot is found) submits `SubmeterVistoCriaPDF` and
downloads the PDF via `submit_form_binary()`. Usage:

```
uv run probe_gettime_live.py "<user1>[,<user2>,...]" [posto_id] [residence] [num_days]
```

## Proxy pool status (2026-07-05)

- `app/core/data/proxies_soax.txt` (+ `probes/data/proxies_soax.txt`): 10,000 unique sessions,
  `sessionlength=3600` (1h sticky window, up from 300s).
- Format: `http://package-335959-sessionid-{16-char}-sessionlength-3600:{password}@proxy.soax.com:5000`
  — no index prefix (an earlier draft accidentally included a stray `N\t` tab-index prefix per line,
  which broke `ProxyPool._normalise()`; confirmed fixed).
