// Shared constants — loaded in both service worker (importScripts) and
// content scripts (manifest content_scripts). Add only values that are
// genuinely needed in more than one context.

// ---------------------------------------------------------------------------
// Storage keys — single source of truth for all chrome.storage.local keys.
// Use SK.X everywhere instead of raw strings to catch typos at read time.
// ---------------------------------------------------------------------------

const SK = Object.freeze({
  // Credentials
  USERNAME:             "username",
  PASSWORD:             "password",

  // Workflow state
  WORKFLOW_TYPE:        "workflow-type",
  WORKFLOW_STEP:        "workflow-step",
  LAST_ERROR_NO:        "last-error-no",
  WARMUP_IDLE_STATE:    "warmup-idle-state",
  WARMUP_TAB_ID:        "warmup-tab-id",
  ACTIVE_TAB_ID:        "active-tab-id",

  // Registration
  REGISTER_PERSON:      "register-person",
  REGISTER_EMAIL:       "register-email",
  REGISTER_RETRIED:     "register-retried",
  REGISTER_POST_SUBMITTED: "register-post-submitted",
  REGISTER_TOKEN_RETRY: "register-token-retry",
  CHALLENGE_COUNT:      "challenge-count",
  PENDING_ACCOUNT:      "pending-account",

  // Email
  EMAIL_PROVIDER:       "email-provider",
  EMAIL_POLL:           "emailPoll",
  EMAIL_TOKEN:          "email-token",
  EMAIL_CODE_TOKEN:     "email-code-token",

  // Cloudflare email config
  CF_MAIL_DOMAIN:       "cf-mail-domain",
  CF_WORKER_URL:        "cf-worker-url",
  CF_WORKER_SECRET:     "cf-worker-secret",

  // Captcha
  CAPTCHA_SOLVER:          "captcha-solver",
  CAPTCHA_SOLVER_PARALLEL: "captcha-solver-parallel", // bool — race all solvers in parallel
  ANTICAPTCHA_KEYS:        "anticaptcha-keys",
  TWOCAPTCHA_KEYS:         "twocaptcha-keys",
  CAPMONSTER_KEYS:         "capmonster-keys",
  CAPSOLVER_KEY:           "capsolver-key",

  // Session proxy — written by the manager workflow command, read by solver functions.
  // When present, solvers use proxy-based task types so the reCAPTCHA token is scored
  // on the same IP the browser is using, matching the Enterprise API's IP-consistency check.
  PROXY_TYPE:  "proxy-type",   // "socks5" | "http"
  PROXY_HOST:  "proxy-host",
  PROXY_PORT:  "proxy-port",   // number
  PROXY_LOGIN: "proxy-login",
  PROXY_PASS:  "proxy-pass",

  // Visa application
  REAL_PERSON_INPUT:    "real-person-input",
  VISA_CONSULAR_POST:   "visa-consular-post",
  VISA_ARRIVAL_DATE:    "visa-arrival-date",
  VISA_DEPARTURE_DATE:  "visa-departure-date",
  TRIGGER_MODE:         "trigger-mode",

  // Persistent data
  ACCOUNTS:             "accounts",
  BURNED_PROXIES:       "burned-proxies",
  INSTALLED_AT:         "installedAt",

  // Feature flags
  LANG_SWITCH_ENABLED:  "lang-switch-enabled",
  PRE_VISIT_WARMUP:     "pre-visit-warmup",    // bool — browse warm-up sites before applying

  // Warmup navigation
  WARMUP_END_TIME:      "warmup-end-time",
  WARMUP_SITE_INDEX:    "warmup-site-index",

  // Pending states
  LOGIN_PENDING:        "login-pending",

  // Live run status (written by background, read by popup for live display)
  RUN_STATUS:           "run-status",
  RUN_ERROR:            "run-error",

  // Post monitor
  TARGET_POST_ID:       "target-post-id",
  DIRECT_SLOTS_FETCH:   "direct-slots-fetch",
});

