// Service worker — credential storage, workflow dispatch, mail.tm email polling.

importScripts('communication.js');

// Load static config into storage so CF email settings are available without popup interaction.
(async () => {
  try {
    const cfg = await fetch(chrome.runtime.getURL("config.json")).then(r => r.json());
    const update = {};
    if (cfg.cfMailDomain)   update["cf-mail-domain"]   = cfg.cfMailDomain;
    if (cfg.cfWorkerUrl)    update["cf-worker-url"]    = cfg.cfWorkerUrl;
    if (cfg.cfWorkerSecret) update["cf-worker-secret"] = cfg.cfWorkerSecret;
    if (Object.keys(update).length) await chrome.storage.local.set(update);
  } catch (e) {
    console.warn("[OctoProbe] config.json CF load failed:", e);
  }
})();

const TARGET_URL  = "https://pedidodevistos.mne.gov.pt/VistosOnline/";
const TARGET_HOST = new URL(TARGET_URL).hostname;
const MAILTM    = "https://api.mail.tm";

// In-memory active tab ID; persisted to storage for SW restart survival.
let _activeTabId = null;
chrome.storage.local.get("active-tab-id").then(d => { if (d["active-tab-id"]) _activeTabId = d["active-tab-id"]; });

// ---------------------------------------------------------------------------
// Run logger — accumulates timestamped entries during a single command run
// and saves to a .log file via chrome.downloads when the run ends.
// One file per command: register_auto_YYYY-MM-DDTHH-MM-SS.log etc.
// ---------------------------------------------------------------------------

const _runLog = (() => {
  // Capture originals NOW, before _patchBgConsole wraps them — prevents infinite recursion
  // where _push → console.log → patched wrapper → _runLog.entry → _push → ...
  const _print = console.log.bind(console);
  let _lines = [];
  let _label = null;

  function _ts() { return new Date().toISOString(); }

  function start(label) {
    _label = label;
    _lines = [];
    _push(`=== ${label} started ===`);
  }

  function _push(msg) {
    const line = `${_ts()}  ${msg}`;
    _lines.push(line);
    _print(`[RunLog] ${msg}`); // always use pre-patch original
  }

  function entry(msg) {
    if (_label) _push(msg);
  }

  function finish(status) {
    if (!_label) return;
    _push(`=== finished: ${status} ===`);
    const ts = _ts().replace(/[:.]/g, "-").slice(0, 19);
    const filename = `${_label}_${ts}.log`;
    const url = "data:text/plain;charset=utf-8," + encodeURIComponent(_lines.join("\n") + "\n");
    chrome.downloads.download({ url, filename, saveAs: false }).catch(() => {});
    _lines = [];
    _label = null;
  }

  return { start, entry, finish };
})();
self._runLog = _runLog; // expose to communication.js (same SW scope, but const isn't on self)

// Forward SW console output to the run logger.
(function _patchBgConsole() {
  let _inFwd = false; // recursion guard (belt-and-suspenders; _runLog uses pre-patch console)
  const _fwd = (level, args) => {
    if (_inFwd) return;
    _inFwd = true;
    try {
      const text = args.map(a => (a instanceof Error ? a.stack : typeof a === "object" ? JSON.stringify(a) : String(a))).join(" ");
      _runLog.entry(`[${level}] ${text}`);
    } catch (_) {}
    _inFwd = false;
  };
  for (const level of ["log", "warn", "error", "info"]) {
    const orig = console[level].bind(console);
    console[level] = function (...args) { orig(...args); _fwd(level.toUpperCase(), args); };
  }
})();

// ---------------------------------------------------------------------------
// Fake person generator (JS port of data/person.py)
// ---------------------------------------------------------------------------

const _FIRST = ["Ana","Maria","João","Carlos","Pedro","Sofia","Luísa","Miguel","Rita","Filipe",
                 "Sara","Rui","Inês","Tiago","Beatriz","André","Catarina","Nuno","Mariana","Diogo"];
const _LAST  = ["Silva","Santos","Oliveira","Costa","Ferreira","Pereira","Rodrigues","Alves",
                 "Martins","Carvalho","Gomes","Lopes","Sousa","Marques","Mendes","Correia"];
const _NAT   = ["CPV"];

function _pick(arr) { return arr[Math.floor(Math.random() * arr.length)]; }

function _rand(n) { return Math.floor(Math.random() * n); }

function _genPassword() {
  const specials = "!@#$%&*_+";
  const digits   = "0123456789";
  const uppers   = "ABCDEFGHIJKLMNOPQRSTUVWXYZ";
  const lowers   = "abcdefghijklmnopqrstuvwxyz";
  const all      = specials + digits + uppers + lowers;
  let chars = [
    specials[_rand(specials.length)],
    digits[_rand(digits.length)],
    uppers[_rand(uppers.length)],
  ];
  for (let i = 0; i < 13; i++) chars.push(all[_rand(all.length)]);
  for (let i = chars.length - 1; i > 0; i--) {
    const j = _rand(i + 1);
    [chars[i], chars[j]] = [chars[j], chars[i]];
  }
  return chars.join("");
}

function _genBirthdate() {
  const year  = 1964 + _rand(41);   // age 20-60 from 2024
  const month = 1  + _rand(12);
  const day   = 1  + _rand(28);
  return `${year}/${String(month).padStart(2,"0")}/${String(day).padStart(2,"0")}`;
}

// Parse passport DOB strings into YYYY/MM/DD.
// Handles: DD-MM-YYYY, DD/MM/YYYY, DD - MM - YYYY,
//          DD MON YYYY, DD MON/MON YY  (English & French month abbrevs)
function _parseDOB(raw) {
  if (!raw) return "";
  const s = String(raw).trim();
  const MON = {
    jan:1, feb:2, fev:2, mar:3, apr:4, avr:4,
    may:5, mai:5, jun:6, jul:7, jui:7,
    aug:8, aou:8, sep:9, oct:10, nov:11, dec:12,
  };
  const pad = n => String(n).padStart(2, "0");

  // Normalise spaces around dashes/slashes
  const n = s.replace(/\s*-\s*/g, "-").replace(/\s*\/\s*/g, "/");

  // DD-MM-YYYY  or  DD/MM/YYYY
  let m = n.match(/^(\d{1,2})[-/](\d{1,2})[-/](\d{4})$/);
  if (m) return `${m[3]}/${pad(m[2])}/${pad(m[1])}`;

  // YYYY-MM-DD  or  YYYY/MM/DD  (already normalised order)
  m = n.match(/^(\d{4})[-/](\d{1,2})[-/](\d{1,2})$/);
  if (m) return `${m[1]}/${pad(m[2])}/${pad(m[3])}`;

  // DD MON [/ ALT_MON] YY[YY]  — e.g. "21 OCT 2005", "14 MAI / MAY 96"
  const parts = s.trim().split(/\s+/);
  if (parts.length >= 3 && /^\d{1,2}$/.test(parts[0])) {
    const monStr = parts[1].replace(/[^A-Za-z]/g, "").slice(0, 3).toLowerCase();
    const yearPart = parts[parts.length - 1].replace(/[^0-9]/g, "");
    const mon = MON[monStr];
    if (mon && /^\d{2,4}$/.test(yearPart)) {
      let year = parseInt(yearPart, 10);
      if (year < 100) year = year > 26 ? 1900 + year : 2000 + year;
      return `${year}/${pad(mon)}/${pad(parts[0])}`;
    }
  }

  console.warn("[OctoProbe] DOB parse failed:", raw, "— using as-is");
  return s;
}

