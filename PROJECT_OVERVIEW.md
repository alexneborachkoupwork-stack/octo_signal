# E-VISA Appointment Automation — Project Overview

**Target:** `pedidodevistos.mne.gov.pt` — Portuguese Consular Services portal (Cape Verde applicants)  
**Goal:** Automatically book Schengen visa appointments for 196 real accounts the moment slots open.  
**Date:** 2026-06-08

---

## What This Is

The Portuguese e-VISA portal (`VistosOnline`) is appointment-only. Slots for Cape Verde nationals
open rarely and fill within seconds. The goal is to have ~100+ authenticated sessions already
warmed up and ready to submit booking requests the moment a slot is detected, faster than any
human can react.

---

## Pipeline (Confirmed from HAR)

```
[1] REGISTER     →  [2] VERIFY EMAIL  →  [3] LOGIN  →  [4] QUESTIONNAIRE
                                                                 ↓
[8] PDF STORED   ←  [7] SUBMIT/PDF    ←  [6] SLOT   ←  [5] SCHEDULE FORM
```

| Step | Endpoint | CAPTCHA | Notes |
|------|----------|---------|-------|
| Register | `POST /VistosOnline/Register` | reCAPTCHA v2 Enterprise (REGISTER_EVISA) | External solver works |
| Verify email | Browser click on token URL | None | Requires same browser session |
| Login | `POST /VistosOnline/Authentication.jsp` | reCAPTCHA v2 Enterprise (LOGIN_EVISA) | **Blocker** (see below) |
| Questionnaire | `GET /QuestNextQuestion` × 7 | None | Nationality-dependent steps |
| Schedule form | `POST /ScheduleController` | None | CSRF token required |
| Slot probe | `POST /slots` | reCAPTCHA v3 Enterprise (SCHEDULE_EVISA) | Score-based, invisible |
| Submit | `POST /SubmeterVistoCriaPDF` | None | Different posto_id (5084 vs 5086) |

---

## What We Built

### Core Stack

| Module | Purpose |
|--------|---------|
| `session.py` | Hybrid Playwright + primp session. Playwright bypasses FingerprintJS, extracts cookies, primp does the HTTP work. Browser stays alive for email verification. |
| `solver.py` | CAPTCHA race-all: CapSolver + AntiCaptcha + 2Captcha + CapMonster race in parallel, first token wins. Proxy-aware tasks (not ProxyLess). |
| `account_pool.py` | Thread-safe CSV read/write with per-account status tracking. |
| `proxy_pool.py` | SOAX sticky HTTP proxy pool with burn-on-failure logic. |
| `email_pool.py` | Cloudflare Worker KV email (maillab.live) + mail.tm fallback. |
| `session_store.py` | Persists logged-in primp sessions to disk; warmup reloads them. |
| `slot_manager.py` | Semaphore-based slot allocation; hands one slot to one worker. |
| `slot_detector.py` | Polls `/slots` every 30s; HTTP server on :8989 for human signal injection. |

### Batch Entry Points

| Script | Does |
|--------|------|
| `batch_register.py` | Registers + verifies `new`/`failed` accounts concurrently. |
| `batch_login.py` | Logs in `verified` accounts concurrently, marks `active`. |
| `batch_warmup.py` | Logs in all active accounts, keeps sessions alive, triggers apply on slot signal. |
| `batch_apply.py` | Full questionnaire → form → slot → submit flow per account. |
| `reset_registered.py` | Regenerates credentials for stuck `registered` accounts; resets to `new`. |

### Data Files

| File | Content |
|------|---------|
| `data/accounts.csv` | 208 rows (12 fake + 196 real). Columns: id, username, password, status, first/last name, gender, birthdate, nationality, traveldoc, email, email_pass, proxy, ... |
| `data/proxies_soax.txt` | SOAX HTTP sticky proxies — `http://...-sessionid-{ID}-...:pass@proxy.soax.com:5000` |
| `data/quest_steps.json` | Questionnaire step sequence keyed by nationality (CPV, extendable) |
| `data/form_defaults.json` | Static + dynamic + per-account field mapping for the schedule form |
| `.env` | API keys, posto_id, email domain, solver preference |

---

## Current Account Status

| Status | Count | Meaning |
|--------|-------|---------|
| `verified` | **~119** | Registered + email verified. Ready for login. |
| `registered` | **~29** | Registered on server, email verification failed. Must reset + re-register. |
| `failed` | **~48** | Did not complete registration. Will retry. |
| `active` | 0 | Logged in. None yet. |

> **Goal:** All 196 accounts reach `active` so `batch_warmup` can hold 196 live sessions.

---

## What Works ✓

- **Registration** (step 1): Playwright + proxy bypasses WAF and FingerprintJS.
  External CapSolver handles reCAPTCHA v2 Enterprise checkbox. Server accepts registration.
- **Email verification** (step 2): Cloudflare Worker KV polls inbox, browser clicks the
  verification link. Works reliably.
- **CAPTCHA race**: capsolver + 2captcha + capmonster solve in parallel (anticaptcha was
  unreachable — all keys timeout). First token wins, ~10-30s per solve.