// ---------------------------------------------------------------------------
// Storage helpers — shared by service worker and content scripts.
// Use SK.X keys so typos are caught at reference time.
//
//   skGet(SK.FOO)           → { "foo": value }
//   skGet(SK.FOO, SK.BAR)   → { "foo": v1, "bar": v2 }
//   skSet({ [SK.FOO]: val }) → writes "foo" = val
//   skRemove(SK.FOO, ...)   → removes the key(s)
// ---------------------------------------------------------------------------
const skGet    = (...keys) => chrome.storage.local.get(keys.length === 1 ? keys[0] : keys);
const skSet    = (obj)     => chrome.storage.local.set(obj);
const skRemove = (...keys) => chrome.storage.local.remove(keys.length === 1 ? keys[0] : keys);

// ---------------------------------------------------------------------------
// Target site
// ---------------------------------------------------------------------------

const TARGET_URL  = "https://pedidodevistos.mne.gov.pt/VistosOnline/";
const TARGET_HOST = "pedidodevistos.mne.gov.pt";

// ---------------------------------------------------------------------------
// External services
// ---------------------------------------------------------------------------

const MAILTM = "https://api.mail.tm";

// ---------------------------------------------------------------------------
// Workflow steps
//
// Transition model: every step change is   last_step = cur_step, cur_step = next
// NONE / WARMUP are pre-page states (no target tab open).
// All other steps map to a URL bucket (see STEP_BUCKETS).
// ---------------------------------------------------------------------------

const WF_STEPS = {
  NONE:                    -1,  // No target tab open
  WARMUP:                   0,  // warmup command received, browsing warm-up sites

  // ── Home / index ──────────────────────────────────────────────────────────
  HOME_READY:               1,  // Target site loaded, unauthenticated
  LOGIN_SUCCESS:           15,  // Index page loaded after successful login

  // ── Auth page  (/Authentication.jsp, no ?token) ───────────────────────────
  AUTH_READY:               2,  // Login form visible
  REG_FORM_READY:           3,  // Registration form visible (cmd: register)
  REG_FORM_SUBMITTED:       4,  // Reg form submitted — attempt 1, awaiting email
  REG_FORM_FAILED:          5,  // Reg form submission failed — attempt 1
  REG_FORM_SUBMITTED2:      6,  // Reg form submitted — retry
  REG_FORM_FAILED2:         7,  // Reg form submission failed — retry
  LOGIN_SUBMITTED:         11,  // Login form submitted — attempt 1
  LOGIN_FAILED:            12,  // Login failed — attempt 1
  LOGIN_SUBMITTED2:        13,  // Login form submitted — retry
  LOGIN_FAILED2:           14,  // Login failed — retry

  // ── Auth page  (/Authentication.jsp?token=…) ─────────────────────────────
  REG_VERIFY_READY:        8,  // Email token form visible
  REG_VERIFY_FAILED:       9,  // Token verification failed / rejected / timeout
  REG_VERIFY_SUCCESS:      10,  // Token accepted — account created

  // ── Questionnaire  (/Questionario) ────────────────────────────────────────
  QUERY_READY:             16,  // Questionnaire page ready
  QUERY_SUBMITTED:         17,  // Questionnaire submitted
  QUERY_FAILED:            18,  // Query selection failed / timeout / submit invisible

  // ── Request form  (/Formulario) ───────────────────────────────────────────
  REQ_FORM_READY:          19,  // Request form loaded and ready
  REQ_FORM_FILLED:         20,  // All fields filled except consular post
  REQ_FORM_SIGNAL:         21,  // Consular post available (or awaiting external signal)
  REQ_FORM_SUBMITTED:      22,  // Form submitted
  REQ_FORM_FAILED:         23,  // Form failed

  // ── Schedule page  (/Schedule… | /Agendamento…) ───────────────────────────
  SCHEDULE_RECAPTCHA_READY:  24,  // reCAPTCHA challenge detected
  SCHEDULE_RECAPTCHA_SOLVED: 25,  // Token injected, calendar/slot picker visible
  SCHEDULE_RECAPTCHA_FAILED: 26,  // reCAPTCHA solve failed / timeout
  SCHEDULE_SLOT_EMPTY:       27,  // No available slots
  SCHEDULE_SLOT_PICKED:      28,  // Calendar date + slot selected
  SCHEDULE_SUBMITTED:        29,  // Submit clicked
  SCHEDULE_FAILED:         30,  // Server rejected / timeout

  // ── Confirmation ──────────────────────────────────────────────────────────
  PDF_READY:               31,  // Confirmation PDF page visible
  PDF_DOWNLOAD:            32,  // PDF download triggered
  COMPLETED:               33,  // PDF confirmed downloaded — workflow done
};

