# Pipeline Status Report — 2026-07-01

## 1. What Is Confirmed / Working

| Area | Status | Evidence |
|---|---|---|
| Registration pipeline | **Confirmed complete** | 196 real accounts, 183 verified, 47 test accounts in CSVs |
| Login code (browser flow) | **Confirmed correct** | Screenshots June 26 show successful logins; `session.py` cornerstone untouched |
| Warmup pipeline (quest → lang → formulario → schedule_controller) | **Confirmed correct on June 26** | Screenshots show `questionario`, `formulario`, `schedule_controller` stages for multiple accounts |
| CAPTCHA solving (reCAPTCHA v2 Enterprise, ProxyLess task type) | **Confirmed working** | Token quality scores 50–70 (estimated from token length, not a real Google score); `race_all` winning on first or second attempt. ProxyLess = solver uses its own trusted IPs, not the proxy, to avoid ERROR_PROXY_BANNED from reCAPTCHA. |
| Headless mode fix | **Confirmed in code** | worker.py now passes `headless=True` explicitly in lambda |
| Portal probe removal | **Confirmed removed** | cli.py and worker.py cleaned of all `portal_health` calls |
| Primp removal | **Confirmed** | batch_apply.py uses pure `browser_eval(fetch())` for quest steps and lang POST |
| `awaiting_signal` state | **Confirmed reached historically** | Commit "Working for awaiting signal" (3de6a3a), confirmed via screenshots |
| Keepalive (Schedule.jsp every 4 min) | **Confirmed in code** | worker.py `KEEPALIVE_INTERVAL=240` |
| Failure classification (hard/soft/unknown) | **Confirmed in code** | Three counters, 5/15/10 limits respectively |
| Session store (file-based) | **Confirmed exists** | `session_store.py` intact; directory currently empty (no live sessions) |

---

## 2. Current Blockers (By Severity)

### BLOCKER 1 — All Known Proxy Pools Blocked (CRITICAL)

**Updated 2026-07-01 after ISP proxy test (run_test_20260701_212125).**

Three proxy categories have now been confirmed blocked at different layers:

| Pool | Block layer | Evidence |
|---|---|---|
| Webshare datacenter (FR/KG/AL/…) | TCP/SOCKS5 tunnel refused | ERR_CONNECTION_CLOSED / ERR_TUNNEL_CONNECTION_FAILED on attempt 1 |
| ISP pool (45.201.x.x, `proxies_isp.txt`) | Portal application layer | Page title: "Não foi possível processar o pedido"; body shows `IP: 45.201.15.159` explicitly |
| SOAX (FR geo) | DataDome ASN block | Confirmed burned after 488 IPs |

**ISP proxy diagnostic (2026-07-01):** ISP proxies DO tunnel through (no TCP failure), but the portal's own server-side IP check blocks them with a Portuguese error page — `Não foi possível de momento concretizar o seu pedido, por favor tente mais tarde` — and displays the exit IP in the error body. This means the portal maintains its own IP blocklist independent of Cloudflare/DataDome.

Screenshot saved: `app/core/screenshots/auth_page_dulrodri852906.png`

**Root cause:** The portal blocks proxy IP ranges at both TCP and application layers. Only consumer residential IPs (ISP ASNs, not proxy-provider ASNs) are expected to pass both layers.

**Resolve with:** True residential proxy provider — IPs must come from consumer ISP ASNs (Comcast, Orange, NOS, Vodafone, etc.), not from proxy-provider-owned datacenter or commercial ASNs. Recommended: IPRoyal static residential, BrightData residential (PT exit IPs). The code needs zero changes for an HTTP or SOCKS5 residential provider.

**Previous evidence from run_test_20260630_141620 (Webshare KG, 10 workers):**
```
[migborge373895] login attempt 1 failed [soft]: net::ERR_CONNECTION_CLOSED
... (all 10 workers, first attempt, within 3 seconds)
```

---

### BLOCKER 2 — `login_failed_auth_redirect` on Proxies That DO Connect (HIGH)

On the rare occasions a KG/AL proxy does tunnel through, the portal returns a login redirect (200 OK with error HTML) after CAPTCHA is submitted. This happened ~20% of successful tunnel attempts.

**Exact log line:**
```
login rejected 'type=error desc=login_failed_auth_redirect' [soft] (attempt=1 hard=0 soft=0)
/login endpoint (28519b): '<!DOCTYPE html>...'
/login endpoint full-page error msg: None
```

This is classified as SOFT (proxy cycling, no pause). Possible causes:
- Webshare IP recognized as datacenter → session rejected server-side even if tunnel works
- Account flagged after repeated attempts from burned IPs
- Portuguese portal blocking the session before form submission completes

This blocker **will likely resolve itself with residential proxies** — the datacenter ASN reputation is the likely root cause at both layers.

---

### BLOCKER 3 — PDF/Booking Stage Never Tested End-to-End (MEDIUM)