- **SOAX HTTP proxies**: Confirmed working. SOCKS5 at same port fails (Chromium cannot auth
  SOCKS5). HTTP on port 5000 is the format in use.
- **Account regeneration**: When accounts get stuck at `registered`, `reset_registered.py`
  regenerates username + password (old username is burned on server) and resets to `new`.
- **Questionnaire + schedule form**: Implemented in `batch_apply.py`, reverse-engineered from
  HAR capture. Quest steps and form fields externalized to JSON.
- **Slot detector + human signal**: `SlotDetector` polls `/slots` or accepts POST to `:8989/signal`.

---

## What Is Missing / Untested ✗

| Missing | Impact |
|---------|--------|
| Login stage never confirmed working at scale | **Pipeline completely blocked** downstream of step 2 |
| Slot booking + PDF submission | Untested — depends on login working |
| `batch_warmup` end-to-end | Untested — needs active sessions |
| AntiCaptcha connectivity | 7 keys unreachable; each race wastes 30s waiting for timeout |
| 29 stuck `registered` accounts | Need reset + re-register before they can proceed |
| 48 `failed` accounts | Need another registration run |

---

## Blockers

### CRITICAL: Login reCAPTCHA — CONFIRMED FAILING

The login page (`Authentication.jsp`) uses **reCAPTCHA v2 Enterprise** with a visible checkbox.

**Login is confirmed to fail.** Registration works against the same reCAPTCHA v2 Enterprise
widget type — but the login threshold is set meaningfully stricter. This is the most important
asymmetry in the system.

**What we know:**
- External solver tokens (CapSolver, 2Captcha, capmonster) are rejected — `ReCaptchaError`
- `browser_login()` (Playwright clicks the real checkbox in headless Chromium) also fails or
  scores too low — `secblock` or `ReCaptchaError` from the server
- Registration passes with the same proxy + solver setup, confirming the server explicitly
  treats login with a higher score requirement

**Why login is harder than registration:**
- Registration is a one-time action; the server may tolerate moderate scores to allow signups
- Login is the gate to all protected resources; the server enforces a high Enterprise score
  threshold to prevent automated session creation at scale
- Headless Chromium without a real GPU, real user history, and real mouse interaction patterns
  likely scores below the login threshold even with proxy-aware tasks

**Unexplored paths:**
1. High-trust ISP residential proxies (current ISP list is dead; need fresh credentials)
2. Webshare residential proxy with username/password auth (untested)
3. Solving the reCAPTCHA image challenge manually / via AntiCaptcha image-grid solver during
   browser session
4. Injecting a pre-solved token directly into `grecaptcha.enterprise.getResponse` override
   before the login POST fires

### SECONDARY: AntiCaptcha API unreachable

All 7 AntiCaptcha keys timeout (30s each) during every CAPTCHA race. Remove `ANTICAPTCHA_KEYS`
from `.env` to stop the 30s penalty per race until connectivity is restored.

---

## Anti-Bot Stack on the Site

This is the complete detection/protection system as understood from reverse engineering:

### 1. Custom WAF — FingerprintJS BotDetectorLib (`/ch/bd.js`)
- Custom JavaScript challenge served at page load — evaluates browser environment
- Checks: canvas rendering, WebGL, audio context, font enumeration, hardware concurrency,
  `navigator.webdriver`, mouse event timing, etc.
- Headless Chromium without stealth patches fails this challenge
- **Bypass:** Playwright with stealth injection + residential proxy exits. Session cookies are
  then reused in primp for subsequent API calls. The browser must stay alive for verification.
- **Note:** No Cloudflare Bot Management / JS challenge / cf_clearance in play. This is the
  site's own WAF system, not a third-party CDN layer.

### 2. reCAPTCHA Enterprise (Google) — Three Checkpoints

| Checkpoint | Type | Action name | Where |
|-----------|------|-------------|-------|
| Registration | v2 checkbox (visible) | `REGISTER_EVISA` | `/VistosOnline/Register` |
| Login | v2 checkbox (visible) | `LOGIN_EVISA` | `/VistosOnline/Authentication.jsp` |
| Slot booking | v3 invisible (score-based) | `SCHEDULE_EVISA` | `/VistosOnline/slots` |

- Site key: `6LdOB9crAAAAADT4RFruc5sPmzLKIgvJVfL830d4`
- Enterprise scoring factors in IP reputation, browser fingerprint, behavioral signals, and
  historical trust of the Google account/browser profile
- **Registration bypass (confirmed):** External solver with proxy-aware task works — server
  accepts `REGISTER_EVISA` tokens
- **Login bypass (uncertain):** Real browser click works in theory; score threshold may be
  stricter than registration
- **Slot booking bypass (untested):** v3 score-based; same external solver approach should work
  but score threshold unknown

### 3. Unknown Systems (Partially Identified)

At various points the server returns responses that don't map cleanly to the above:
- `type=secblock` on login — may be an internal IP reputation list, separate from reCAPTCHA score
- The login page may load additional fingerprinting JS beyond `bd.js` that is not yet identified
- There may be a behavioral layer (session-level mouse/timing signals) feeding the Enterprise score

