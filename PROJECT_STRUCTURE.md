# Octo Connection — Project Structure & Current State

## Goal

Automated E-VISA appointment booking system for the Portuguese consulate portal (vistosonline.gov.pt). Logs in with real applicant accounts, warms up sessions to the slot selection screen, monitors for available slots, and books appointments the moment they open — downloading confirmation PDFs.

---

## Architecture — Three Layers

```
engine/                     ← Orchestration layer (asyncio)
│   manager.py              — Spawns & supervises all worker coroutines
│   worker.py               — Per-account state machine
│   signals.py              — SlotSignalBus: resettable event + HTTP server :8989
│   __init__.py             — Adds auto_api/ to sys.path
│
auto_api/                   ← Automation layer (FROZEN — do not modify)
│   session.py              — Playwright browser login + fingerprint bypass
│   batch_apply.py          — Full apply workflow steps 1-9
│   session_store.py        — Session checkpoint save/load/probe
│   proxy_pool.py           — PersistentProxyPool (round-robin, cursor persisted to disk)
│   slot_manager.py         — SlotManager atomic lease system
│   solver.py               — CAPTCHA solver race (capsolver/anticaptcha/2captcha/capmonster)
│   batch_warmup.py         — Standalone warmup CLI (fallback)
│   data/
│       accounts.csv        — 196 real applicant accounts (172 verified)
│       test_accounts.csv   — 100 test/fake accounts (94 verified)
│       proxies_soax.txt    — 20,000 SOAX rotating proxies (package 335959)
│       proxies_isp.txt     — 192 static ISP proxies (not yet usable — HTTPS tunnel fails)
│       quest_steps.json    — Portal questionnaire steps per nationality
│       form_defaults.json  — Default form field values
│       sessions/           — Per-account checkpoint JSON files
│       pdfs/               — Downloaded booking confirmation PDFs
│
data/                       ← Identity generation utilities
```

---

## Pipeline — Confirmed Working Stages

| Stage | Status | Notes |
|-------|--------|-------|
| Register | ✅ Confirmed | SOAX HTTP :5000 + Playwright WAF bypass + reCAPTCHA race |
| Verify email | ✅ Confirmed | Browser context fetch (primp alone fails challenge) |
| Login | ✅ Confirmed | Playwright + external ProxyLess solver; score ≥55 on clean IPs |
| Questionnaire (steps 1-6) | ✅ Confirmed | 7 steps CPV/IRL, primp HTTP only after login |
| Schedule.jsp | ✅ Confirmed | ScheduleController multipart body → 302 → Schedule.jsp 200 |
| Slot polling | ✅ Confirmed | `{"data":{}}` = genuine no-slot; CAPTCHA not enforced server-side |
| Session restore | ✅ Confirmed | Cookie + proxy URL checkpoint; alive up to ~9 min idle |
| Scout + signal | ✅ Confirmed | Scouts poll every 300s; `signal_bus.fire()` wakes all real workers |
| Engine layer | ✅ Confirmed | Manager + Worker + SlotSignalBus all proven in V4-V6 tests |
| Booking + PDF | ⏳ Untested | Code matches HAR exactly; needs real open slot to test |

---

## Engine Worker State Machine

```
idle → logging_in → logged_in → warming_up → warmed
                                                │
                                    scout: polling_slots (loop, 300s interval)
                                    real:  awaiting_signal → applying → done/no_slot_exhausted
                                                              ↑ reset ↓
                                              (manager resets signal after all no_slot)
```

**Keepalive:** background task probes session every 18 min. Dead → restore cookies (5 proxy rotations) → re-warmup. Down (5xx) → defer 5 min, no re-login.

**Session restore:** saves `{cookies, proxy_url, posto_id}` to `data/sessions/<username>.json` after Schedule.jsp. On restart, workers with alive checkpoints skip login+warmup entirely.

---

## Run Command (Real Mode)

```bash
cd auto_api
set PYTHONPATH=..
uv run python -m engine.manager \
  --mode real \
  --scouts 15 \
  --count 196 \
  --account-offset 32 \
  --max-lifetime 72000 \
  --login-concurrency 40 \
  > ..\engine_real_run.log 2> ..\engine_real_run_err.log
```

**Monitor:**
```bash
powershell -c "Get-Content ..\engine_real_run.log -Wait"
```

**Signal (manual):**
```bash
curl -X POST http://localhost:8989/signal -H "Content-Type: application/json" -d "{}"
curl http://localhost:8989/status
```

---

## Key Parameters

| Param | Value | Notes |
|-------|-------|-------|
| POSTO_ID | 5084 | Real booking posto (Dublin) |
| TEST_POSTO_ID | 2032 | Test posto (fake accounts only) |
| NATIONALITY | CPV | Cape Verde |
| RESIDENCE | IRL | Ireland |
| POLL_INTERVAL | 300s | Scouts poll every 5 min (portal kills session ~90s after /slots POST) |
| KEEPALIVE_INTERVAL | 1080s | 18 min probe cycle |
| LOGIN_MAX_ATTEMPTS | 25 | Per worker; drops to 5 in critical window (<2h to event) |
| login-concurrency | 40 | Max simultaneous Playwright browsers |

---

## Known Constraints

- **auto_api/ is frozen** — confirmed login/register flows must not be modified. All changes go in engine/ or around them.
- **Audio challenge banned** — clicking audio button triggers "automated queries" bot detection. Never re-enable.
- **SOAX package 335959** — rotating proxies. Traffic can be exhausted; cursor state persisted to `proxies_soax.txt.proxy_state.json`.
- **ISP proxies** — `proxies_isp.txt` has 192 entries but most fail HTTPS CONNECT tunneling (can't reach portal). The `WHnPcVTJPqBz` credential group returns 403 auth failures.
- **aiohttp must be installed** in `auto_api/.venv` for HTTP signal server on :8989. Install: `cd auto_api && .venv\Scripts\python -m pip install aiohttp`
- **2captcha balance** — currently zero. Solver race falls back to capsolver/capmonster/anticaptcha.
- **is_alive false negative** — `probe_session()` in `session_store.py` returns "dead" post-warmup if portal body no longer contains "Questionario"/"logout". Mitigated by 300s poll interval. Cannot fix without unfreezing session_store.py.
- **--account-offset** — applies to test_accounts.csv (scouts) only. Accounts 0-31 used in previous runs.

---

## Solver Keys (from .env)

| Solver | Key |
|--------|-----|
| Capsolver | CAP-D09F9ADF...DA409 |
| AntiCaptcha | 417a6009... |
| 2Captcha | f29a22fb..., 8945d1e8... (ZERO BALANCE) |
| CapMonster | ce7d4529... |

---

## Proxy Sources

| Source | File | Count | Status |
|--------|------|-------|--------|
| SOAX rotating | proxies_soax.txt | 20,000 | ✅ Active (package 335959) |
| ISP static | proxies_isp.txt | 192 | ❌ HTTPS tunnel fails |
| Webshare | proxies_webshare.txt | — | Not tested recently |