function generatePerson() {
  const first = _pick(_FIRST);
  const last  = _pick(_LAST);
  const uname = (first.slice(0,3) + last.slice(0,3) + (1000000 + _rand(99000000))).toLowerCase()
                .normalize("NFD").replace(/[̀-ͯ]/g,"");
  const traveldoc = "PA" + Array.from({length:6}, () => _rand(10)).join("");
  return {
    name:        first,
    surname:     last,
    username:    uname,
    password:    _genPassword(),
    birth_date:  _genBirthdate(),
    gender:      _rand(2) ? "M" : "F",
    nationality: _pick(_NAT),
    traveldoc,
  };
}

// ---------------------------------------------------------------------------
// Email provider — switchable: "mailtm" (default) | "cloudflare"
// Storage keys: "email-provider", "cf-mail-domain", "cf-worker-url", "cf-worker-secret"
// ---------------------------------------------------------------------------

// ---- mail.tm ---------------------------------------------------------------

async function _mtDomain() {
  const r = await fetch(`${MAILTM}/domains`);
  const d = await r.json();
  return d["hydra:member"][0]["domain"];
}

async function _createMailTM() {
  const domain = await _mtDomain();
  const local  = Math.random().toString(36).slice(2, 12);
  const email  = `${local}@${domain}`;
  const pwd    = Math.random().toString(36).slice(2) + Math.random().toString(36).slice(2);

  await fetch(`${MAILTM}/accounts`, {
    method: "POST",
    headers: {"Content-Type":"application/json"},
    body: JSON.stringify({address: email, password: pwd}),
  });

  const tr = await fetch(`${MAILTM}/token`, {
    method: "POST",
    headers: {"Content-Type":"application/json"},
    body: JSON.stringify({address: email, password: pwd}),
  });
  const {token: jwt} = await tr.json();
  return {email, password: pwd, jwt};
}

async function _pollMailTM(jwt) {
  const r = await fetch(`${MAILTM}/messages`, {
    headers: {"Authorization": `Bearer ${jwt}`},
  });
  if (r.status === 401) {
    const err = new Error("mail.tm JWT expired (401)");
    err.authExpired = true;
    throw err;
  }
  const d = await r.json();
  const items = d["hydra:member"] ?? [];
  if (!items.length) return null;

  const mr = await fetch(`${MAILTM}/messages/${items[0].id}`, {
    headers: {"Authorization": `Bearer ${jwt}`},
  });
  return mr.json();
}

// ---- Cloudflare Worker inbox -----------------------------------------------
// jwt field stores the local part of the email address (the polling key).

async function _createCloudflareMail() {
  const {"cf-mail-domain": domain} = await chrome.storage.local.get("cf-mail-domain");
  if (!domain) throw new Error("Cloudflare email domain not configured (cf-mail-domain)");
  const local = Math.random().toString(36).slice(2, 12);
  const email = `${local}@${domain}`;
  return {email, password: "", jwt: email};  // jwt = full address used as polling key
}

async function _pollCloudflare(fullEmail) {
  const {
    "cf-worker-url":    workerUrl,
    "cf-worker-secret": secret,
  } = await chrome.storage.local.get(["cf-worker-url", "cf-worker-secret"]);

  if (!workerUrl || !secret) throw new Error("Cloudflare Worker not configured");

  const url = `${workerUrl.replace(/\/$/, "")}?email=${encodeURIComponent(fullEmail)}`;
  const r = await fetch(url, {headers: {"x-secret": secret}});
  if (!r.ok) throw new Error(`CF Worker error ${r.status}`);

  const data = await r.json();
  if (!data.found) return null;
  return data;  // {found, linkToken, codeToken}
}

// ---- unified facade --------------------------------------------------------

async function createTempEmail() {
  const {"email-provider": provider = "mailtm"} = await chrome.storage.local.get("email-provider");
  return provider === "cloudflare" ? _createCloudflareMail() : _createMailTM();
}

function _hexToUUID(h) {
  return `${h.slice(0,8)}-${h.slice(8,12)}-${h.slice(12,16)}-${h.slice(16,20)}-${h.slice(20,32)}`;
}