These systems are not yet reverse-engineered and may be contributing to login failures.

### 4. Per-IP CAPTCHA Rate Limiting
- Server tracks CAPTCHA attempts per exit IP
- After N failures: `"Dispõe de mais 0 tentativas"` = "You have 0 remaining attempts"
- Forces proxy rotation; no way to reset without IP change
- **Mitigation:** Rotate SOAX session IDs; each sessionid = new exit IP

### 5. CSRF Token (`__RequestVerificationToken`)
- ASP.NET anti-forgery token embedded in the schedule form HTML
- Must be extracted from `GET /VistosOnline/Formulario?copy=true` response
- Expires with the session; re-fetched each apply run
- **Handled:** `_extract_csrf()` in `batch_apply.py`

### 6. Session + Cookie Binding
- Server session cookies (`ASP.NET_SessionId` + Cloudflare cookies) are bound to the proxy IP
- Switching proxy mid-session invalidates the session
- **Handled:** SOAX sticky sessionid binds the exit IP for 3600s; both Playwright and primp
  use the same sessionid URL

### 7. Email Verification Gate
- After registration the account is locked (`registered` state) until the user clicks the
  emailed verification link in a browser (not just an HTTP GET)
- The link carries a one-time token; a browser must navigate to a specific JSP page
- **Handled:** `s.verify_email()` navigates Playwright to the token URL and submits the form

### 8. Login Security Responses
- `type=secblock` — IP is flagged/blocked. Server refuses login regardless of CAPTCHA.
- `type=ReCaptchaError` — Token rejected (score too low or reused)
- `type=EmailSend` — Account is not server-verified; triggers re-send of verification email
- `type=error` — Wrong credentials or rate-limited proxy

---

## Can We Achieve the Final Goal?

**Yes — with high confidence — if login works.**

Registration and email verification are confirmed working. The apply flow (questionnaire →
form → slot → submit) is fully implemented from HAR traces and structurally sound. The
slot detector + warmup orchestrator are complete. The only unconfirmed link in the chain
is the login step.

**Risk assessment:**

| Component | Confidence | Reason |
|-----------|-----------|--------|
| Register + verify | HIGH | Confirmed working for 119/196 accounts |
| Login | LOW — CONFIRMED FAILING | reCAPTCHA v2 Enterprise threshold is stricter than registration; all approaches tried so far rejected |
| Questionnaire + form | HIGH | Reverse-engineered from real HAR, externalized config |
| Slot detection | HIGH | Polling logic is straightforward; human signal override is a fallback |
| Slot submission | MEDIUM | Untested; structurally same pattern as confirmed steps |
| PDF generation | MEDIUM | Untested; single POST; no CAPTCHA at this step |

**The registration step is the hardest WAF/CAPTCHA challenge on this site** — it is
FingerprintJS + Cloudflare + reCAPTCHA v2 Enterprise all active simultaneously — and we
solved it. Login is the same reCAPTCHA type but without the FingerprintJS component (the
session is already established), so it is arguably easier.

**What could still block the final goal:**
1. Login reCAPTCHA scoring — if browser tokens score too low, login fails for every account
2. Slot scarcity — slots may open faster than the booking pipeline can complete
3. SOAX proxy reputation — if exit IPs are flagged at the login step as with `secblock`

**Recommendation:** Run `batch_login.py` for 5 accounts now with `--proxy-type soax` and
check whether any reach `active`. If at least 1 succeeds, the pipeline is viable. If all
return `ReCaptchaError` or `secblock`, the proxy type needs to change before the full run.

---

## Immediate Next Steps

```
Priority 1 (right now):
  uv run python reset_registered.py --account-type real
  uv run python batch_register.py --account-type real --proxy-type soax --concurrency 10 --include-failed
  # → clears the 29+48=77 remaining accounts

Priority 2 (login validation):
  uv run python batch_login.py --account-type real --proxy-type soax --count 5 --dry-run
  uv run python batch_login.py --account-type real --proxy-type soax --count 5
  # → confirms or disproves the login path

Priority 3 (fix anticaptcha):
  # Remove ANTICAPTCHA_KEYS from .env (or blank it)
  # Saves 30s per CAPTCHA race

Priority 4 (if login confirmed):
  uv run python batch_warmup.py --count 50 --concurrency 20 --posto 5086 --trigger-mode signal
  # → load 50 live sessions, human signals slot via :8989/signal
```

---

## Run History

| Run | Accounts | Verified | Registered | Failed | Notes |
|-----|----------|----------|------------|--------|-------|
| Run 1 (ISP proxies) | 122 real | 0 | 0 | 122 | All ISP proxies dead (ERR_SSL) |
| Run 2 (SOAX) | 122 real (39 new + 83 failed) | 47 | 89 | — | UnicodeEncodeError fixed mid-run |
| Reset | — | — | 89→new | — | reset_registered.py regenerated credentials |
| Run 3 (SOAX) | 149 real (97 new + 52 failed) | 72 | 29 | 48 | Clean run; 119 total verified |
