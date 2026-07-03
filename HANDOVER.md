# Project Handover — E-VISA Appointment Automation
## Portuguese Visa Portal (pedidodevistos.mne.gov.pt)

---

## What This Project Is

Automation of the Portuguese Ministry of Foreign Affairs e-VISA appointment booking portal:

> **https://pedidodevistos.mne.gov.pt/VistosOnline/**

The goal is to detect open appointment slots and book them the moment they become available, across many accounts simultaneously. The portal is JSP-based, protected by **DataDome** (bot detection) and **Google reCAPTCHA v2 Enterprise**. Slots open at unpredictable times and fill within seconds.

---

## What The Portal Looks Like (Technical)

- JSP login page at `/VistosOnline/Authentication.jsp`
- Registration flow: AJAX state machine (3 DOM states, no full page navigations)
- Login: credential POST via `doLogin()` AJAX → JSON response `{"type":"success"}` or `{"type":"error"}`
- Questionnaire: 7-step form sequence → `Formulario.jsp` → `ScheduleController` → `Schedule.jsp`
- Slots: `POST /slots?posto_id=X` → `{"data": {}}` (no slots) or `{"data": {"...": {...}}}` (slots available)
- Booking: `SubmeterVistoCriaPDF` → `MostrarPdf` (PDF download)

### Key Security Layers
| Layer | Detail |
|-------|--------|
| **DataDome** | Bot detection on every request — blocks by IP reputation and browser fingerprint. bd.js PoW challenge on suspicious IPs. |
| **reCAPTCHA v2 Enterprise** | Sitekey `6LdOB9crAAAAADT4RFruc5sPmzLKIgvJVfL830d4`, action `LOGIN_EVISA` (login) / `REGISTER_EVISA` (register). Score-based — portal uses score threshold, not just token validity. |
| **Session binding** | `Vistos_sid` cookie + SOAX/proxy session ID must match between warmup and booking. |
| **CSRF tokens** | All form POSTs require server-issued CSRF token (56-char, single-use). |

---

## What We Built (Playwright / API Approach)

### Architecture
A Python async pipeline:
1. **Account pool** — registered & verified accounts (200+)
2. **Proxy pool** — rotating residential proxies (SOAX, Webshare SOCKS5)
3. **CAPTCHA solvers** — parallel race: capsolver + 2captcha + capmonster
4. **Browser workers** (Playwright) — each worker logs in, warms up to `Schedule.jsp`, then waits
5. **Scout workers** — continuously poll `/slots` to detect availability
6. **Signal bus** — when scout finds slots, fires HTTP signal → all real workers wake and attempt booking
7. **Session keepalive** — sessions expire ~30 min; workers re-warm automatically

### What Is Fully Confirmed Working

| Stage | Status | Notes |
|-------|--------|-------|
| **Account registration** | ✅ Confirmed | SOAX HTTP + proxy-aware reCAPTCHA solve → `{"type":"success"}` |
| **Email verification** | ✅ Confirmed | mail.tm temp inboxes; browser context `fetch()` for token URL |
| **Login** | ✅ Confirmed | Playwright browser + ProxyLess reCAPTCHA + `page.evaluate(fetch(...))` POST |
| **Questionnaire → Schedule.jsp** | ✅ Confirmed | 7-step form, Formulario, ScheduleController all working |
| **Session restore / keepalive** | ✅ Confirmed | Cookie swap + re-warmup < 60s per cycle |
| **Slot polling** | ✅ Confirmed | `/slots` endpoint reached, `{"data":{}}` = genuine no-slot response |
| **Scout → Signal → Book trigger** | ✅ Confirmed | Signal bus fires, workers wake and attempt booking |
| **PDF download** | ❌ Not tested | Code written; no open slots encountered during development |

### What Blocks Production Today

