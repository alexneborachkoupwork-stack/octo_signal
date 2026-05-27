# Manager ↔ Extension — WebSocket Integration Reference

## Overview

The Octo Probe extension connects **outward** to the manager's WebSocket server.  
The manager is the **server**; the extension is the **client**.

```
Manager WS Server (you)
        ↑  commands
        ↓  events
   Extension (client)
```

The extension auto-connects on startup using the URL in `extension/config.json`:
```json
{ "managerWsUrl": "ws://localhost:9000" }
```

Reconnect is automatic (5 s back-off). All frames are **JSON text**.

---

## Connection Lifecycle

```
Extension starts
    └─ connects to ws://localhost:9000
    └─ sends  → { "type": "hello", "version": "3.0.0" }

[workflow runs…]
    └─ sends  → { "type": "warmup-ready", "ok": true, "idleStep": "login" }
    └─ sends  → { "type": "apply-done", "ok": true }

Extension disconnects (browser closed / service worker killed)
    └─ TCP close — manager should keep listening, extension reconnects in 5 s
```

---

## Commands: Manager → Extension

### Workflow Commands

---

### `register`
Creates a new account (email + registration form + token verification).

```jsonc
{
  "type": "register",

  // ── Person data (omit for auto-generated fake identity) ───────────────────
  "realPerson": {
    "firstName":   "ADILENE MEGANE",
    "lastName":    "BRITO SOARES",
    "dob":         "07-05-2009",   // DD-MM-YYYY | DD/MM/YYYY | DD MON YYYY
    "gender":      "F",            // "F" | "M"
    "nationality": "CPV",
    "traveldoc":   "PA551574"
    // surnameAtBirth, placeOfBirth → hard-coded "+" by the extension
  },
  // realPerson omitted → extension generates random fake identity

  // ── Captcha solver ────────────────────────────────────────────────────────
  "captchaSolver":   "capsolver",    // "capsolver" | "anti-captcha" | "capmonster" | "2captcha"
  "captchaParallel": false,          // true = race all solvers simultaneously

  // ── Email provider ────────────────────────────────────────────────────────
  "emailProvider": "mailtm",         // "mailtm" | "cloudflare"
  "cfDomain":      "",               // required if emailProvider="cloudflare"
  "cfWorkerUrl":   "",               // required if emailProvider="cloudflare"
  "cfWorkerSecret":"",               // required if emailProvider="cloudflare"
}
```

**Response event:** `register-done { ok, email, username, password, status }`

---

### `warmup`
Logs in and pre-positions the browser at a chosen idle stage.

```jsonc
{
  "type": "warmup",

  "username": "user@example.com",   // omit to reuse stored credentials
  "password": "secret",             // omit to reuse stored credentials

  // ── Idle position ─────────────────────────────────────────────────────────
  "idleStep": "login",   // "login"    → idle at dashboard after login
                         // "form"     → questionnaire filled, form tabs filled, not submitted
                         // "schedule" → form submitted, idle at schedule page

  // ── Visa form data (required for idleStep "form" or "schedule") ───────────
  "arrivalDate": "2026-07-01",   // desired arrival date
  "consulPost":  "5088",         // consular post ID (default: "5088" = Lisbon)
  "realPerson":  { ... },        // same schema as register.realPerson

  // ── Captcha solver ────────────────────────────────────────────────────────
  "captchaSolver":   "capsolver",
  "captchaParallel": false
}
```

**Response event:** `warmup-ready { ok, idleStep }`

---

### `apply`
Resumes from the current idle position and completes the booking to PDF download.  
Reads `idleStep` from where the last `warmup` left off.

```json
{ "type": "apply" }
```

| Idle position | Resume path |
|---|---|
| `"login"` | questionnaire → form → submit → schedule → book → PDF |
| `"form"` | submit form → schedule → book → PDF |
| `"schedule"` | schedule → book → PDF |

**Response event:** `apply-done { ok, status, pdfFilename? }`

---

### `all-in-one`
Debug/test command: full pipeline in one shot — register → login → apply.

```jsonc
{
  "type": "all-in-one",

  "realPerson":      { ... },        // same schema as register.realPerson (null = fake)
  "captchaSolver":   "capsolver",
  "captchaParallel": false,
  "emailProvider":   "mailtm",
  "cfDomain": "", "cfWorkerUrl": "", "cfWorkerSecret": "",
  "arrivalDate":     "2026-07-01",
  "consulPost":      "5088"
}
```

