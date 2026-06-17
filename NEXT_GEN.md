# Next Generation — Implementation Roadmap

## Overview

Current state: engine layer works end-to-end. Workers login, warm up, poll, signal, and apply. The next generation adds reliability, observability, remote control, and scalability on top of the proven engine core — without touching the frozen auto_api/ layer.

---

## Phase 1 — Fix Immediate Blockers

### 1.1 ISP Proxy Integration
**Goal:** Real workers use sticky ISP proxies (no rotation after login), SOAX as fallback.

**Why:** SOAX session IDs expire; ISP static IPs give stable long-lived sessions. Reduces session death rate during `awaiting_signal` phase.

**Plan:** (already designed — see plan file)
- `engine/isp_proxy_pool.py` — `IspProxyPool` + `IspFirstRequester`
- Manager routes real workers to ISP-first requester, scouts stay on SOAX
- Per-worker sticky: same ISP proxy held after login success; rotate only on failure
- Fallback to SOAX when all 192 ISP proxies exhausted

**Blocker:** ISP proxies currently fail HTTPS tunneling. Needs working ISP proxy source.

---

### 1.2 Fix `is_alive` False Negative
**Goal:** `probe_session()` in `session_store.py` incorrectly returns "dead" after warmup.

**Root cause:** Post-warmup, `/VistosOnline/` body no longer contains "Questionario" or "logout" → false "dead" → tight restore loop.

**Option A (engine-layer workaround):** Override the probe in worker keepalive — check `/Schedule.jsp` directly instead of calling `probe_session()`. No change to frozen files.

**Option B (unfreeze session_store.py):** Add a third alive signal to `probe_session()`: check for presence of `Schedule.jsp` link or any portal-authenticated content marker.

---

### 1.3 Solver Balance Monitoring
**Goal:** Detect zero-balance solvers before they pollute every login attempt.

**Implementation:** On startup, manager queries each solver's balance API. Disables solvers with zero balance and logs a warning. Re-checks every 30 min.

---

## Phase 2 — Remote Control & Observability

### 2.1 Telegram Bot
**Goal:** Monitor and control the run from anywhere via Telegram.

**Commands:**
- `/status` — live worker state table (warmed/polling/waiting/failed counts)
- `/signal` — fire slot signal manually (same as `POST /signal`)
- `/signal 2026-07-15 1` — inject specific date + period then fire
- `/stop` — graceful stop all workers
- `/log` — tail last 20 log lines

**Implementation:**
- `engine/telegram_bot.py` — python-telegram-bot (async)
- Bot token + chat ID in `.env`
- Manager passes `signal_bus` reference to bot; bot calls `signal_bus.fire_synthetic()`
- Worker `status_cb` events forwarded to bot's broadcast queue

---

### 2.2 FastAPI Dashboard
**Goal:** Live web dashboard showing worker states, slot pool, signal history.

**Endpoints:**
```
GET  /workers          — list of all workers + current state
GET  /status           — slot pool stats + signal rounds
POST /signal           — fire signal (replaces aiohttp server in signals.py)
POST /signal/reset     — manual reset
GET  /logs             — last N log lines
WS   /ws/events        — WebSocket stream of state-change events
```

**Implementation:**
- `engine/api.py` — FastAPI app
- Manager's `status_cb` emits to a broadcast queue consumed by WebSocket handler
- `Manager.run()` starts as background asyncio task within FastAPI lifespan
- Frontend: simple HTML/JS with EventSource or WebSocket table

---

### 2.3 Structured JSON Logging
**Goal:** Replace plaintext log lines with JSONL for easy parsing and replay.

**Format:**
```json
{"ts": 1718200000.1, "worker": "josnog5197", "role": "real", "state": "warmed", "proxy": "soax:abc123", "event": "state_change"}
{"ts": 1718200060.2, "worker": "feralv6017", "role": "scout", "event": "poll", "result": "no_slot"}
{"ts": 1718200120.3, "event": "signal_fired", "round": 1, "pool": 3, "real_warmed": 18}
```

**File:** `data/logs/run_{timestamp}.jsonl` (separate from human-readable console log)

---

## Phase 3 — Database Backend

### 3.1 SQLite/PostgreSQL Schema
**Goal:** Replace CSV files and in-memory state with a persistent database.