// ---------------------------------------------------------------------------
// Step machine — shared helpers for deterministic step resolution
//
// Resolution priority (same logic in ut_currentWfStep and detectCurrentStep):
//   1. cur_page  (URL)     → classifyUrlPath → bucket → defines valid step set
//   2. last_step (storage) → trusted if it falls within the current bucket
//   3. bucket default      → coarse fallback when no stored step matches
//
// NOTE: DOM-level page_state is NOT used here. Steps are set explicitly by
// workflow code via chrome.storage ("workflow-step"). The stored step is the
// authoritative source; the bucket is the sanity filter.
// ---------------------------------------------------------------------------

// Maps URL pathname+search → bucket string.
// Called with (location.pathname, location.search) in content
// and with (url.pathname, url.search) in background.
function classifyUrlPath(pathname, search) {
  if (/Authentication/i.test(pathname))
    return search.includes("token=") ? "auth_token" : "auth";
  if (/Questionario/i.test(pathname))         return "query";
  if (/Formulario/i.test(pathname))           return "form";
  if (/Schedule|Agendamento/i.test(pathname)) return "schedule";
  return "home";
}

// WF_STEPS values that are valid while on each URL bucket.
// A stored last_step NOT in this set means the page changed since the step was
// written (crash, navigation) — discard it and fall back to the bucket default.
const STEP_BUCKETS = {
  home: new Set([
    WF_STEPS.HOME_READY,
    WF_STEPS.LOGIN_SUCCESS,
  ]),
  auth: new Set([
    WF_STEPS.AUTH_READY,
    WF_STEPS.REG_FORM_READY,
    WF_STEPS.REG_FORM_SUBMITTED,  WF_STEPS.REG_FORM_FAILED,
    WF_STEPS.REG_FORM_SUBMITTED2, WF_STEPS.REG_FORM_FAILED2,
    WF_STEPS.LOGIN_SUBMITTED,     WF_STEPS.LOGIN_FAILED,
    WF_STEPS.LOGIN_SUBMITTED2,    WF_STEPS.LOGIN_FAILED2,
  ]),
  auth_token: new Set([
    WF_STEPS.REG_VERIFY_READY,
    WF_STEPS.REG_VERIFY_FAILED,
    WF_STEPS.REG_VERIFY_SUCCESS,
  ]),
  query: new Set([
    WF_STEPS.QUERY_READY,
    WF_STEPS.QUERY_SUBMITTED,
    WF_STEPS.QUERY_FAILED,
  ]),
  form: new Set([
    WF_STEPS.REQ_FORM_READY,
    WF_STEPS.REQ_FORM_FILLED,
    WF_STEPS.REQ_FORM_SIGNAL,
    WF_STEPS.REQ_FORM_SUBMITTED,
  ]),
  schedule: new Set([
    WF_STEPS.SCHEDULE_RECAPTCHA_READY,
    WF_STEPS.SCHEDULE_RECAPTCHA_SOLVED,
    WF_STEPS.SCHEDULE_RECAPTCHA_FAILED,
    WF_STEPS.SCHEDULE_SLOT_EMPTY,
    WF_STEPS.SCHEDULE_SLOT_PICKED,
    WF_STEPS.SCHEDULE_SUBMITTED,
    WF_STEPS.SCHEDULE_FAILED,
    WF_STEPS.PDF_READY,
    WF_STEPS.PDF_DOWNLOAD,
    WF_STEPS.COMPLETED,
  ]),
};