**Response event:** `all-in-one-done { ok, status, pdfFilename? }`

---

### Maintenance Commands

| Command | Purpose | Response |
|---|---|---|
| `ping` | Keepalive | `pong` |
| `status` | Query active workflow/step | `status` |
| `abort` | Cancel everything, reset all state | `status` (all nulls) |
| `check-proxy` | Trigger proxy quality snapshot | *(no reply)* |
| `check-solver-balances` | Query all captcha solver balances | `solver-balances` |

---

## Events: Extension → Manager

### `hello`
Sent on every WS connect.

```json
{ "type": "hello", "version": "3.0.0" }
```

### `pong`
Reply to `ping`.

### `status`
Reply to `status` command, or sent after `abort`.

```jsonc
{
  "type":     "status",
  "workflow": "register",   // active workflow type, or null
  "step":     "auth",       // active step, or null
  "errorNo":  0             // last error code (0 = no error)
}
```

### `register-done`
```jsonc
{
  "type":     "register-done",
  "ok":       true,
  "email":    "abc123@example.com",
  "username": "abc123@example.com",
  "password": "Xk3!mN9pQr",
  "status":   "verified"
}
```

### `warmup-ready`
```jsonc
{ "type": "warmup-ready", "ok": true, "idleStep": "login" }
```

### `apply-done`
```jsonc
{ "type": "apply-done", "ok": true, "status": "booked", "pdfFilename": "visa_2026-07-01.pdf" }
```

### `all-in-one-done`
Same shape as `apply-done`.

### `solver-balance`
Sent during a captcha solve when the primary solver balance is checked.

```jsonc
{ "type": "solver-balance", "solver": "capsolver", "balance": 4.87, "ts": "2026-05-27T10:00:00.000Z" }
```

### `solver-balances`
Reply to `check-solver-balances`.

```jsonc
{ "type": "solver-balances", "balances": { "capsolver": 4.87, "anti-captcha": 1.20 }, "ts": "..." }
```

### `error`
Workflow stopped on a hard error. Send `abort` before starting a new workflow.

```jsonc
{ "type": "error", "reason": "F_warmup: expected questionnaire, got auth" }
```

---

## Typical Full Session

```
Manager                              Extension
───────                              ─────────
                                     ← hello { version: "3.0.0" }

→ register { emailProvider:"mailtm" }

                                     (~3 min — register + email verification)
                                     ← register-done { ok, email, username, password }

→ warmup { username, password, idleStep:"login" }

                                     (~30 s — login)
                                     ← warmup-ready { ok, idleStep:"login" }

  [wait for slot signal]

→ apply {}

                                     (~5 min — questionnaire → form → schedule → PDF)
                                     ← apply-done { ok, pdfFilename }
```

---

## Node.js Minimal Server Example

```js
const { WebSocketServer } = require('ws');
const wss = new WebSocketServer({ port: 9000 });

wss.on('connection', ws => {
  let registeredCreds = null;

  ws.on('message', raw => {
    const msg = JSON.parse(raw);
    console.log('←', msg);

    if (msg.type === 'hello') {
      // Extension connected — kick off registration
      ws.send(JSON.stringify({ type: 'register' }));
    }

    if (msg.type === 'register-done' && msg.ok) {
      registeredCreds = { username: msg.username, password: msg.password };
      ws.send(JSON.stringify({
        type: 'warmup',
        username: msg.username,
        password: msg.password,
        idleStep: 'login',
      }));
    }

    if (msg.type === 'warmup-ready' && msg.ok) {
      // Extension is idle — fire apply when slot opens
      // (in practice you'd wait for a slot-open signal here)
      ws.send(JSON.stringify({ type: 'apply' }));
    }

    if (msg.type === 'apply-done') {
      console.log('Done:', msg);
    }

    if (msg.type === 'error') {
      console.error('Error:', msg.reason);
      ws.send(JSON.stringify({ type: 'abort' }));
    }
  });
});

console.log('Manager WS server listening on ws://localhost:9000');
```

---

## Notes

- `extension/config.json` is gitignored. Copy `extension/config.json.example` and fill in your values before loading the extension.
- The extension sends a `ping` keepalive every ~25 s. Manager may ignore it or reply with `pong`.
- All dates use `YYYY-MM-DD` format in the manager protocol; the extension converts internally as needed.
- One Octo Browser profile = one extension instance = one WS connection. Run one manager server; each profile connects as a separate WS client.