1. **Proxy IP reputation** — DataDome blocks most proxy IPs on login POST. Only clean residential IPs (specific SOAX sessions, specific Webshare SOCKS5 country blocks) pass. The "clean" IPs burn quickly and rotate.
2. **No open slots yet** — `{"data":{}}` across all tested postos (Dublin/3088, Paris/3059, Paris/2032). Slots appear to be time-gated (released at specific server-side schedules, not always available).
3. **reCAPTCHA score variability** — capsolver returns scores of 10–70 for the same site; score=55 works on clean IPs; proxy IP reputation overrides token quality entirely.
4. **2captcha zero balance** — only capsolver active throughout testing.

### Key Technical Findings

**Proxy is the #1 variable.** Not CAPTCHA score. A score=55 token on a clean IP succeeds; a score=100 token on a dirty IP fails. Every proxy-related decision cascades to login success rate.

**ProxyLess mode for login CAPTCHA.** Webshare uses a gateway hostname (`p.webshare.io`), not a direct IP — proxy-aware solver tasks reject it. Use ProxyLess (capsolver solves from its own clean IPs).

**DataDome WAF bypass.** When DataDome serves `bd.js`, the PoW challenge parameters (nonce, token, difficulty=5) are embedded in the page HTML. Solve via SHA256 (`nonce+token+str(p)`) and POST from browser context (not Python HTTP client — server ties the challenge to the TCP connection).

**Session binding.** Save the exact proxy URL (SOAX session ID) used during warmup. Restore it on keepalive. Mismatched proxy = instant session kill.

**Slots are real empty.** `{"data":{}}` is NOT a DataDome block. The portal genuinely has no appointments available for tested postos. Confirmed by manual browsing.

---

## The Extension Approach (Attempted, Incomplete)

We began a Chrome extension approach before switching:

### What Was Built
- MV3 Chrome extension skeleton (`background.js`, `content.js`, `popup.html`)
- Registration state machine in content script (AJAX page state tracking)
- Alert interception (native `window.alert` override via main-world script injection, dispatches `CustomEvent`)
- Extension state via `chrome.storage.local` (workflow-type, workflow-step, person data, email token)
- Extension loaded into **Octo Browser** profiles

### Why It Was Not Completed
- Octo Browser's multi-profile management added complexity without benefit for this use case
- The Playwright-based approach (direct CDP + API) was faster to iterate and debug
- Extension approach was deprioritized when the API-level pipeline proved viable

### Why Competitors Use AdsPower + Extension

Competitors are succeeding with **AdsPower + Chrome Extension** instead. The key reasons:

1. **Genuine browser fingerprint** — AdsPower profiles have unique, persistent fingerprints (canvas, WebGL, fonts, screen, timezone, hardware). Each profile looks like a real individual user to DataDome.
2. **No programmatic fingerprint leakage** — Extensions run inside the real browser render process; no CDP automation markers that headless Playwright exposes.
3. **Human-mimicking timing** — Extension actions use real DOM events with natural delays; bot detectors see normal event timing.
4. **Persistent sessions** — AdsPower profiles maintain cookies, localStorage, and fingerprint across restarts — exactly like a returning human user.
5. **Proxy isolation** — Each AdsPower profile is paired with one proxy, so DataDome associates a consistent IP+fingerprint combination (not a rotating IP with a static fingerprint).

---

## Guidelines for AdsPower + Extension Approach

### AdsPower Setup

- Use one profile per account. Pair each profile with a **dedicated residential proxy** (not rotating).
- Set the profile's timezone to match the proxy's exit country (e.g. proxy in France → `Europe/Paris`).
- Set language to `pt-PT` or `fr-FR` matching the user's expected locale.
- Enable hardware fingerprint randomization per profile (canvas, WebGL noise).
- Do NOT reuse a profile that has previously been DataDome-challenged. Start fresh.

### Extension Architecture (MV3)

The extension must implement a state machine across page navigations using `chrome.storage.local` — the only persistent store that survives navigations:

```
popup click → background.js → generate person + create temp email → navigate to portal
  content.js (home page) → switch language → click login link → update step="auth"
  content.js (auth page) → click register → fill form → solve CAPTCHA → submit → update step="token"
  content.js (token page) → poll mail.tm → fill token → submit → mark done → export CSV
```