**Tables:**
```sql
accounts       (username, password, email, status, last_login, checkpoint, notes)
runs           (id, started_at, ended_at, mode, posto_id, workers_total, workers_warmed, pdfs_downloaded)
bookings       (id, run_id, username, posto_id, date, period_id, pdf_path, booked_at)
proxy_usage    (proxy_url, worker, used_at, outcome)
solver_events  (solver, action, success, score, latency_ms, ts)
```

**Benefits:**
- Account rotation history (never reuse burned accounts)
- Run analytics (success rate, login time, solver performance)
- Booking deduplication (don't book same slot twice)

---

### 3.2 Account Health Tracking
**Goal:** Automatically flag accounts that consistently fail and skip them in future runs.

**Logic:**
- 3+ consecutive login failures → `status = 'soft_blocked'` → skip for 24h
- Login success after soft-block → restore to `status = 'active'`
- Portal returns specific error for account → `status = 'hard_blocked'` → never retry

---

## Phase 4 — Scalability & Reliability

### 4.1 Multi-Posto Support
**Goal:** Run workers simultaneously against multiple postos (Dublin 5084, Paris 3059, etc.).

**Implementation:**
- `--posto` accepts comma-separated list: `--posto 5084,3059,2891`
- Manager creates separate worker pools per posto
- Separate slot pools and signal buses per posto
- Scouts assigned to specific posto (or round-robin across all)

---

### 4.2 Dynamic Worker Scaling
**Goal:** Add more warmed workers during a run without restarting.

**API:**
```
POST /workers/add  {"count": 10, "role": "real"}
POST /workers/add  {"count": 2, "role": "scout"}
```

Manager spawns new Worker coroutines into the running event loop and adds them to lifecycle monitor.

---

### 4.3 Session Pre-warming Pool
**Goal:** Keep a pool of pre-warmed sessions ready at all times, not just during active runs.

**Flow:**
1. Background process continuously warms fresh accounts to Schedule.jsp checkpoint
2. When a run starts, it draws from the pre-warmed pool instead of warming from scratch
3. Workers reach `awaiting_signal` in seconds instead of minutes
4. Pool auto-refills as sessions expire

---

### 4.4 Proxy Health Scoring
**Goal:** Track per-proxy success rates and prefer high-quality proxies.

**Logic:**
- Each proxy gets a score (0-100) based on login success rate, session longevity, WAF pass rate
- `proxy_pool.advance()` weighted by score instead of pure round-robin
- Proxies below threshold score are quarantined for 1h before retry

---

## Phase 5 — Booking Reliability

### 5.1 Apply Retry with Re-warm
**Goal:** If `apply_book()` fails due to form state expiry, re-run steps 2-6 and retry.

**Current behavior:** Form state expires after ~5-7 min of the session being idle. `apply_book()` returns error.

**Fix:** On form state expiry error in `apply_book()`, worker calls `_run_steps_2_to_6()` (re-warm steps, no CAPTCHA, ~5-10s) then retries `apply_book()` immediately.

---

### 5.2 Parallel Slot Acquisition
**Goal:** When signal fires, all warmed workers race to acquire slots in parallel.

**Current behavior:** Workers independently request from `SlotManager.request_slot()` — already atomic. No change needed, just ensure `apply_concurrency` semaphore is high enough.

---

### 5.3 PDF Verification
**Goal:** Confirm downloaded PDF is valid before marking booking as done.

**Check:** PDF file size > 10KB AND contains applicant name string.

---

## Priority Order

| Priority | Item | Effort | Impact |
|----------|------|--------|--------|
| 🔴 Now | Fix aiohttp install in auto_api/.venv | 1 min | Unblocks manual signal |
| 🔴 Now | Top up 2captcha balance | External | Faster logins |
| 🟠 Soon | ISP proxy integration (1.1) | ~2h | Stable sessions |
| 🟠 Soon | is_alive false negative fix (1.2) | ~1h | Stops restore loops |
| 🟡 Next | Telegram bot (2.1) | ~4h | Remote control |
| 🟡 Next | Structured JSON logging (2.3) | ~1h | Better observability |
| 🟢 Later | FastAPI dashboard (2.2) | ~1 day | Live monitoring |
| 🟢 Later | Database backend (3.x) | ~2 days | Full analytics |
| 🟢 Later | Session pre-warming pool (4.3) | ~1 day | Zero warmup delay |
| 🟢 Later | Multi-posto support (4.1) | ~4h | More booking chances |