function _extractToken(msg) {
  const text = msg?.text ?? "";
  const rawHtml = Array.isArray(msg?.html) ? msg.html.join(" ") : (msg?.html ?? "");
  const html  = rawHtml.replace(/<[^>]+>/g, " ");
  const body  = text || html;
  const patterns = [
    /\b([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\b/i,
    /\b([0-9a-f]{32})\b/i,
    /[?&]token=([A-Za-z0-9\-]{6,})/,
    /token[=:\s]+([A-Za-z0-9\-]{6,})/i,
    /\b([0-9]{4,8})\b/,
  ];
  for (const p of patterns) {
    const m = body.match(p);
    if (!m) continue;
    let token = m[1];
    if (/^[0-9a-f]{32}$/i.test(token)) token = _hexToUUID(token);
    return token;
  }
  return null;
}

// ---------------------------------------------------------------------------
// reCAPTCHA API solvers
// ---------------------------------------------------------------------------

const _SOLVER_URLS = {
  "anti-captcha": "https://api.anti-captcha.com",
  "2captcha":     "https://api.2captcha.com",
  "capmonster":   "https://api.capmonster.cloud",
  "capsolver":    "https://api.capsolver.com",
};

// All four services share the same getBalance request/response shape.
// Returns null if the API is unreachable or returns an error — caller must not skip on null.
async function _fetchBalance(baseUrl, apiKey) {
  try {
    const r = await fetch(`${baseUrl}/getBalance`, {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({clientKey: apiKey}),
    });
    const d = await r.json();
    if (d.errorId === 0) return +(d.balance ?? 0);
  } catch (_) {}
  return null;
}

const _ANTICAPTCHA_KEYS = [
  "4b3f3ddf9569d07d3e66c47faa76f84b","f7772963b2e9974ee7e91c1da18e274f",
  "fc636707e6c69d93116eb45ae1a98dfd","51cfd3edeca41fc08e0ac3e4de89c884",
  "417a60094923c1a0a463df174e2bcadb","b1b0b96c51acd4ba4333d3eddb64ab9a",
  "1de98a29086aa627c2a3e9085bab57a2",
];
const _TWOCAPTCHA_KEYS  = ["f29a22fb1f956499db93706323d1d266","8945d1e803c52e98a888c7e8dd0376e8"];
const _CAPMONSTER_KEYS  = ["ce7d4529a80c407501dfbfbb5d5a8bbd","09fb5674452d54cbdfb45b6dc56b8e19"];
const _CAPSOLVER_KEY    = "CAP-D09F9ADF91258845DBD724E3D8C3069F0E4AFD8666D9260F44D34891884DA409";

// Shared polling solver for Anti-Captcha / CapMonster / 2Captcha (identical JSON API).
async function _solveACFormat(baseUrl, apiKey, pageUrl, siteKey, action) {
  const task = {type:"RecaptchaV2EnterpriseTaskProxyless", websiteURL:pageUrl, websiteKey:siteKey, isEnterprise:true};
  if (action) task.enterprisePayload = {action};
  const cr = await fetch(`${baseUrl}/createTask`, {
    method: "POST", headers: {"Content-Type":"application/json"},
    body: JSON.stringify({clientKey: apiKey, task}),
  });
  const cd = await cr.json();
  if (cd.errorId !== 0) throw new Error(`${baseUrl} createTask err ${cd.errorId}: ${cd.errorDescription}`);

  for (let i = 0; i < 40; i++) {
    await new Promise(r => setTimeout(r, 4000));
    const rr = await fetch(`${baseUrl}/getTaskResult`, {
      method: "POST", headers: {"Content-Type":"application/json"},
      body: JSON.stringify({clientKey: apiKey, taskId: cd.taskId}),
    });
    const rd = await rr.json();
    if (rd.errorId !== 0) throw new Error(`${baseUrl} getTaskResult err ${rd.errorId}: ${rd.errorDescription}`);
    if (rd.status === "ready") return rd.solution.gRecaptchaResponse;
  }
  throw new Error(`${baseUrl} timed out`);
}

async function _solveCapSolver(pageUrl, siteKey, action) {
  const task = {type:"ReCaptchaV2EnterpriseTaskProxyless", websiteURL:pageUrl, websiteKey:siteKey};
  if (action) task.enterprisePayload = {action};
  const cr = await fetch("https://api.capsolver.com/createTask", {
    method: "POST", headers: {"Content-Type":"application/json"},
    body: JSON.stringify({clientKey: _CAPSOLVER_KEY, task}),
  });
  const cd = await cr.json();
  if (cd.errorId !== 0) throw new Error(`CapSolver createTask err: ${cd.errorDescription}`);

  for (let i = 0; i < 40; i++) {
    await new Promise(r => setTimeout(r, 4000));
    const rr = await fetch("https://api.capsolver.com/getTaskResult", {
      method: "POST", headers: {"Content-Type":"application/json"},
      body: JSON.stringify({clientKey: _CAPSOLVER_KEY, taskId: cd.taskId}),
    });
    const rd = await rr.json();
    if (rd.errorId !== 0) throw new Error(`CapSolver getTaskResult err: ${rd.errorDescription}`);
    if (rd.status === "ready") return rd.solution.gRecaptchaResponse;
  }
  throw new Error("CapSolver timed out");
}

// Race all solvers — resolves on first successful token, rejects only if ALL fail.
function _raceSolvers(pageUrl, siteKey, action) {
  return new Promise((resolve, reject) => {
    let settled = false; let remaining = 4;
    function got(token) {
      if (settled) return;
      if (token) { settled = true; resolve(token); return; }
      if (--remaining === 0) reject(new Error("All solvers failed"));
    }
    _solveACFormat("https://api.anti-captcha.com", _pick(_ANTICAPTCHA_KEYS), pageUrl, siteKey, action).then(got).catch(() => got(null));
    _solveACFormat("https://api.2captcha.com",     _pick(_TWOCAPTCHA_KEYS),  pageUrl, siteKey, action).then(got).catch(() => got(null));
    _solveACFormat("https://api.capmonster.cloud", _pick(_CAPMONSTER_KEYS),  pageUrl, siteKey, action).then(got).catch(() => got(null));
    _solveCapSolver(pageUrl, siteKey, action).then(got).catch(() => got(null));
  });
}

// ---------------------------------------------------------------------------
// Tab helper + page-ready signaling
// ---------------------------------------------------------------------------

// Tear down any in-flight workflow completely before starting a new one.
async function _resetWorkflow() {
  stopF6();
  clearInterval(_pollInterval);
  _pollInterval = null;

  const savedTabId = _activeTabId;
  _activeTabId = null;

  await chrome.storage.local.remove([
    "workflow-type", "workflow-step",
    "register-person", "register-email",
    "email-token", "email-code-token", "emailPoll",
    "login-pending", "pending-account",
    "active-tab-id", "register-retried",
    "warmup-idle-state",
  ]);

  if (savedTabId) await chrome.tabs.remove(savedTabId).catch(() => {});

  // Sweep: close any stray target tabs.
  const allTabs = await chrome.tabs.query({});
  const stragglers = allTabs.filter(t => {
    try { return new URL(t.url ?? "").hostname === TARGET_HOST; } catch (_) { return false; }
  });
  if (stragglers.length > 0) await chrome.tabs.remove(stragglers.map(t => t.id)).catch(() => {});
}

async function _getActiveTab() {
  if (_activeTabId != null) {
    try { await chrome.tabs.get(_activeTabId); return _activeTabId; } catch (_) {}
  }
  const [found] = await chrome.tabs.query({url: `*://${TARGET_HOST}/*`});
  if (found) { _activeTabId = found.id; }
  else {
    const tab = await chrome.tabs.create({url: TARGET_URL, active: true});
    _activeTabId = tab.id;
  }
  await chrome.storage.local.set({"active-tab-id": _activeTabId});
  return _activeTabId;
}

let _pageReadyResolve = null, _pageReadyReject = null, _pageReadyTimer = null;

function waitForPageReady(timeoutMs = 30000) {
  return new Promise((resolve, reject) => {
    if (_pageReadyTimer) clearTimeout(_pageReadyTimer);
    _pageReadyTimer = setTimeout(() => {
      _pageReadyResolve = _pageReadyReject = null;
      reject(new Error("page-ready timeout"));
    }, timeoutMs);
    _pageReadyResolve = (data) => { clearTimeout(_pageReadyTimer); _pageReadyResolve = _pageReadyReject = null; resolve(data); };
    _pageReadyReject  = (err)  => { clearTimeout(_pageReadyTimer); _pageReadyResolve = _pageReadyReject = null; reject(err); };
  });
}

async function sendTabCmd(type, params = {}) {
  const tabId = await _getActiveTab();
  return chrome.tabs.sendMessage(tabId, {type, ...params});
}

// ---------------------------------------------------------------------------
// Polling state (in-memory; service worker may be restarted, but alarms persist)
// ---------------------------------------------------------------------------

let _pollInterval = null;

async function _doPollTick() {
  const {emailPoll} = await chrome.storage.local.get("emailPoll");
  if (!emailPoll) { clearInterval(_pollInterval); _pollInterval = null; return; }

  try {
    const {"email-provider": provider = "mailtm"} = await chrome.storage.local.get("email-provider");
    const msg = provider === "cloudflare"
      ? await _pollCloudflare(emailPoll.jwt)
      : await _pollMailTM(emailPoll.jwt);
    if (!msg) return;

    let linkToken, codeToken;
    if (provider === "cloudflare") {
      // CF worker returns codeToken (UUID form code) and linkToken (URL hex).
      // Use codeToken as the primary token — it is the value the form expects.
      // The linkToken URL hex is already in location.search on the token page.
      codeToken = msg.codeToken ?? null;
      linkToken  = codeToken ?? (msg.linkToken ?? null);  // fall back to linkToken only when no codeToken
    } else {
      linkToken = _extractToken(msg);
      codeToken = null;
    }
    if (!linkToken && !codeToken) return;

    clearInterval(_pollInterval);
    _pollInterval = null;
    await chrome.storage.local.remove("emailPoll");
    const storageUpdate = {};
    if (linkToken) storageUpdate["email-token"]      = linkToken;
    if (codeToken) storageUpdate["email-code-token"] = codeToken;
    await chrome.storage.local.set(storageUpdate);

    // Notify content script — prefer codeToken (UUID) over linkToken (hex).
    if (_activeTabId) chrome.tabs.sendMessage(_activeTabId, {type: "email-token", token: codeToken ?? linkToken}).catch(() => {});
  } catch (e) {
    if (e.authExpired) {
      clearInterval(_pollInterval);
      _pollInterval = null;
      await chrome.storage.local.remove("emailPoll");
      console.warn("[OctoProbe BG] mail.tm JWT expired — poll stopped");
    } else {
      console.error("[OctoProbe BG] email poll error:", e);
    }
  }
}

// ---------------------------------------------------------------------------
// Email poll helpers (used by F2)
// ---------------------------------------------------------------------------

function _startEmailPoll(jwt) {
  chrome.storage.local.set({emailPoll: {jwt}});
  if (!_pollInterval) _pollInterval = setInterval(_doPollTick, 6000);
}

async function _waitForEmailCodeToken(maxMs = 120000) {
  const deadline = Date.now() + maxMs;
  while (Date.now() < deadline) {
    const d = await chrome.storage.local.get(["email-code-token", "email-token"]);
    if (d["email-code-token"]) return d["email-code-token"];
    if (d["email-token"]) return d["email-token"];
    await new Promise(r => setTimeout(r, 2000));
  }
  return null;
}

// ---------------------------------------------------------------------------
// F1–F6 orchestration functions
// ---------------------------------------------------------------------------

async function F1_openAuthPage() {
  let state;
  try { ({state} = await sendTabCmd("cmd-get-state")); } catch (_) { state = "unknown"; }

  if (state === "auth") return {ok: true, status: "ready"};

  if (state !== "not-logged-in") {
    // Set up listener BEFORE navigation so page-ready can't arrive before we're listening.
    const pageReady = waitForPageReady(30000);
    const tabId = await _getActiveTab();
    await chrome.tabs.update(tabId, {url: TARGET_URL});
    ({state} = await pageReady);
  }

  if (state === "auth") return {ok: true, status: "ready"};

  // Retry loop: lang switch + login link click.
  // The lang switch can reload the page (if not already English), and the reload fires
  // a page-ready at almost exactly the 6 s timeout boundary — a tight race where the
  // lang-reload page-ready can bleed into authReady and resolve it with "not-logged-in".
  // Using a 15 s timeout for langReady eliminates the race, and the retry loop catches
  // any remaining edge cases.
  for (let attempt = 0; attempt < 3; attempt++) {
    if (attempt > 0) {
      await new Promise(r => setTimeout(r, 1500 + attempt * 500));
      try { ({state} = await sendTabCmd("cmd-get-state")); } catch (_) { state = "unknown"; }
      if (state === "auth") return {ok: true, status: "ready"};
      if (state !== "not-logged-in") {
        const pr = waitForPageReady(30000);
        const tabId = await _getActiveTab();
        await chrome.tabs.update(tabId, {url: TARGET_URL});
        ({state} = await pr);
        if (state === "auth") return {ok: true, status: "ready"};
      }
    }

    // Language switch may reload the page — 15 s gives ample room past the reload time.
    const langCmd = sendTabCmd("cmd-switch-lang").catch(() => {});
    const langReady = waitForPageReady(15000);
    await langCmd;
    await langReady.catch(() => {}); // ignore timeout (already English) or navigation

    // Confirm still on not-logged-in before arming the login listener.
    try { ({state} = await sendTabCmd("cmd-get-state")); } catch (_) { state = "unknown"; }
    if (state === "auth") return {ok: true, status: "ready"};

    // Login-link click always navigates — arm listener first.
    const authReady = waitForPageReady(20000);
    await sendTabCmd("cmd-click-login-link").catch(() => {});
    try {
      const {state: authState} = await authReady;
      if (authState === "auth") return {ok: true, status: "ready"};
      state = authState;
    } catch (_) {
      state = "timeout";
    }
    _runLog.entry(`F1: attempt ${attempt + 1} got state=${state} — retrying`);
  }
  throw new Error(`F1: expected auth, got ${state}`);
}

async function F2_register(person, emailAcct) {
  _runLog.entry(`F2: person=${person.name} ${person.surname} email=${emailAcct.email}`);
  await sendTabCmd("cmd-register-open-form");
  _runLog.entry("F2: form opened");
  await sendTabCmd("cmd-register-fill", {person, email: emailAcct.email});
  _runLog.entry("F2: form filled");

  // Arm BEFORE the submit loop — the server redirect to the token page can arrive
  // within milliseconds of the RGPD submit, before the loop exits and we could call
  // waitForPageReady.  If we armed it after, the page-ready signal would be lost.
  const tokenPageReady = waitForPageReady(180000);

  // 2-attempt retry is handled inside cmd-register-submit (_registerSubmit).
  _runLog.entry("F2: captcha submit (up to 2 attempts internally)");
  const result = await sendTabCmd("cmd-register-submit").catch(() => ({ok: true, status: "navigated"}));
  _runLog.entry(`F2: submit result → ${result.status}`);
  if (result.status === "ip_blocked") throw new Error("F2: IP blocked by captcha rate-limit");
  if (result.status !== "navigated" && result.status !== "submitted")
    throw new Error(`F2: register failed: ${result.status}`);

  _runLog.entry("F2: waiting for token page redirect + email code");
  _startEmailPoll(emailAcct.jwt);
  await tokenPageReady; // wait for token page navigation
  _runLog.entry("F2: token page reached — polling email");

  const codeToken = await _waitForEmailCodeToken(120000);
  if (!codeToken) throw new Error("F2: email code token timeout");
  _runLog.entry(`F2: email code token received: ${codeToken}`);

  // Write pending-account BEFORE submit click — page navigates on success,
  // killing the content script; main() on the next page picks this up and saves the CSV.
  await chrome.storage.local.set({
    "pending-account": {
      username:      person.username,
      password:      person.password,
      name:          person.name,
      surname:       person.surname,
      email:         emailAcct.email,
      birth_date:    person.birth_date,
      gender:        person.gender,
      nationality:   person.nationality ?? "CPV",
      traveldoc:     person.traveldoc,
      registered_at: new Date().toISOString(),
    }
  });

  await sendTabCmd("cmd-token-fill", {token: codeToken});
  const verifyReady = waitForPageReady(30000); // arm before submit navigates
  await sendTabCmd("cmd-token-submit").catch(() => {});
  const {state: finalState} = await verifyReady;
  _runLog.entry(`F2: verification done → state=${finalState}`);
  return {ok: true, status: finalState === "auth" ? "verified" : finalState};
}

async function F3_login(creds) {
  let state;
  try { ({state} = await sendTabCmd("cmd-get-state")); } catch (_) { state = "unknown"; }
  if (state === "logged-in") return {ok: true, status: "already-logged-in"};

  await sendTabCmd("cmd-login-fill", creds);

  for (let attempt = 0; attempt < 3; attempt++) {
    // Port-closed error = tab navigated mid-command; wait briefly for new page-ready.
    // Normal "navigated" return = _loginSubmit already waited 5s + session probe,
    // new page is fully loaded — no additional wait needed.
    const result = await sendTabCmd("cmd-login-submit").catch(async () => {
      await waitForPageReady(15000).catch(() => {});
      return {ok: true, status: "navigated"};
    });
    if (result.status === "logged-in" || result.status === "navigated") {
      return {ok: true, status: "logged-in"};
    }
    if (result.status === "rejected") return {ok: false, status: "rejected"};
    if (attempt < 2) await new Promise(r => setTimeout(r, 4000));
  }
  return {ok: false, status: "login_failed"};
}

async function F4_formFilling() {
  // Arm each listener BEFORE the command that triggers navigation.
  let pageReady = waitForPageReady(30000);
  await sendTabCmd("cmd-go-questionnaire").catch(() => {});
  const {state: qState} = await pageReady;
  if (qState !== "questionnaire") throw new Error(`F4: expected questionnaire, got ${qState}`);

  let fState;
  for (let attempt = 0; attempt < 2; attempt++) {
    pageReady = waitForPageReady(90000);
    await sendTabCmd("cmd-fill-questionnaire").catch(() => {});
    ({state: fState} = await pageReady);
    if (fState === "form") break;
    if (fState !== "questionnaire") throw new Error(`F4: expected form, got ${fState}`);
    // questionnaire page reloaded — retry
  }
  if (fState !== "form") throw new Error(`F4: questionnaire never reached form after retries`);

  pageReady = waitForPageReady(90000);
  await sendTabCmd("cmd-fill-form").catch(() => {});
  const {state: sState} = await pageReady;
  if (sState !== "schedule") throw new Error(`F4: expected schedule, got ${sState}`);

  return {ok: true, status: "form-ready"};
}

async function F5_scheduling(config) {
  return sendTabCmd("cmd-schedule", config).catch(e => ({ok: false, error: e.message}));
}

let _f6Interval = null;

function F6_keepSession() {
  if (_f6Interval) clearInterval(_f6Interval);
  _f6Interval = setInterval(() => sendTabCmd("cmd-keep-tick").catch(() => {}), 30000);
}

function stopF6() {
  if (_f6Interval) { clearInterval(_f6Interval); _f6Interval = null; }
}

async function F_warmup(config) {
  const idleStep = config.idleStep ?? "login";
  _runLog.start(`warmup_${idleStep}`);
  _runLog.entry(`username=${config.username} idleStep=${idleStep}`);
  stopF6();
  await _resetWorkflow();
  await chrome.storage.local.set({"warmup-idle-state": idleStep});

  _runLog.entry("F_warmup: opening auth page");
  await F1_openAuthPage();
  _runLog.entry("F_warmup: logging in");
  await F3_login({username: config.username, password: config.password});
  _runLog.entry("F_warmup: logged in");

  if (idleStep === "login") {
    F6_keepSession();
    _runLog.finish("ok idleStep=login");
    return {ok: true, idleStep: "login"};
  }

  _runLog.entry("F_warmup: navigating to questionnaire");
  let pageReady = waitForPageReady(30000);
  await sendTabCmd("cmd-go-questionnaire").catch(() => {});
  const {state: qState} = await pageReady;
  if (qState !== "questionnaire") throw new Error(`F_warmup: expected questionnaire, got ${qState}`);
  _runLog.entry("F_warmup: filling questionnaire");

  pageReady = waitForPageReady(60000);
  await sendTabCmd("cmd-fill-questionnaire").catch(() => {});
  const {state: fState} = await pageReady;
  if (fState !== "form") throw new Error(`F_warmup: expected form, got ${fState}`);
  _runLog.entry("F_warmup: on form page");

  if (idleStep === "form") {
    await sendTabCmd("cmd-fill-form-tabs").catch(() => {});
    F6_keepSession();
    _runLog.finish("ok idleStep=form");
    return {ok: true, idleStep: "form"};
  }

  if (idleStep === "schedule") {
    _runLog.entry("F_warmup: submitting form → schedule");
    pageReady = waitForPageReady(90000);
    await sendTabCmd("cmd-fill-form").catch(() => {});
    const {state: sState} = await pageReady;
    if (sState !== "schedule") throw new Error(`F_warmup: expected schedule, got ${sState}`);
    F6_keepSession();
    _runLog.finish("ok idleStep=schedule");
    return {ok: true, idleStep: "schedule"};
  }

  throw new Error(`F_warmup: unknown idleStep "${idleStep}"`);
}

async function F_apply() {
  _runLog.start("apply");
  stopF6();
  const {"warmup-idle-state": idleState = "login"} =
    await chrome.storage.local.get("warmup-idle-state");
  _runLog.entry(`F_apply: idleState=${idleState}`);

  if (idleState === "login") {
    _runLog.entry("F_apply: running F4 form filling");
    await F4_formFilling();
    _runLog.entry("F_apply: running F5 scheduling");
    const r = await F5_scheduling({});
    _runLog.finish(`ok status=${r.status ?? r.ok}`);
    return r;
  }

  if (idleState === "form") {
    _runLog.entry("F_apply: submitting form from idle");
    const pageReady = waitForPageReady(30000);
    await sendTabCmd("cmd-submit-form").catch(() => {});
    const {state: sState} = await pageReady;
    if (sState !== "schedule") throw new Error(`F_apply: expected schedule, got ${sState}`);
    _runLog.entry("F_apply: running F5 scheduling");
    const r = await F5_scheduling({});
    _runLog.finish(`ok status=${r.status ?? r.ok}`);
    return r;
  }

  if (idleState === "schedule") {
    _runLog.entry("F_apply: running F5 scheduling from idle");
    const r = await F5_scheduling({});
    _runLog.finish(`ok status=${r.status ?? r.ok}`);
    return r;
  }

  throw new Error(`F_apply: unknown idleState "${idleState}"`);
}

async function F_allInOne(config) {
  _runLog.start("all_in_one");
  stopF6();
  await _resetWorkflow();

  let person;
  if (config.realPerson) {
    const acct = generatePerson();
    const rp   = config.realPerson;
    const gRaw = String(rp.gender ?? "").trim().toUpperCase();
    person = {
      name:        String(rp.firstName ?? "").trim(),
      surname:     String(rp.lastName  ?? "").trim(),
      username:    acct.username,
      password:    acct.password,
      birth_date:  _parseDOB(rp.dob),
      gender:      gRaw === "F" || gRaw === "FEMALE" || gRaw === "FEMININO" ? "F" : "M",
      nationality: String(rp.nationality ?? "CPV").trim(),
      traveldoc:   String(rp.traveldoc  ?? "").trim(),
    };
  } else {
    person = generatePerson();
  }

  const emailAcct = await createTempEmail();
  await chrome.storage.local.set({
    "register-person":  person,
    "register-email":   emailAcct,
    "email-token":      null,
    "email-code-token": null,
  });

  _runLog.entry(`all_in_one: person=${person.name} ${person.surname} email=${emailAcct.email}`);
  await F1_openAuthPage();
  await sendTabCmd("cmd-log-ip").catch(() => {});
  await F2_register(person, emailAcct);
  _runLog.entry("all_in_one: registration done — logging in");

  await F1_openAuthPage();
  await F3_login({username: person.username, password: person.password});
  _runLog.entry("all_in_one: logged in — running F4+F5");

  await F4_formFilling();
  const r = await F5_scheduling({});
  _runLog.finish(`ok status=${r.status ?? r.ok}`);
  return r;
}

// ---------------------------------------------------------------------------
// Direct-call helpers — used by communication.js to avoid service-worker
// self-messaging (chrome.runtime.sendMessage does not loop back to the same SW).
// ---------------------------------------------------------------------------

function _runCheckProxy() {
  if (_activeTabId) chrome.tabs.sendMessage(_activeTabId, {type: "run-proxy-check"}).catch(() => {});
}

function _runDispatchAbort() {
  stopF6();
  if (_activeTabId) chrome.tabs.sendMessage(_activeTabId, {type: "abort"}).catch(() => {});
}

function _runCheckSolverBalances() {
  const solverKeys = {
    "anti-captcha": _pick(_ANTICAPTCHA_KEYS),
    "2captcha":     _pick(_TWOCAPTCHA_KEYS),
    "capmonster":   _pick(_CAPMONSTER_KEYS),
    "capsolver":    _CAPSOLVER_KEY,
  };
  const balances = {};
  Promise.all(Object.entries(_SOLVER_URLS).map(async ([name, url]) => {
    balances[name] = await _fetchBalance(url, solverKeys[name]);
  })).then(() => self.Comm?.send({type: "solver-balances", balances, ts: new Date().toISOString()}));
}

function _runRegister(realPerson) {
  (async () => {
    try {
      stopF6();
      await _resetWorkflow();
      const label = realPerson ? "register_real" : "register_auto";
      _runLog.start(label);
      let person;
      if (realPerson) {
        const acct = generatePerson();
        const gRaw = String(realPerson.gender ?? "").trim().toUpperCase();
        person = {
          name:        String(realPerson.firstName ?? "").trim(),
          surname:     String(realPerson.lastName  ?? "").trim(),
          username:    acct.username,
          password:    acct.password,
          birth_date:  _parseDOB(realPerson.dob),
          gender:      gRaw === "F" || gRaw === "FEMALE" || gRaw === "FEMININO" ? "F" : "M",
          nationality: String(realPerson.nationality ?? "").trim(),
          traveldoc:   String(realPerson.traveldoc  ?? "").trim(),
        };
        _runLog.entry(`person: ${person.name} ${person.surname} ${person.nationality} ${person.traveldoc}`);
      } else {
        person = generatePerson();
        _runLog.entry(`person (auto): ${person.name} ${person.surname}`);
      }
      const emailAcct = await createTempEmail();
      _runLog.entry(`email created: ${emailAcct.email}`);
      await chrome.storage.local.set({
        "register-person":  person,
        "register-email":   emailAcct,
        "email-token":      null,
        "email-code-token": null,
      });
      F1_openAuthPage()
        .then(() => sendTabCmd("cmd-log-ip").catch(() => {}))
        .then(() => F2_register(person, emailAcct))
        .then(r => {
          _runLog.finish(`ok status=${r.status}`);
          self.Comm?.send({type: "register-done", ...r, email: emailAcct.email, username: person.username, password: person.password});
        })
        .catch(e => {
          _runLog.finish(`error: ${e.message}`);
          self.Comm?.send({type: "error", reason: e.message});
        });
    } catch(e) {
      _runLog.finish(`error: ${String(e)}`);
      self.Comm?.send({type: "error", reason: String(e)});
    }
  })();
}

// ---------------------------------------------------------------------------
// Message handler
// ---------------------------------------------------------------------------

chrome.runtime.onInstalled.addListener(() => {
  chrome.storage.local.set({installedAt: new Date().toISOString()});
  console.log("[OctoProbe] Extension installed.");
});

// WS keepalive alarm — prevents service worker suspension while WS is open.
chrome.alarms.create("ws-ping", {periodInMinutes: 0.4});
chrome.alarms.onAlarm.addListener((alarm) => {
  if (alarm.name === "ws-ping" && self.Comm?.isConnected()) {
    self.Comm.send({type: "ping"});
  }
});

chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {

  if (msg.type === "page-ready") {
    if (sender.tab?.id === _activeTabId) _pageReadyResolve?.({state: msg.state, url: msg.url});
    sendResponse({ok: true});

  } else if (msg.type === "comm-dispatch") {
    // Route a popup or internal caller through the same command layer as the manager.
    if (self.Comm) {
      self.Comm.dispatch(msg.payload).catch(e => console.error("[OctoProbe BG] comm-dispatch error:", e));
    }
    sendResponse({ok: true});

  } else if (msg.type === "ping") {
    sendResponse({type: "pong", version: chrome.runtime.getManifest().version});

  } else if (msg.type === "get-creds") {
    chrome.storage.local.get(["username","password"], (d) => {
      sendResponse({username: d.username||"", password: d.password||""});
    });
    return true;

  } else if (msg.type === "save-creds") {
    chrome.storage.local.set({username: msg.username, password: msg.password}, () => {
      sendResponse({ok: true});
    });
    return true;

  } else if (msg.type === "log-entry") {
    _runLog.entry(msg.msg);
    sendResponse({ok: true});

  } else if (msg.type === "run-register") {
    _runRegister(msg.realPerson);
    sendResponse({ok: true});

  } else if (msg.type === "run-F6") {
    F6_keepSession();
    sendResponse({ok: true});
    return true;

  } else if (msg.type === "start-email-poll") {
    _startEmailPoll(msg.jwt);
    sendResponse({ok: true});

  } else if (msg.type === "stop-email-poll") {
    clearInterval(_pollInterval);
    _pollInterval = null;
    chrome.storage.local.remove("emailPoll");
    sendResponse({ok: true});

  } else if (msg.type === "sleep") {
    setTimeout(() => sendResponse({ok: true}), msg.ms);
    return true;

  } else if (msg.type === "dispatch-proxy-check") {
    _runCheckProxy();
    sendResponse({ok: !!_activeTabId});

  } else if (msg.type === "dispatch-abort") {
    _runDispatchAbort();
    sendResponse({ok: true});

  } else if (msg.type === "save-account") {
    // Persist account to chrome.storage.local and download a CSV file.
    const account = msg.account;
    const ts = new Date().toISOString().replace(/[:.]/g, "-").slice(0, 19);
    const filename = `account_${ts}.csv`;

    // Append to running accounts list in storage
    chrome.storage.local.get("accounts", ({accounts}) => {
      const list = Array.isArray(accounts) ? accounts : [];
      list.push(account);
      chrome.storage.local.set({accounts: list});
    });

    // Build CSV and trigger download
    const headers = Object.keys(account);
    const values  = headers.map(k => `"${String(account[k]).replace(/"/g, '""')}"`);
    const csv     = headers.join(",") + "\n" + values.join(",") + "\n";
    const dataUrl = "data:text/csv;charset=utf-8," + encodeURIComponent(csv);
    chrome.downloads.download({url: dataUrl, filename, saveAs: false}, (id) => {
      console.log(`[OctoProbe BG] Account saved → ${filename} (download id ${id})`);
      sendResponse({ok: true, filename});
    });
    return true;

  } else if (msg.type === "close-tab") {
    const tabId = sender.tab?.id;
    if (tabId) chrome.tabs.remove(tabId);
    sendResponse({ok: true});

  } else if (msg.type === "clear-workflow") {
    chrome.storage.local.remove([
      "workflow-type","workflow-step","register-person","register-email","email-token","email-code-token","emailPoll","warmup-idle-state"
    ]);
    sendResponse({ok: true});

  } else if (msg.type === "inject-alert-capture") {
    // Inject alert override into the page's MAIN world via scripting API.
    // This bypasses the site's CSP (inline <script> injection is blocked by CSP,
    // but chrome.scripting.executeScript with world:"MAIN" is not).
    //
    // confirmToo=true adds confirm()/prompt() overrides — only safe at form-submit
    // time, NOT during login (reCAPTCHA Enterprise checks native-function toString()).
    const tabId = sender.tab?.id;
    if (!tabId) { sendResponse({ok: false, error: "no sender tab"}); return true; }
    chrome.scripting.executeScript({
      target: {tabId},
      world: "MAIN",
      args: [!!msg.confirmToo],
      func: (confirmToo) => {
        if (!window._octoAlertHooked) {
          window._octoAlertHooked = true;
          window.alert = function(m) {
            window._octoLastAlert = String(m);
            document.dispatchEvent(new CustomEvent("octo-alert", {detail: {msg: String(m)}}));
          };
          console.log("[OctoProbe BG] window.alert suppressed via scripting API");
        }
        // confirm/prompt: only override when explicitly requested (form-submit step).
        // Overriding these during login lets reCAPTCHA Enterprise detect tampered
        // native APIs via toString(), which lowers the session score.
        if (confirmToo && !window._octoConfirmHooked) {
          window._octoConfirmHooked = true;
          window.confirm = function(m) {
            window._octoLastConfirm = String(m);
            document.dispatchEvent(new CustomEvent("octo-confirm", {detail: {msg: String(m)}}));
            return true;
          };
          window.prompt = function(_m, def) { return def ?? ""; };
          console.log("[OctoProbe BG] window.confirm/prompt suppressed via scripting API");
        }
      },
    })
    .then(() => sendResponse({ok: true}))
    .catch(e => sendResponse({ok: false, error: String(e)}));
    return true;

  } else if (msg.type === "reset-recaptcha") {
    // Reset the reCAPTCHA widget before a retry attempt — clears any stale token.
    const tabId = sender.tab?.id;
    if (!tabId) { sendResponse({ok: false, error: "no sender tab"}); return true; }
    chrome.scripting.executeScript({
      target: {tabId}, world: "MAIN",
      func: () => {
        try {
          if (typeof grecaptcha !== "undefined") {
            if (typeof grecaptcha.enterprise?.reset === "function") grecaptcha.enterprise.reset();
            else if (typeof grecaptcha.reset === "function") grecaptcha.reset();
          }
        } catch (_) {}
        for (const sel of ["#g-recaptcha-response-1", "#g-recaptcha-response"]) {
          const ta = document.querySelector(sel);
          if (ta) ta.value = "";
        }
        console.log("[OctoProbe BG] reCAPTCHA widget reset for retry");
      },
    })
    .then(() => sendResponse({ok: true}))
    .catch(e => sendResponse({ok: false, error: String(e)}));
    return true;

  } else if (msg.type === "fill-token") {
    // Fill the token input in MAIN world — guaranteed to bypass any page-level block.
    const tabId = sender.tab?.id;
    const {token} = msg;
    if (!tabId) { sendResponse({ok: false, error: "no sender tab"}); return true; }
    chrome.scripting.executeScript({
      target: {tabId},
      world: "MAIN",
      args: [token],
      func: async (token) => {
        const sels = [
          "input[name='tokenInput']",   // primary — confirmed from 2.activation.html
          "input[id='tokenInput']",
          "input[id*='oken']",
          "#mainContent input[type='text']",
          "form input[type='text']",
        ];
        let el = null;
        for (const sel of sels) {
          const found = document.querySelector(sel);
          if (found && found.offsetParent !== null) { el = found; break; }
        }
        if (!el) return {ok: false, error: "input not found"};

        el.focus();
        const sleep = ms => new Promise(r => setTimeout(r, ms));
        const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")?.set;

        for (const ch of token) {
          const delay = 60 + Math.random() * 120 + (Math.random() < 0.07 ? 280 + Math.random() * 400 : 0);
          await sleep(delay);
          el.dispatchEvent(new KeyboardEvent("keydown",  {key: ch, bubbles: true, cancelable: true}));
          el.dispatchEvent(new KeyboardEvent("keypress", {key: ch, bubbles: true, cancelable: true}));
          const cur = el.value;
          if (setter) setter.call(el, cur + ch); else el.value = cur + ch;
          el.dispatchEvent(new InputEvent("input", {data: ch, inputType: "insertText", bubbles: true}));
          el.dispatchEvent(new KeyboardEvent("keyup", {key: ch, bubbles: true}));
        }

        await sleep(80 + Math.random() * 100);
        el.dispatchEvent(new Event("change", {bubbles: true}));
        console.log("[OctoProbe BG] type-token result:", el.value);
        return {ok: true, value: el.value};
      },
    })
    .then(([r]) => sendResponse(r?.result ?? {ok: false}))
    .catch(e => sendResponse({ok: false, error: String(e)}));
    return true;

  } else if (msg.type === "solve-recaptcha-api") {
    const {pageUrl, siteKey, action = null} = msg;
    (async () => {
      try {
        const {
          "captcha-solver":   solver   = "capsolver",
          "captcha-parallel": parallel = false,
        } = await chrome.storage.local.get(["captcha-solver","captcha-parallel"]);

        let token;
        if (parallel) {
          token = await _raceSolvers(pageUrl, siteKey, action);
        } else {
          const primary = (solver in _SOLVER_URLS) ? solver : "capsolver";
          // Pick key once — same key used for balance check and solve call.
          const pickedKey = primary === "anti-captcha" ? _pick(_ANTICAPTCHA_KEYS)
            : primary === "2captcha"   ? _pick(_TWOCAPTCHA_KEYS)
            : primary === "capmonster" ? _pick(_CAPMONSTER_KEYS)
            : _CAPSOLVER_KEY;

          // Check primary solver balance; skip to fallbacks only on confirmed zero.
          // null means the balance API was unreachable — proceed with primary anyway.
          const bal = await _fetchBalance(_SOLVER_URLS[primary], pickedKey);
          if (bal !== null) self.Comm?.send({type: "solver-balance", solver: primary, balance: bal, ts: new Date().toISOString()});
          if (bal !== null && bal < 0.01) console.warn(`[OctoProbe BG] ${primary} balance $${bal.toFixed(4)} — skipping to fallbacks`);

          let usePrimary = bal === null || bal >= 0.01;
          if (usePrimary) {
            try {
              token = primary === "capsolver"
                ? await _solveCapSolver(pageUrl, siteKey, action)
                : await _solveACFormat(_SOLVER_URLS[primary], pickedKey, pageUrl, siteKey, action);
            } catch (primaryErr) {
              console.warn(`[OctoProbe BG] Primary solver "${primary}" failed: ${primaryErr.message} — trying fallback`);
              usePrimary = false;
            }
          }

          if (!usePrimary) {
            // Race all remaining solvers as fallback.
            const fallbackEntries = Object.entries(_SOLVER_URLS).filter(([k]) => k !== primary);
            token = await new Promise((resolve, reject) => {
              let remaining = fallbackEntries.length;
              let settled = false;
              for (const [k] of fallbackEntries) {
                const fn = k === "capsolver"
                  ? () => _solveCapSolver(pageUrl, siteKey, action)
                  : () => _solveACFormat(_SOLVER_URLS[k],
                      k === "anti-captcha" ? _pick(_ANTICAPTCHA_KEYS)
                        : k === "2captcha" ? _pick(_TWOCAPTCHA_KEYS)
                        : _pick(_CAPMONSTER_KEYS),
                      pageUrl, siteKey, action);
                fn().then(t => {
                  if (!settled && t) { settled = true; resolve(t); }
                }).catch(() => {
                  if (--remaining === 0 && !settled) reject(new Error("All solvers failed"));
                });
              }
            });
          }
        }
        sendResponse({ok: true, token});
      } catch(e) {
        sendResponse({ok: false, error: String(e)});
      }
    })();
    return true;

  } else if (msg.type === "inject-recaptcha-token") {
    const tabId = sender.tab?.id;
    const {token} = msg;
    if (!tabId) { sendResponse({ok: false, error: "no sender tab"}); return true; }
    chrome.scripting.executeScript({
      target: {tabId}, world: "MAIN", args: [token],
      func: (token) => {
        // 1. Write the solved token into both reCAPTCHA hidden textareas
        for (const sel of ["#g-recaptcha-response-1","#g-recaptcha-response"]) {
          const ta = document.querySelector(sel);
          if (!ta) continue;
          const setter = Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype,"value")?.set;
          if (setter) setter.call(ta, token); else ta.value = token;
          ta.dispatchEvent(new Event("input",  {bubbles:true}));
          ta.dispatchEvent(new Event("change", {bubbles:true}));
        }
        // 2. Patch grecaptcha getResponse() AND execute() to return our token.
        //    getResponse() covers sites that read the response after widget callback fires.
        //    execute() covers sites whose doLogin() calls execute() for a fresh token —
        //    we return the pre-solved token immediately so no new challenge fires.
        try {
          const _tok = token;
          if (window.grecaptcha?.enterprise?.getResponse) {
            window.grecaptcha.enterprise.getResponse = function() { return _tok; };
          }
          if (window.grecaptcha?.getResponse) {
            window.grecaptcha.getResponse = function() { return _tok; };
          }
          if (window.grecaptcha?.enterprise?.execute) {
            window.grecaptcha.enterprise.execute = function() { return Promise.resolve(_tok); };
          }
          if (window.grecaptcha?.execute) {
            window.grecaptcha.execute = function() { return Promise.resolve(_tok); };
          }
          console.log("[OctoProbe] grecaptcha getResponse + execute patched to return injected token");
        } catch(_) {}
        // 3. Walk ___grecaptcha_cfg.clients and call the widget success callback.
        //    This sets the widget's internal "solved" state and may enable the submit
        //    button — required for sites where doLogin() checks widget state before
        //    reading the token rather than calling getResponse() directly.
        try {
          const _tok = token;
          const seen = new WeakSet();
          function _findAndCallCb(obj, depth) {
            if (!obj || depth > 8 || typeof obj !== "object" || seen.has(obj)) return false;
            seen.add(obj);
            if (typeof obj.callback === "function") {
              try { obj.callback(_tok); } catch(_) {}
              console.log("[OctoProbe] findCb: fired widget success callback");
              return true;
            }
            for (const v of Object.values(obj)) {
              if (_findAndCallCb(v, depth + 1)) return true;
            }
            return false;
          }
          for (const client of Object.values(window.___grecaptcha_cfg?.clients ?? {})) {
            if (_findAndCallCb(client, 0)) break;
          }
        } catch(_) {}
        // 4. Notify content script (CustomEvent crosses MAIN→isolated boundary via DOM)
        document.dispatchEvent(new CustomEvent("octo-recaptcha-pass", {detail:{token}}));
      },
    })
    .then(() => sendResponse({ok: true}))
    .catch(e => sendResponse({ok: false, error: String(e)}));
    return true;

  } else if (msg.type === "cdp-click") {
    // Dispatch a trusted mouse click via CDP Input.dispatchMouseEvent.
    // isTrusted=true inside cross-origin iframes (e.g. reCAPTCHA anchor frame).
    const tabId = sender.tab?.id;
    if (!tabId) { sendResponse({ok: false, error: "no sender tab"}); return true; }
    (async () => {
      try {
        await chrome.debugger.attach({tabId}, "1.3");
        const {x, y} = msg;
        const base = {x, y, modifiers: 0};
        await chrome.debugger.sendCommand({tabId}, "Input.dispatchMouseEvent",
          {...base, type: "mouseMoved", button: "none", clickCount: 0, buttons: 0});
        await new Promise(r => setTimeout(r, 40 + Math.random() * 60));
        await chrome.debugger.sendCommand({tabId}, "Input.dispatchMouseEvent",
          {...base, type: "mousePressed", button: "left", clickCount: 1, buttons: 1});
        await new Promise(r => setTimeout(r, 80 + Math.random() * 100));
        await chrome.debugger.sendCommand({tabId}, "Input.dispatchMouseEvent",
          {...base, type: "mouseReleased", button: "left", clickCount: 1, buttons: 0});
        await chrome.debugger.detach({tabId});
        sendResponse({ok: true});
      } catch(e) {
        console.warn("[OctoProbe BG] CDP click failed:", e.message);
        try { await chrome.debugger.detach({tabId}); } catch(_) {}
        sendResponse({ok: false, error: String(e)});
      }
    })();
    return true;

  } else if (msg.type === "get-recaptcha-sitekey") {
    const tabId = sender.tab?.id;
    if (!tabId) { sendResponse({ok: false, siteKey: null}); return true; }
    chrome.scripting.executeScript({
      target: {tabId}, world: "MAIN",
      func: () => {
        // 1. data-sitekey attribute
        const el = document.querySelector("[data-sitekey]");
        if (el?.dataset?.sitekey) return el.dataset.sitekey;
        // 2. reCAPTCHA iframe src (?k= param)
        const fr = document.querySelector("iframe[src*='recaptcha']");
        if (fr) { const m = fr.src.match(/[?&]k=([A-Za-z0-9_-]+)/); if (m) return m[1]; }
        // 3. Walk ___grecaptcha_cfg for a sitekey-shaped string
        try {
          const seen = new WeakSet();
          function findKey(obj, depth) {
            if (depth > 6 || !obj || typeof obj !== "object" || seen.has(obj)) return null;
            seen.add(obj);
            for (const [k, v] of Object.entries(obj)) {
              if ((k === "sitekey" || k === "key") && typeof v === "string" && v.length > 20) return v;
              const r = findKey(v, depth + 1);
              if (r) return r;
            }
            return null;
          }
          for (const client of Object.values(window.___grecaptcha_cfg?.clients ?? {})) {
            const key = findKey(client, 0);
            if (key) return key;
          }
        } catch(_) {}
        return null;
      },
    })
    .then(([r]) => sendResponse({ok: true, siteKey: r?.result ?? null}))
    .catch(() => sendResponse({ok: false, siteKey: null}));
    return true;

  } else if (msg.type === "exec-page-script") {
    // Execute arbitrary code in the MAIN world of the sender's tab.
    const tabId = sender.tab?.id;
    if (!tabId) { sendResponse({ok: false, error: "no sender tab"}); return true; }
    const code = msg.code ?? "";
    chrome.scripting.executeScript({
      target: {tabId},
      world: "MAIN",
      args: [code],
      func: (c) => { try { return (new Function(c))(); } catch(e) { return {error: String(e)}; } },
    })
    .then(([r]) => sendResponse({ok: true, result: r?.result ?? null}))
    .catch(e => sendResponse({ok: false, error: String(e)}));
    return true;

  } else if (msg.type === "ws-send") {
    // Relay a message from content.js to the manager via WebSocket.
    if (self.Comm) self.Comm.send(msg.data);
    sendResponse({ok: true});

  } else if (msg.type === "check-solver-balances") {
    _runCheckSolverBalances();
    sendResponse({ok: true});

  } else if (msg.type === "inject-recaptcha-watcher") {
    // Inject reCAPTCHA solve watcher into MAIN world via scripting API (bypasses CSP).
    // Resets any previous watcher and starts a fresh polling loop.
    const tabId = sender.tab?.id;
    if (!tabId) { sendResponse({ok: false, error: "no sender tab"}); return true; }
    chrome.scripting.executeScript({
      target: {tabId},
      world: "MAIN",
      func: () => {
        // Reset watcher state so a new solve is required
        window._octoRcpToken   = null;
        window._octoRcpWatcher = true;

        // Reset the reCAPTCHA widget to clear the previous (possibly expired) response.
        // Without this, getResponse() returns the old token and check() fires instantly.
        try {
          if (typeof grecaptcha !== "undefined") {
            if (typeof grecaptcha.enterprise?.reset === "function") grecaptcha.enterprise.reset();
            else if (typeof grecaptcha.reset === "function") grecaptcha.reset();
          }
        } catch (_) {}
        // Also clear the hidden textarea directly — fallback for when reset() silently fails.
        const _ta = document.querySelector("#g-recaptcha-response-1,#g-recaptcha-response");
        if (_ta) _ta.value = "";

        function check() {
          if (!window._octoRcpWatcher) return;
          try {
            const r = typeof grecaptcha !== "undefined" &&
              (grecaptcha.enterprise?.getResponse?.() || grecaptcha.getResponse?.());
            if (r && r.length > 0) {
              window._octoRcpToken = r;
              document.dispatchEvent(new CustomEvent("octo-recaptcha-pass", {detail: {token: r}}));
              return;
            }
          } catch (_) {}
          setTimeout(check, 800);
        }
        check();
        console.log("[OctoProbe BG] reCAPTCHA watcher injected (widget reset)");
      },
    })
    .then(() => sendResponse({ok: true}))
    .catch(e => sendResponse({ok: false, error: String(e)}));
    return true;
  }

  return true;
});