**Critical patterns:**
- `chrome.storage.local` for all cross-navigation state — never in-memory variables
- Use `chrome.tabs.onUpdated` to re-attach content logic after navigation
- Override `window.alert` in the page's main world (inject `<script>` tag) and relay to content via `CustomEvent` — MV3 content scripts cannot intercept native alerts directly

### reCAPTCHA Handling in Extension Mode

In AdsPower, the browser profile has a real fingerprint, which means reCAPTCHA may **auto-solve** (the checkbox passes without an image challenge) if the IP is clean enough. This is the key advantage over the Playwright approach.

If auto-solve does not pass:
- Do NOT attempt audio challenge — it is a flagged bot signal
- Integrate an external solver via `chrome.runtime.sendMessage` from content → background, which calls capsolver/2captcha API
- Inject the token directly: `document.getElementById('g-recaptcha-response-1').value = token`
- Fire the callback: `window.onCaptchaSuccess(token)` or `___grecaptcha_cfg.clients[0].aa.aa.callback(token)`

### Proxy Requirements

DataDome blocks proxy ASNs, not just individual IPs. Requirements:
- **Residential proxies only** — datacenter IPs are blocked by DataDome instantly
- **Dedicated per profile** — rotating proxies cause fingerprint/IP mismatch, triggering DataDome
- **European exit nodes** (France, Portugal preferred) — matches the expected applicant geography
- **Mobile proxies** are the strongest option — mobile ASNs (4G/5G) are rarely blocked by DataDome
- Test each IP against the portal before assigning to a profile: `curl --proxy <proxy> https://pedidodevistos.mne.gov.pt/VistosOnline/ -I` — HTTP 200 = clean, any redirect or challenge = burned

### Slot Timing

Slots are not always available — the portal releases appointments at specific times (believed to be early morning Lisbon time). A scout loop must continuously poll `/slots` for each `posto_id`. Confirmed postos:

| Posto ID | Location |
|----------|----------|
| 3088 | Paris |
| 3059 | Paris (2nd) |
| 2032 | Dublin |
| 5084 | Cape Verde |

When slots appear, the booking window is **seconds**. The extension must be able to trigger the booking flow immediately from the scout signal.

### Confirmed Form Selectors

These are stable and confirmed against the live portal:

| Field | Selector |
|-------|----------|
| Name | `#name` |
| Surname | `#surname` |
| Username | `#username` |
| Password | `#password` |
| Confirm password | `#valPassword` |
| Gender | `#gender` (select: M/F/O) |
| Birth date | `#bday` (format: `yyyy/mm/dd`) |
| Nationality | `#nationality` (3-letter ISO) |
| Travel document | `#traveldoc` |
| Email | `#email` |
| reCAPTCHA textarea | `#g-recaptcha-response-1` |
| Privacy submit | `#registroSubmit` |
| Register trigger | `span[name='registration']` (AJAX, loads form into `#mainContent`) |

---

## What To Avoid

| ❌ Don't | Why |
|----------|-----|
| Audio CAPTCHA challenge | Flagged as automated query — triggers stricter bot scoring |
| Rotating proxies per session | Fingerprint/IP mismatch = DataDome block |
| Datacenter / VPN IPs | ASN-blocked by DataDome instantly |
| Reusing accounts across proxy changes | Portal ties session to IP; mismatch = logout |
| Clicking checkbox hoping for auto-pass | Only works with very clean IPs; always have solver ready |
| Reusing a DataDome-challenged profile | The fingerprint is flagged; start a new profile |
| Sharing one proxy across multiple profiles | DataDome associates many accounts with one IP = flagged |

---

## Summary

This project fully confirmed the booking pipeline from registration through slot polling. The remaining gap is slot availability (none observed during development) and a reliable proxy strategy at scale.

The **AdsPower + extension** approach that competitors use addresses the root DataDome problem differently: by presenting a genuine, persistent browser fingerprint paired with a dedicated proxy, rather than managing fingerprint simulation programmatically. For a team taking that direction, the selectors, form flow, CAPTCHA integration patterns, and portal behavior documented here are directly applicable — the portal does not change, only the automation layer does.