// Coarse step to return when no stored step is valid for the current bucket.
const BUCKET_DEFAULT_STEP = {
  home:       WF_STEPS.HOME_READY,
  auth:       WF_STEPS.AUTH_READY,
  auth_token: WF_STEPS.REG_VERIFY_READY,
  query:      WF_STEPS.QUERY_READY,
  form:       WF_STEPS.REQ_FORM_READY,
  schedule:   WF_STEPS.SCHEDULE_RECAPTCHA_READY,
};

// Pure step resolver — shared by ut_currentWfStep (background) and detectCurrentStep (content).
// Each context classifies the current URL and reads last_step from storage, then calls this.
//   bucket   — from classifyUrlPath()
//   lastStep — from storage["workflow-step"], or null if not set
function resolveStep(bucket, lastStep) {
  if (lastStep !== null && STEP_BUCKETS[bucket]?.has(lastStep)) return lastStep;
  return BUCKET_DEFAULT_STEP[bucket] ?? WF_STEPS.NONE;
}

const TIMER_MS = Object.freeze({
  // Warmup browsing (F0)
  warmup: Object.freeze({
    CHECK_INTERVAL:   400,    // tab.status poll cadence
    READY_TIMEOUT:  12_000,   // max wait for each warmup page load
    DWELL_MIN:       7_000,   // minimum dwell on each site
    DWELL_MARGIN:    4_000,   // random extra dwell (uniform 0–margin)
  }),

  // waitForPageReady() timeouts — keyed by the operation they guard
  pageReady: Object.freeze({
    DEFAULT:        30_000,   // general navigation / form submit
    LANG_SWITCH:    15_000,   // language redirect after cmd-switch-lang
    AUTH_CLICK:     20_000,   // auth page after login-link click
    LOGIN_SUBMIT:   15_000,   // login submit → response or tab navigation
    SSO:           120_000,   // SSO / WAF / reCAPTCHA heavy redirect
    REG_FORM:       90_000,   // registration form page load
    TOKEN_PAGE:    600_000,   // token page after reg form submit
    QUESTIONNAIRE: 150_000,   // full questionnaire fill + submit (F4)
    WARMUP_Q:       60_000,   // warmup questionnaire fill (shorter path)
    FORM_FILL:     150_000,   // visa form fill → schedule page (F4)
    WARMUP_FORM:    90_000,   // warmup form fill → schedule (F_warmup idleStep)
  }),

  // Email polling
  email: Object.freeze({
    POLL_INTERVAL:   6_000,   // setInterval cadence for _startEmailPoll
    CODE_TIMEOUT:  120_000,   // max wait for email verification code
    POLL_RETRY:      2_000,   // sleep between _waitForEmailCodeToken checks
  }),

  // Captcha solving
  captcha: Object.freeze({
    POLL_INTERVAL:   4_000,   // getTaskResult poll cadence
    MIN_SOLVE:      12_000,   // minimum enforced solve time (anti-bot signal)
    CLICK_MIN:       1_500,   // min delay before reCAPTCHA click (anti-fingerprint)
    CLICK_MARGIN:    1_500,   // random extra (uniform 0–margin)
    // NOTE: CLICK_MIN/CLICK_MARGIN cannot be referenced inside _recaptchaInjectFunc
    // (runs in MAIN world via executeScript); values are mirrored inline there.
  }),

  // Session keep-alive (F6)
  keepAlive: Object.freeze({
    PING_MIN:       60_000,   // minimum interval between keep-alive ticks
    PING_MARGIN:   120_000,   // random extra (uniform 0–margin)
  }),

  // Auth / registration retries and settle waits
  auth: Object.freeze({
    RETRY_BASE:      1_500,   // base back-off in F1 retry loop
    RETRY_STEP:        500,   // per-attempt increment in F1 retry loop
    LOGIN_RETRY:     4_000,   // sleep between F3 login attempts
    SETTLE_MIN:      3_000,   // min settle wait before login after registration
    SETTLE_MARGIN:   3_000,   // random extra (uniform 0–margin)
    REJECTION_BASE: 30_000,   // first login-rejection wait in all_in_one
    REJECTION_STEP: 30_000,   // additional wait per subsequent rejection
  }),

  // Proxy / burn list
  proxy: Object.freeze({
    BURN_TTL: 86_400_000,     // 24 h — how long a burned proxy stays blacklisted
  }),

  // Miscellaneous
  misc: Object.freeze({
    DOWNLOAD_CAP:   5_000,    // safety cap on chrome.downloads.onChanged wait
  }),
});