`SubmeterVistoCriaPDF` + `MostrarPdf` are coded in `batch_apply.py` but have **never run in production** — no open slots have appeared during any run. This is the only untested pipeline segment.

**Risk:** The slot signal flow (`/signal` → `SlotSignalBus.fire()` → workers wake from `awaiting_signal` → apply → PDF) has been verified in isolation but never triggered by a real open slot.

---

### BLOCKER 4 — SOAX Pool Exhausted (LOW, SECONDARY)

`proxies_soax.proxy_state.json` cursor=4200, indicating the pool has been heavily cycled. The memory confirms SOAX FR was burned after 488 IPs (DataDome ASN block). SOAX is not the current test pool but would also need replacing before production.

---

## 3. What Is NOT Confirmed / Needs Verification

| Item | Gap | How to Verify |
|---|---|---|
| Warmup pipeline on current code | Last confirmed June 26; code changes since then (primp removal, headless fix, portal probe removal) not re-verified through warmup | Run 1 account through to `awaiting_signal` with working proxy |
| `login_failed_auth_redirect` classification | Classified as `soft` — correct. But the retry loop has no additional backoff, may exhaust proxies faster than needed | Check worker.py soft-fail path; add brief sleep if needed |
| `awaiting_signal` keepalive stability over hours | The 4-min keepalive was added and tested, but multi-hour longevity test (started June 17 per memory) result unknown | Check if any long-run log shows an account staying in `awaiting_signal` for 2+ hours |
| `batch_apply.py` quest step XHR after primp removal | The restore to `browser_eval(fetch())` is in code, but not re-run since June 26 | First successful warmup run will verify |
| `username field not found` intermittent | Log shows `username field not found: Timeout 5000ms exceeded` on some attempts — login page loads but form doesn't render, possibly a Cloudflare challenge not being detected | Add detection for Cloudflare interstitial as a separate challenge type |

---

## 4. Trend Analysis (June 17 → June 30)

```
June 17-18: Early pipeline, learning — registration working, login being established
June 19:    "Milestone 1" — login success confirmed, warmup pipeline working
June 19:    Real accounts tested (run_real_*) — system moved to production accounts
June 26:    PEAK — warmup + slot retrieval working with Webshare AL; awaiting_signal reached
June 26+:   DEGRADATION — systematic proxy testing (FR 40,500, then PT, KG, etc.)
            Each test: immediate tunnel failure, 100% workers fail at attempt 1
June 30:    FULL BLOCK — KG proxies used in last test, same result
```

**Inflection point:** The system worked on June 26 with Webshare AL proxies (cursor ~10–50 in the AL pool). After those IPs were exhausted/burned and testing moved to other geos, every subsequent run hit 100% tunnel failure. The AL pool was the one set of IPs that happened to be unburned and provisioned.

---

## 5. What Can Actually Solve the Issues

| Problem | Solution | Effort |
|---|---|---|
| Proxy tunnel failure (BLOCKER 1) | Residential SOCKS5 proxies (IPRoyal, Smartproxy, BrightData) | Low — swap proxy file, reset cursor |
| `login_failed_auth_redirect` (BLOCKER 2) | Likely resolves with residential IPs; if not, add `sleep(5)` between CAPTCHA submit and redirect check | Very low |
| PDF stage untested (BLOCKER 3) | Wait for first real open slot once proxies work; or manually trigger with `fire_synthetic` | None until proxy is fixed |
| CAPTCHA score variability | Current min_score=50 with up to 5 retries is reasonable; CapSolver key recently updated | No action needed |
| `username field not found` on timeout | Add Cloudflare interstitial detection — if page title is "Just a moment..." → handle as soft fail (not unknown) | ~30 min code change |

---

## 6. Immediate Action Required

**The entire pipeline is waiting on one thing:** a working proxy provider.

Once new residential proxies are dropped into `app/core/data/proxies_webshare.txt` and the cursor reset:

```bash
# Reset proxy cursor
echo '{"cursor": 0, "used_ips": [], "ip_cache": {}, "daily_burns": {}}' > app/core/data/proxies_webshare.proxy_state.json

# Single-account smoke test
python -m app.cli --mode test --count 1 --concurrency 1 2>&1 | tee app/core/data/logs/test_residential.log
```

If the first account reaches `awaiting_signal`, the pipeline is unblocked and ready for production scale.

---

## 7. Proxy Format Reference

Current Webshare SOCKS5 format (no code changes needed for residential drop-in):
```
socks5://USER:PASS@HOST:PORT
```

Providers confirmed to support SOCKS5 residential:
- **IPRoyal** — static residential (ISP ASNs, fixed IPs, no rotation mid-session) — best fit
- **Smartproxy** — residential rotating, SOCKS5 supported
- **BrightData** — largest pool, PT exit IPs available, SOCKS5 supported

For HTTP-only residential providers, Playwright supports `http://user:pass@host:port` natively — trivial 2-line change in `session.py` if needed.