// ---------------------------------------------------------------------------
// Error taxonomy — proxyStatus and nextAction values attached to thrown errors.
// The manager reads these to decide how to recover (rotate proxy, change device,
// re-queue, etc.).  Use these constants everywhere instead of raw strings so
// that adding a new action or renaming one is a single-file change.
// ---------------------------------------------------------------------------

const ERR_STATUS = Object.freeze({
  OK:                 "ok",                   // workflow completed successfully
  UNKNOWN:            "unknown",              // unexpected state — catch-all
  PROXY_SLOW:         "proxy_slow",           // page-ready / form timeout
  BLOCKED:            "blocked",              // WAF or IP rate-limit block
  BURNED:             "burned",               // captcha failure — IP blacklisted by server
  LOGIN_REJECTED:     "login_rejected",       // server rejected login credentials
  EMAIL_PROVIDER:     "email_provider_error", // mail.tm / Cloudflare email API failure
  EMAIL_TAKEN:        "email_taken",          // registration email already in use
  USERNAME_COLLISION: "username_collision",   // generated username already registered
});

const NEXT_ACTION = Object.freeze({
  ROTATE_PROXY:  "rotate_proxy",   // swap proxy and retry the same operation
  CHANGE_DEVICE: "change_device",  // fingerprint flagged — need a fresh Octo profile
});

// ---------------------------------------------------------------------------
// Message type constants — single source of truth for chrome.runtime.sendMessage
// and chrome.tabs.sendMessage type strings.  Use MSG.X everywhere so renaming
// a message type is a single-file change and typos are caught at reference time.
//
// Direction notation:
//   Popup → BG       triggered by user clicking a popup button
//   Content ↑ BG     content script reports state / requests BG action
//   BG ↓ Content     background commands the content script
//   BG ↔ any         utility messages used in multiple directions
// ---------------------------------------------------------------------------
const MSG = Object.freeze({
  // ── Popup → Background (workflow triggers) ─────────────────────────────────
  LOGIN_IDLE:                  "login-idle",
  LOGIN_APPLY:                 "login-apply",
  REGISTER_ONLY:               "register-only",
  REGISTER_LOGIN:              "register-login",
  REGISTER_APPLY:              "register-login-apply",

  // ── Content ↑ Background (state signals) ──────────────────────────────────
  PAGE_READY:                  "page-ready",
  TARGET_POST_AVAILABLE:       "target-post-available",

  // ── Background ↓ Content (MAIN-world scripting bridge) ────────────────────
  INJECT_ALERT_CAPTURE:        "inject-alert-capture",
  RESET_RECAPTCHA:             "reset-recaptcha",
  SOLVE_RECAPTCHA_API:         "solve-recaptcha-api",
  SOLVE_AND_INJECT_RECAPTCHA:  "solve-and-inject-recaptcha",
  INJECT_RECAPTCHA_TOKEN:      "inject-recaptcha-token",
  GET_RECAPTCHA_SITEKEY:       "get-recaptcha-sitekey",
  EXEC_PAGE_SCRIPT:            "exec-page-script",

  // ── Background ↔ any (utility / plumbing) ─────────────────────────────────
  GET_SESSION_KEYS:            "get-session-keys",
  PING:                        "ping",
  SW_KEEPALIVE_PING:           "sw-keepalive-ping",
  SLEEP:                       "sleep",
  LOG_ENTRY:                   "log-entry",
  START_EMAIL_POLL:            "start-email-poll",
  STOP_EMAIL_POLL:             "stop-email-poll",
  DISPATCH_ABORT:              "dispatch-abort",
  SAVE_ACCOUNT:                "save-account",
  CLOSE_TAB:                   "close-tab",
  CLEAR_WORKFLOW:              "clear-workflow",
  EMAIL_TOKEN:                 "email-token",   // BG → content (email poll delivers code)
  GENERATE_PERSON:             "generate-person", // popup → BG: generate fake person data
});