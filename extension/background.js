// Service worker — credential storage, workflow dispatch, mail.tm email polling.

importScripts('communication.js');

// Load static config into storage so CF email settings are available without popup interaction.
async function _loadExtensionConfig() {
  try {
    const cfg = await fetch(chrome.runtime.getURL("config.json")).then(r => {
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      return r.json();
    });
    const update = {};
    if (cfg.cfMailDomain)   update["cf-mail-domain"]   = cfg.cfMailDomain;
    if (cfg.cfWorkerUrl)    update["cf-worker-url"]    = cfg.cfWorkerUrl;
    if (cfg.cfWorkerSecret) update["cf-worker-secret"] = cfg.cfWorkerSecret;
    if (Object.keys(update).length) await chrome.storage.local.set(update);
    return cfg;
  } catch (e) {
    console.warn("[OctoProbe] config.json CF load failed:", e);
    return null;
  }
}

async function _ensureCfEmailConfig() {
  const stored = await chrome.storage.local.get(["cf-mail-domain", "cf-worker-url", "cf-worker-secret"]);
  if (String(stored["cf-mail-domain"] ?? "").trim()) return stored;
  await _loadExtensionConfig();
  return chrome.storage.local.get(["cf-mail-domain", "cf-worker-url", "cf-worker-secret"]);
}

self._ensureCfEmailConfig = _ensureCfEmailConfig;

(async () => { await _loadExtensionConfig(); })();

const TARGET_URL  = "https://pedidodevistos.mne.gov.pt/VistosOnline/";
const TARGET_HOST = new URL(TARGET_URL).hostname;
const MAILTM    = "https://api.mail.tm";

// Session-unique key for MAIN-world property names and custom event names.
// Regenerated on every SW start — avoids detectable static "_octo*" fingerprint.
const _SK       = Math.random().toString(36).slice(2, 10);
const _EVT_ALERT = 'a' + _SK;
const _EVT_RCP   = 'r' + _SK;

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

  async function finish(status) {
    if (!_label) return;
    _push(`=== finished: ${status} ===`);
    const ts = _ts().replace(/[:.]/g, "-").slice(0, 19);
    // Capture label + content as locals BEFORE clearing state.
    // storage.get is async — without this, _label and _lines are already cleared
    // by the time the .then() fires, so the downloaded file gets null/empty content.
    const label   = _label;
    const content = _lines.join("\n") + "\n";
    _lines = [];
    _label = null;
    try {
      const d   = await chrome.storage.local.get("botId");
      const sid = d.botId ? `_${d.botId.slice(0, 8)}` : "";
      const filename = `${label}${sid}_${ts}.log`;
      const url = "data:text/plain;charset=utf-8," + encodeURIComponent(content);
      const downloadId = await chrome.downloads.download({ url, filename, saveAs: false });
      // Wait for the file to be fully written before resolving.
      // chrome.downloads.download() resolves when Chrome *accepts* the request, not when
      // the file is on disk.  For Octo profiles each profile is its own Chrome process —
      // the process exits when the manager calls stopProfile(), cancelling any download
      // that hasn't finished writing yet.  Waiting for onChanged state=complete ensures
      // the file is on disk before we send the done/error WS message that triggers teardown.
      await new Promise((resolve) => {
        const timer = setTimeout(resolve, 5000); // safety cap — never hang
        function onChanged(delta) {
          if (delta.id !== downloadId) return;
          const s = delta.state?.current;
          if (s === "complete" || s === "interrupted") {
            clearTimeout(timer);
            chrome.downloads.onChanged.removeListener(onChanged);
            resolve();
          }
        }
        chrome.downloads.onChanged.addListener(onChanged);
      });
    } catch (_) {}
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

function _pick(arr) { return arr[Math.floor(Math.random() * arr.length)]; }
function _rand(n)   { return Math.floor(Math.random() * n); }

// ---------------------------------------------------------------------------
// Fake CPV person generator (used when manager does not supply realPerson)
// ---------------------------------------------------------------------------

const _FIRST_M = ["João","Carlos","Hélder","António","Manuel","Sérgio","Leandro","Dário","Orlando",
                  "Arlindo","Paulo","Filipe","Osvaldo","Wilfredo","Adílson","Valdemar","Hailton",
                  "Pedro","Rui","Nuno","Décio","Sandro","Edílson","Lúcio","Gilberto"];
const _FIRST_F = ["Maria","Ana","Edna","Rosa","Lúcia","Eunice","Arminda","Sandra","Graça","Noemia",
                  "Filomena","Carla","Nair","Isadora","Vera","Conceição","Milena","Suzete","Lisete",
                  "Ercília","Anilsa","Dulce","Odete","Yara","Valdira"];
const _LAST    = ["Semedo","Tavares","Correia","Lima","Varela","Monteiro","Évora","Fernandes",
                  "Rodrigues","Furtado","Mendes","Barros","Cruz","Veiga","Delgado","Pires",
                  "Andrade","Soares","Cardoso","Lopes","Brito","Gonçalves","Neves","Spencer",
                  "Borges","Moreno","Duarte","Fontes","Mascarenhas","Santos"];
const _PARTICLES = ["da","de","do","dos","das"];

// Combining diacritical marks range U+0300–U+036F
const _COMB = new RegExp("[\\u0300-\\u036f]","g");

function _genGivenName(gender) {
  const pool = gender === "M" ? _FIRST_M : _FIRST_F;
  const a = _pick(pool);
  const r = _rand(10);
  if (r < 4) { let b = _pick(pool); if (b===a) b=_pick(pool); return `${a} ${b}`; }
  if (r < 7) { const p=_pick(_PARTICLES); let b=_pick(pool); if(b===a)b=_pick(pool); return `${a} ${p} ${b}`; }
  return a;
}

function _genSurname() {
  const a = _pick(_LAST);
  if (_rand(2)) { let b=_pick(_LAST); if(b===a)b=_pick(_LAST); return `${a} ${b}`; }
  return a;
}

function _genPassword() {
  const sp="!@#$%&*_+", di="0123456789", up="ABCDEFGHIJKLMNOPQRSTUVWXYZ", lo="abcdefghijklmnopqrstuvwxyz";
  const all = sp+di+up+lo;
  let c=[sp[_rand(sp.length)], di[_rand(di.length)], up[_rand(up.length)]];
  for (let i=0;i<13;i++) c.push(all[_rand(all.length)]);
  for (let i=c.length-1;i>0;i--) { const j=_rand(i+1); [c[i],c[j]]=[c[j],c[i]]; }
  return c.join("");
}

function _genUsername(firstName, lastName) {
  // Normalize accents (é→e, ã→a, ç→c) before extracting chars to avoid "srg" instead of "ser".
  const norm = s => s.normalize("NFD").replace(_COMB,"").split(" ")[0].toLowerCase().replace(/[^a-z]/g,"");
  const f = norm(firstName), l = norm(lastName);
  return f.slice(0,3) + l.slice(0,3) + String(_rand(90000000)+10000000); // e.g. "serlim56027114"
}

function generatePerson() {
  const gender    = _rand(2) === 0 ? "M" : "F";
  const firstName = _genGivenName(gender);
  const lastName  = _genSurname();
  const year      = 1964 + _rand(41);
  const month     = 1   + _rand(12);
  const day       = 1   + _rand(28);
  const pad       = n => String(n).padStart(2,"0");
  return {
    name:       firstName,
    surname:    lastName,
    username:   _genUsername(firstName, lastName),
    password:   _genPassword(),
    birth_date: `${year}/${pad(month)}/${pad(day)}`,
    gender,
    nationality: "CPV",
    traveldoc:   _pick(["PA","PB","PC"]) + String(_rand(900000)+100000),
  };
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


// ---------------------------------------------------------------------------
// Email provider — switchable: "mailtm" (default) | "cloudflare"
// Storage keys: "email-provider", "cf-mail-domain", "cf-worker-url", "cf-worker-secret"
// ---------------------------------------------------------------------------

// ---- mail.tm ---------------------------------------------------------------

async function _mtDomain() {
  const r = await fetch(`${MAILTM}/domains`);

  if (!r.ok) {
    throw new Error(`Failed to fetch domains: ${r.status}`);
  }

  const d = await r.json();

  const domains = (d["hydra:member"] || [])
    .filter(x => x.isActive)
    .map(x => x.domain);

  if (!domains.length) {
    throw new Error("No active mail.tm domains available");
  }

  // random domain
  return domains[Math.floor(Math.random() * domains.length)];
}

function _randomstring(len = 10) {
  let s = '';
  while (s.length < len) s += Math.random().toString(36).slice(2);
  return s.slice(0, len);
}

async function _createMailTM(retries = 5) {
  let lastErr;

  for (let i = 0; i < retries; i++) {
    try {
      const domain = await _mtDomain();
      const local = _randomstring(10 + Math.floor(Math.random() * 5));
      const email = `${local}@${domain}`;

      const pwd = 
        _randomstring(16) +
        _randomstring(16);
      
      // create account
      const ar = await fetch(`${MAILTM}/accounts`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
        },
        body: JSON.stringify({
          address: email,
          password: pwd,
        }),
      });

      if (!ar.ok) {
        const txt = await ar.text();
        throw new Error(
          `Account creation failed (${ar.status}): ${txt}`
        );
      }

      // login / get token
      const tr= await fetch(`${MAILTM}/token`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
        },
        body: JSON.stringify({
          address: email,
          password: pwd,
        }),
      });

      if (!tr.ok) {
        const txt = await tr.text();
        throw new Error(
          `Token request failed (${tr.status}): ${txt}`
        );
      }

      const td = await tr.json();

      if (!td.token) {
        throw new Error("mail.tm did not return JWT");
      }

      return {
        email,
        password: pwd,
        jwt: td.token,
        domain,
      };
    } catch (err) {
      lastErr = err;
      console.error(`mail.tm retry ${i + 1} failed:`, err);
    }
  }
  throw lastErr;
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

  if (!r.ok) {
    throw new Error(`Failed to fetch messages: ${r.status}`);
  }

  const d = await r.json();
  const items = d["hydra:member"] ?? [];

  if (!items.length) return null;

  // newest first
  items.sort(
    (a, b) =>
      new Date(b.createdAt) - new Date(a.createdAt)
  );

  const msgId = items[0].id;
  const mr = await fetch(`${MAILTM}/messages/${msgId}`, {
    headers: {"Authorization": `Bearer ${jwt}`},
  });

  if (!mr.ok) {
    throw new Error(
      `Failed to fetch message body: ${mr.status}`
    );
  }

  return mr.json();
}

// ---- Cloudflare Worker inbox -----------------------------------------------
// jwt field stores the local part of the email address (the polling key).

async function _createCloudflareMail() {
  await _ensureCfEmailConfig();
  const {"cf-mail-domain": domain} = await chrome.storage.local.get("cf-mail-domain");
  if (!String(domain ?? "").trim()) {
    const err = new Error(
      "Cloudflare email domain not configured — set cfMailDomain in octo_signal/extension/config.json (copy config.json.example), rebuild the extension, or let the hub send cfDomain on the register command",
    );
    err.proxyStatus = "email_provider_error";
    err.nextAction = "rotate_proxy";
    throw err;
  }
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

// reCAPTCHA token injection script — runs in MAIN world via executeScript.
// Self-contained (no closure refs); data passed via args = [token, evtRcp].
// Used by both inject-recaptcha-token and solve-and-inject-recaptcha handlers.
function _recaptchaInjectFunc(token, evtRcp) {
  for (const sel of ["#g-recaptcha-response-1","#g-recaptcha-response"]) {
    const ta = document.querySelector(sel);
    if (!ta) continue;
    const setter = Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype,"value")?.set;
    if (setter) setter.call(ta, token); else ta.value = token;
    ta.dispatchEvent(new Event("input",  {bubbles:true}));
    ta.dispatchEvent(new Event("change", {bubbles:true}));
  }
  // grecaptcha.getResponse / execute overrides removed:
  // Setting these as own properties on the instance is detectable via
  // Object.getOwnPropertyDescriptor(window.grecaptcha.enterprise, 'getResponse') returning
  // a descriptor (real methods are prototype-inherited, so the descriptor is undefined).
  // The textarea write + DFS callback walk below is sufficient to complete the solve.
  try {
    const _tok = token;
    const seen = new WeakSet();
    function _findAndCallCb(obj, depth) {
      if (!obj || depth > 8 || typeof obj !== "object" || seen.has(obj)) return;
      seen.add(obj);
      if (typeof obj.callback === "function") { try { obj.callback(_tok); } catch(_) {} }
      // Enterprise may store data-callback as a string name rather than a resolved function ref
      if (typeof obj.callback === "string" && typeof window[obj.callback] === "function") {
        try { window[obj.callback](_tok); } catch(_) {}
      }
      for (const v of Object.values(obj)) _findAndCallCb(v, depth + 1);
    }
    for (const client of Object.values(window.___grecaptcha_cfg?.clients ?? {})) _findAndCallCb(client, 0);
    // Direct fallback: fire onCaptchaSuccess if calendarDiv is still hidden.
    // Randomize the delay (1.5–3s) — a fixed 400ms is a machine-regular timing signature.
    var _cbDelay = 1500 + Math.floor(Math.random() * 1500);
    setTimeout(function() {
      try {
        const cal = document.getElementById("calendarDiv");
        const alreadyVisible = cal && cal.style.display !== "none" && cal.style.visibility !== "hidden" && cal.offsetParent !== null;
        if (!alreadyVisible && typeof window.onCaptchaSuccess === "function") {
          window.onCaptchaSuccess(_tok);
        }
      } catch(_) {}
    }, _cbDelay);
  } catch(_) {}
  try {
    const _anchor = document.querySelector("iframe[src*='recaptcha/api2/anchor'], iframe[src*='recaptcha/enterprise/anchor']");
    if (_anchor?.contentWindow) {
      for (const _m of [JSON.stringify({token}), JSON.stringify({type:"verify",token}), JSON.stringify({source:"recaptcha",type:"solution",token})]) {
        try { _anchor.contentWindow.postMessage(_m, "*"); } catch(_) {}
      }
    }
  } catch(_) {}
  document.dispatchEvent(new CustomEvent(evtRcp, {detail:{token}}));
}

// Unified solver dispatch — races all solvers in parallel; first token wins.
async function _doSolveRecaptcha(pageUrl, siteKey, action) {
  return _raceSolvers(pageUrl, siteKey, action);
}

// ---------------------------------------------------------------------------
// Tab helper + page-ready signaling
// ---------------------------------------------------------------------------

// Tear down shared workflow state before starting a new session.
// Each session is responsible for closing its OWN tab in a finally{} block.
// The stray-tab sweep was removed — it killed concurrent sessions' tabs.
async function _resetWorkflow() {
  stopF6();
  clearInterval(_pollInterval);
  _pollInterval = null;
  _activeTabId = null;

  await chrome.storage.local.remove([
    "workflow-type", "workflow-step",
    "register-person", "register-email",
    "email-token", "email-code-token", "emailPoll",
    "login-pending", "pending-account",
    "active-tab-id", "register-retried",
    "warmup-idle-state", "warmup-tab-id",
    "challenge-count",
  ]);
}

// Kept for SW-restart survival: if the worker restarts mid-session, _activeTabId is
// reloaded from storage and the single-tab popup flow still works.
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

// Creates a dedicated background tab for one concurrent session.
// visibilityState is patched by antidetect.js to always return "visible",
// and CDP mouse events (reCAPTCHA click) work on background tabs.
async function _createSessionTab() {
  const tab = await chrome.tabs.create({url: TARGET_URL, active: false});
  const tabId = tab.id;
  _activeTabId = tabId; // keep for abort/proxy-check only
  await chrome.storage.local.set({"active-tab-id": tabId});
  return tabId;
}

// Per-tab page-ready resolvers — replaces the three scalar globals that only allowed
// one concurrent waitForPageReady at a time.
const _pageReadyResolvers = new Map(); // tabId → {resolve, reject, timer}

function waitForPageReady(tabId, timeoutMs = 30000) {
  return new Promise((resolve, reject) => {
    const prev = _pageReadyResolvers.get(tabId);
    if (prev?.timer) clearTimeout(prev.timer);
    const timer = setTimeout(() => {
      _pageReadyResolvers.delete(tabId);
      const err = new Error("page-ready timeout");
      err.proxyStatus = "proxy_slow";
      err.nextAction  = "rotate_proxy";
      reject(err);
    }, timeoutMs);
    _pageReadyResolvers.set(tabId, {
      resolve: (data) => { clearTimeout(timer); _pageReadyResolvers.delete(tabId); resolve(data); },
      reject:  (err)  => { clearTimeout(timer); _pageReadyResolvers.delete(tabId); reject(err);  },
      timer,
    });
  });
}

// tabId is now an explicit first parameter — no global lookup.
// This makes concurrent sessions safe: each session carries its own tabId.
async function sendTabCmd(tabId, type, params = {}) {
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

    // Notify the session's content script — prefer codeToken (UUID) over linkToken (hex).
    // emailPoll.tabId was stored when _startEmailPoll() was called — routes to the correct session.
    const _pollTabId = emailPoll.tabId ?? _activeTabId;
    if (_pollTabId) chrome.tabs.sendMessage(_pollTabId, {type: "email-token", token: codeToken ?? linkToken}).catch(() => {});
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

function _startEmailPoll(jwt, tabId) {
  chrome.storage.local.set({emailPoll: {jwt, tabId}});
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

// ---------------------------------------------------------------------------
// Service-worker keepalive
// ---------------------------------------------------------------------------
// Chrome MV3 terminates idle service workers after a few seconds of inactivity.
// setInterval / storage calls inside the SW do NOT prevent termination — Chrome
// only tracks active *event handler* execution as "work".
//
// The reliable fix: content.js opens a chrome.runtime.connect port ("sw-keepalive")
// on every tab it injects into (<all_urls>).  As long as any such port is open
// the SW is kept alive by Chrome's port-tracking infrastructure.  The worker-init
// tab stays open throughout the entire workflow, guaranteeing coverage.
//
// _startSwKeepalive / _stopSwKeepalive are kept as no-ops so call sites are clean.

const _keepalivePorts = new Set();
chrome.runtime.onConnect.addListener(port => {
  if (port.name !== "sw-keepalive") return;
  _keepalivePorts.add(port);
  port.onDisconnect.addListener(() => _keepalivePorts.delete(port));
});

function _startSwKeepalive() {}
function _stopSwKeepalive()  {}

// Minimal pre-session warmup — visits 2 real sites before the target.
// Builds a browsing history entry so reCAPTCHA Enterprise sees a non-fresh session.
// Sites are Google-affiliated (high trust) and topic-relevant (visa search).
// Warmup URL pool — pick 2 random sites each session.
// Requirements: no Google affiliation, publicly accessible, high Alexa rank,
// loads quickly, does not run reCAPTCHA itself.
const _WARMUP_POOL = [
  "https://en.wikipedia.org/wiki/Visa_policy_of_Portugal",
  "https://en.wikipedia.org/wiki/Schengen_Area",
  "https://en.wikipedia.org/wiki/Portugal",
  "https://www.bbc.com/news/world-europe",
  "https://www.bbc.com/travel/article/20200127-visiting-portugal",
  "https://www.reuters.com/world/europe/",
  "https://www.theguardian.com/world/europe-news",
  "https://en.wikipedia.org/wiki/Visa_requirements_for_Cape_Verdean_citizens",
];

function _pickWarmupUrls() {
  const pool = _WARMUP_POOL.slice();
  const a = Math.floor(Math.random() * pool.length);
  pool.splice(a, 1);
  const b = Math.floor(Math.random() * pool.length);
  return [_WARMUP_POOL[a], pool[b]];
}

async function F0_warmupBrowse(tabId) {
  for (const url of _pickWarmupUrls()) {
    await chrome.tabs.update(tabId, {url});
    // Poll until the tab finishes loading (page-ready won't fire for non-target pages)
    await new Promise(resolve => {
      const check = setInterval(async () => {
        const tab = await chrome.tabs.get(tabId).catch(() => null);
        if (!tab || tab.status === "complete") { clearInterval(check); resolve(); }
      }, 400);
      setTimeout(() => { clearInterval(check); resolve(); }, 12000);
    });
    // Dwell — let the page accumulate behavioral signals
    await new Promise(r => setTimeout(r, 7000 + Math.random() * 4000));
  }
  _runLog.entry("F0: warmup browse done");
}

async function F1_openAuthPage(tabId) {
  let state;
  try { ({state} = await sendTabCmd(tabId, "cmd-get-state")); } catch (_) { state = "unknown"; }

  if (state === "auth") return {ok: true, status: "ready"};

  if (state !== "not-logged-in") {
    // Set up listener BEFORE navigation so page-ready can't arrive before we're listening.
    // 120s: each bd.js challenge dwell is 20s; 2 challenges + redirects + page loads ≈ 60–90s.
    // The old 35s was too tight — bd1623aa showed 2 challenges consuming ~34s on a fast session.
    const pageReady = waitForPageReady(tabId, 120000);
    await chrome.tabs.update(tabId, {url: TARGET_URL});
    ({state} = await pageReady);
  }

  if (state === "waf-challenge") {
    // content.js already waited 20s for bd.js to auto-redirect — it didn't.
    const e = new Error("F1: WAF bot-challenge not resolved — IP or fingerprint flagged");
    e.proxyStatus = "blocked"; e.nextAction = "rotate_proxy"; throw e;
  }

  // waf-challenge-active = challenge is in progress; auto-redirect may still happen.
  // Re-arm a fresh long wait so we don't miss the redirect completing.
  if (state === "waf-challenge-active") {
    const pr2 = waitForPageReady(tabId, 120000);
    state = (await pr2.catch(() => ({state: "timeout"}))).state ?? "timeout";
    if (state === "waf-challenge") {
      const e = new Error("F1: WAF bot-challenge not resolved after extended wait");
      e.proxyStatus = "blocked"; e.nextAction = "rotate_proxy"; throw e;
    }
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
      try { ({state} = await sendTabCmd(tabId, "cmd-get-state")); } catch (_) { state = "unknown"; }
      if (state === "auth") return {ok: true, status: "ready"};
      // Don't re-navigate if we're off-site (SSO in progress) — just re-check state.
      if (state !== "not-logged-in" && state !== "off-site") {
        const pr = waitForPageReady(tabId, 120000);
        await chrome.tabs.update(tabId, {url: TARGET_URL});
        ({state} = await pr);
        if (state === "auth") return {ok: true, status: "ready"};
        if (state === "waf-challenge") {
          const e = new Error("F1: WAF bot-challenge — IP or fingerprint flagged");
          e.proxyStatus = "blocked"; e.nextAction = "rotate_proxy"; throw e;
        }
        if (state === "waf-challenge-active") {
          // bd.js is running — wait for its auto-redirect
          const prWaf = waitForPageReady(tabId, 120000);
          state = (await prWaf.catch(() => ({state: "timeout"}))).state ?? "timeout";
          if (state === "auth") return {ok: true, status: "ready"};
          if (state === "waf-challenge") {
            const e = new Error("F1: WAF bot-challenge — IP or fingerprint flagged");
            e.proxyStatus = "blocked"; e.nextAction = "rotate_proxy"; throw e;
          }
        }
      }
    }

    // Language switch may reload the page — 15 s gives ample room past the reload time.
    // Skipped entirely when lang-switch-enabled is false (default) to avoid the 15 s wait.
    const {"lang-switch-enabled": _langEnabled = false} =
      await chrome.storage.local.get("lang-switch-enabled");
    if (_langEnabled) {
      const langCmd = sendTabCmd(tabId, "cmd-switch-lang").catch(() => {});
      const langReady = waitForPageReady(tabId, 15000);
      await langCmd;
      await langReady.catch(() => {}); // ignore timeout (already English) or navigation
    }

    // Confirm still on not-logged-in before arming the login listener.
    try { ({state} = await sendTabCmd(tabId, "cmd-get-state")); } catch (_) { state = "unknown"; }
    if (state === "auth") return {ok: true, status: "ready"};

    // Login-link click always navigates — arm listener first.
    // If the site uses SSO (off-site auth), the click navigates to another domain and
    // page-ready never fires.  In that case wait longer for the SSO to redirect back.
    const authReady = waitForPageReady(tabId, 20000);
    await sendTabCmd(tabId, "cmd-click-login-link").catch(() => {});
    let authState;
    try {
      ({state: authState} = await authReady);
    } catch (_) {
      authState = "timeout";
    }
    if (authState === "auth") return {ok: true, status: "ready"};

    if (authState === "off-site" || authState === "timeout" || authState === "waf-challenge-active") {
      // Login navigated off-site (SSO), timed out, or hit a bd.js challenge on the auth page.
      // Wait for the redirect to complete (SSO back or bd.js auto-resolve).
      const label = authState === "waf-challenge-active" ? "WAF challenge on auth page" : authState;
      _runLog.entry(`F1: login led to ${label} — waiting for redirect (120s)`);
      try {
        const ssoReady = waitForPageReady(tabId, 120000);
        ({state: authState} = await ssoReady);
        if (authState === "auth") return {ok: true, status: "ready"};
        if (authState === "not-logged-in") {
          state = authState; // let the retry loop click login again
          continue;
        }
        if (authState === "waf-challenge") {
          const _e = new Error("F1: WAF bot-challenge — IP or fingerprint flagged");
          _e.proxyStatus = "blocked"; _e.nextAction = "rotate_proxy"; throw _e;
        }
      } catch (_) {
        authState = "sso-timeout";
      }
    }

    state = authState;
    _runLog.entry(`F1: attempt ${attempt + 1} got state=${state} — retrying`);
  }
  const _f1e = new Error(`F1: expected auth, got ${state}`);
  _f1e.proxyStatus = "unknown"; _f1e.nextAction = "rotate_proxy"; throw _f1e;
}

async function F2_register(tabId, person, emailAcct) {
  _runLog.entry(`F2: person=${person.name} ${person.surname} email=${emailAcct.email}`);

  // Pre-arm page-ready BEFORE the link click. If the click causes a full navigation
  // the old context dies (channel close) and this listener catches the new page signal
  // without a gap.
  const regPageReady = waitForPageReady(tabId, 90000);
  regPageReady.catch(() => {});

  let openResult;
  try {
    openResult = await sendTabCmd(tabId, "cmd-register-open-form");
  } catch (e) {
    if (!String(e).includes("message channel closed")) throw e;
    // Full-page navigation: wait for new content.js to fire page-ready, then re-send.
    // _registerOpenForm sees #formReg already present and skips the link-click, going
    // straight to waiting for #name.
    const {state} = await regPageReady.catch(() => ({state: "timeout"}));
    _runLog.entry(`F2: form page navigated — new page state=${state}`);
    openResult = await sendTabCmd(tabId, "cmd-register-open-form");
  }
  if (!openResult?.ok) {
    const status = openResult?.status ?? "form_open_failed";
    const e = new Error(`F2: ${status}`);
    e.proxyStatus = "proxy_slow"; e.nextAction = "rotate_proxy"; throw e;
  }
  _runLog.entry("F2: form opened");

  const fillResult = await sendTabCmd(tabId, "cmd-register-fill", {person, email: emailAcct.email});
  if (fillResult?.status === "form_incomplete") {
    const e = new Error("F2: form_incomplete — fill guard triggered (fields missing after open)");
    e.proxyStatus = "proxy_slow"; e.nextAction = "rotate_proxy"; throw e;
  }
  _runLog.entry("F2: form filled");

  // Check whether this proxy IP is already burned before spending captcha quota.
  // Entries older than 24h are pruned (server block is incremental, not permanent).
  let _proxyIp = null;
  try {
    const _ipRes = await sendTabCmd(tabId, "cmd-log-ip");
    _proxyIp = _ipRes?.ip ?? null;
  } catch (_) {}
  if (_proxyIp) {
    const {"burned-proxies": _burned = []} = await chrome.storage.local.get("burned-proxies");
    const _now = Date.now();
    const _active = _burned.filter(e => (_now - e.burnedAt) < 86400000);
    if (_active.length !== _burned.length) await chrome.storage.local.set({"burned-proxies": _active});
    if (_active.some(e => e.ip === _proxyIp)) {
      const _be = new Error(`F2: proxy ${_proxyIp} already burned — rotate`);
      _be.proxyStatus = "burned"; _be.nextAction = "rotate_proxy"; throw _be;
    }
    _runLog.entry(`F2: proxy ${_proxyIp} — not burned, proceeding`);
  }

  // Arm BEFORE the submit loop — the server redirect to the token page can arrive
  // within milliseconds of the RGPD submit, before the loop exits and we could call
  // waitForPageReady.  If we armed it after, the page-ready signal would be lost.
  // 600s budget: timer starts here (before submit) to avoid the race where the server
  // redirects before we arm the listener.  Pre-captcha dwell (≤40s) + two captcha
  // attempts (≤140s) consume up to ~180s before submit, leaving ≥420s for the redirect.
  const tokenPageReady = waitForPageReady(tabId, 600000);
  // Suppress unhandled rejection if we throw before reaching `await tokenPageReady`
  // (e.g. ip_blocked, captcha_fail, username_taken).  The 600s timer will fire and
  // reject the promise; without this, that becomes an unhandled SW rejection.
  tokenPageReady.catch(() => {});

  // 2-attempt retry is handled inside cmd-register-submit (_registerSubmit).
  _runLog.entry("F2: captcha submit (up to 2 attempts internally)");
  const result = await sendTabCmd(tabId, "cmd-register-submit").catch(() => ({ok: true, status: "navigated"}));
  _runLog.entry(`F2: submit result → ${result.status}`);
  if (result.status === "ip_blocked") {
    const e = new Error("F2: IP blocked by captcha rate-limit");
    e.proxyStatus = "blocked"; e.nextAction = "rotate_proxy"; throw e;
  }
  if (result.status === "username_taken") {
    // Account already exists — safety net for the username randomization in F_allInOne.
    // nextAction: "rotate_proxy" lets the manager retry; F_allInOne will re-randomize
    // the suffix on the next call so the new attempt uses a fresh username.
    const e = new Error("F2: username already taken (collision on retry)");
    e.proxyStatus = "username_collision"; e.nextAction = "rotate_proxy"; throw e;
  }
  if (result.status === "email_taken") {
    // Email already registered — happens when retry reuses an email from a prior attempt.
    const e = new Error("F2: email already registered");
    e.proxyStatus = "email_taken"; e.nextAction = "rotate_proxy"; throw e;
  }
  if (result.status === "captcha_fail") {
    if (_proxyIp) {
      const {"burned-proxies": _bl = []} = await chrome.storage.local.get("burned-proxies");
      const _now = Date.now();
      const _al = _bl.filter(e => (_now - e.burnedAt) < 86400000);
      if (!_al.some(e => e.ip === _proxyIp)) {
        _al.push({ip: _proxyIp, burnedAt: _now});
        await chrome.storage.local.set({"burned-proxies": _al});
        _runLog.entry(`F2: proxy ${_proxyIp} marked as burned`);
      }
    }
    const e = new Error("F2: register failed: captcha_fail");
    e.proxyStatus = "burned"; e.nextAction = "rotate_proxy"; throw e;
  }
  if (result.status !== "navigated" && result.status !== "submitted") {
    const e = new Error(`F2: register failed: ${result.status}`);
    e.proxyStatus = "unknown"; e.nextAction = "rotate_proxy"; throw e;
  }

  _runLog.entry("F2: waiting for token page redirect + email code");
  _startEmailPoll(emailAcct.jwt, tabId);
  await tokenPageReady; // wait for token page navigation
  _runLog.entry("F2: token page reached — polling email");

  const codeToken = await _waitForEmailCodeToken(120000);
  if (!codeToken) {
    const e = new Error("F2: email code token timeout");
    e.proxyStatus = "proxy_slow"; e.nextAction = "rotate_proxy"; throw e;
  }
  _runLog.entry(`F2: email code token received: ${codeToken}`);

  // Write pending-account BEFORE submit click — page navigates on success,
  // killing the content script; main() on the next page picks this up and saves the CSV.
  const {botId: _sessionId} = await chrome.storage.local.get("botId");
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
      sessionId:     _sessionId ?? null,
    }
  });

  await sendTabCmd(tabId, "cmd-token-fill", {token: codeToken});
  _runLog.entry("F2: submitting email verification token");
  const verifyReady = waitForPageReady(tabId, 30000); // arm before submit navigates
  // port-closed error = page navigated = success; {ok:false} = error alert = failure
  const tokenResult = await sendTabCmd(tabId, "cmd-token-submit")
    .catch(() => ({ok: true, status: "navigated"}));
  const {state: finalState} = await verifyReady;
  _runLog.entry(`F2: verification done → result=${tokenResult.ok ? "success" : `failed(${tokenResult.status})`} state=${finalState}`);

  if (!tokenResult.ok) {
    await chrome.storage.local.remove("pending-account");
    const _f2e = new Error(`F2: email verification failed: ${tokenResult.status}${tokenResult.alert ? ` — ${tokenResult.alert}` : ""}`);
    _f2e.proxyStatus = "unknown"; _f2e.nextAction = "rotate_proxy"; throw _f2e;
  }

  // Belt-and-suspenders: save directly from background in case the content.js
  // save-account round-trip failed (navigation timing, WS disconnect, etc).
  // Deduplication by username prevents double entries when content.js also saved.
  try {
    const {accounts: _accs} = await chrome.storage.local.get("accounts");
    const _list = Array.isArray(_accs) ? _accs : [];
    if (!_list.some(a => a.username === person.username)) {
      _list.push({
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
      });
      await chrome.storage.local.set({accounts: _list});
      _runLog.entry(`F2: credentials saved to accounts list (username=${person.username})`);
    } else {
      _runLog.entry(`F2: credentials already in accounts list (username=${person.username})`);
    }
  } catch(e) {
    _runLog.entry(`F2: accounts save error: ${e.message}`);
  }

  return {ok: true, status: finalState === "auth" ? "verified" : finalState};
}

async function F3_login(tabId, creds) {
  let state;
  try { ({state} = await sendTabCmd(tabId, "cmd-get-state")); } catch (_) { state = "unknown"; }
  if (state === "logged-in") return {ok: true, status: "already-logged-in"};

  await sendTabCmd(tabId, "cmd-login-fill", creds);

  for (let attempt = 0; attempt < 3; attempt++) {
    // Port-closed error = tab navigated mid-command; wait briefly for new page-ready.
    // Normal "navigated" return = _loginSubmit already waited 5s + session probe,
    // new page is fully loaded — no additional wait needed.
    const result = await sendTabCmd(tabId, "cmd-login-submit").catch(async () => {
      await waitForPageReady(tabId, 15000).catch(() => {});
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

async function F4_formFilling(tabId) {
  // Arm each listener BEFORE the command that triggers navigation.
  let pageReady = waitForPageReady(tabId, 30000);
  await sendTabCmd(tabId, "cmd-go-questionnaire").catch(() => {});
  const {state: qState} = await pageReady;
  if (qState !== "questionnaire") {
    const e = new Error(`F4: expected questionnaire, got ${qState}`);
    e.proxyStatus = "unknown"; e.nextAction = "rotate_proxy"; throw e;
  }

  let fState;
  for (let attempt = 0; attempt < 2; attempt++) {
    pageReady = waitForPageReady(tabId, 150000);
    await sendTabCmd(tabId, "cmd-fill-questionnaire").catch(() => {});
    ({state: fState} = await pageReady);
    if (fState === "form") break;
    if (fState === "auth") break;  // server-side session expiry — handle below
    if (fState !== "questionnaire") {
      const e = new Error(`F4: expected form, got ${fState}`);
      e.proxyStatus = "unknown"; e.nextAction = "rotate_proxy"; throw e;
    }
    // questionnaire page reloaded — retry
  }

  if (fState === "auth") {
    // Server redirected to auth mid-questionnaire (session expired within ~24s).
    // Re-login once using stored credentials and retry questionnaire.
    const {"register-person": creds} = await chrome.storage.local.get("register-person");
    if (!creds?.username || !creds?.password) {
      const e = new Error("F4: session expired, no stored credentials for re-login");
      e.proxyStatus = "unknown"; e.nextAction = "change_device"; throw e;
    }
    const _reloginRes = await F3_login(tabId, {username: creds.username, password: creds.password});
    if (!_reloginRes.ok) {
      const e = new Error(`F4: re-login failed — status=${_reloginRes.status}`);
      e.proxyStatus = "login_rejected"; e.nextAction = "rotate_proxy"; throw e;
    }
    pageReady = waitForPageReady(tabId, 30000);
    await sendTabCmd(tabId, "cmd-go-questionnaire").catch(() => {});
    const {state: qState2} = await pageReady;
    if (qState2 !== "questionnaire") {
      const e = new Error(`F4: after re-login, expected questionnaire, got ${qState2}`);
      e.proxyStatus = "unknown"; e.nextAction = "rotate_proxy"; throw e;
    }
    pageReady = waitForPageReady(tabId, 150000);
    await sendTabCmd(tabId, "cmd-fill-questionnaire").catch(() => {});
    ({state: fState} = await pageReady);
  }

  if (fState !== "form") {
    const e = new Error(`F4: questionnaire never reached form after retries`);
    e.proxyStatus = "unknown"; e.nextAction = "rotate_proxy"; throw e;
  }

  pageReady = waitForPageReady(tabId, 150000);
  await sendTabCmd(tabId, "cmd-fill-form").catch(() => {});
  const {state: sState} = await pageReady;
  if (sState !== "schedule") {
    const e = new Error(`F4: expected schedule, got ${sState}`);
    e.proxyStatus = "unknown"; e.nextAction = "rotate_proxy"; throw e;
  }

  return {ok: true, status: "form-ready"};
}

async function F5_scheduling(tabId, config) {
  return sendTabCmd(tabId, "cmd-schedule", config).catch(e => ({ok: false, error: e.message}));
}

let _f6Interval       = null;
let _applyTriggerLock = false; // debounce: prevent duplicate apply starts
let _waitingForSignal = false; // EXTERNAL_SIGNAL mode: waiting for signal-apply

function _sendStatusUpdate(state) {
  self.Comm?.send({ type: "status-update", state });
}

function F6_keepSession(tabId) {
  if (_f6Interval) clearTimeout(_f6Interval);
  // Use a truly random interval (60–180s) with no fixed base per idle state.
  // Fixed base values (15/40/90s ± jitter) created a statistically detectable
  // pattern — statistical analysis of request inter-arrival times would flag it.
  function _nextMs() { return 60000 + Math.random() * 120000; } // 1–3 min, uniform
  const _f6Tick = () => {
    sendTabCmd(tabId, "cmd-keep-tick").catch(() => {});
    if (_f6Interval) {
      clearTimeout(_f6Interval);
      _f6Interval = setTimeout(_f6Tick, _nextMs());
    }
  };
  _f6Interval = setTimeout(_f6Tick, _nextMs());
}

function stopF6() {
  if (_f6Interval) { clearTimeout(_f6Interval); _f6Interval = null; }
}

async function F_warmup(config) {
  const idleStep = config.idleStep ?? "login";
  _runLog.start(`warmup_${idleStep}`);
  _runLog.entry(`username=${config.username} idleStep=${idleStep}`);
  _startSwKeepalive();
  stopF6();
  await _resetWorkflow();
  const tabId = await _createSessionTab();
  await chrome.storage.local.set({"warmup-idle-state": idleStep, "warmup-tab-id": tabId});

  try {
    _runLog.entry("F_warmup: opening auth page");
    await F1_openAuthPage(tabId);
    _runLog.entry("F_warmup: logging in");
    const _warmupLoginRes = await F3_login(tabId, {username: config.username, password: config.password});
    if (!_warmupLoginRes.ok) {
      await _runLog.finish(`error: F_warmup: login failed — status=${_warmupLoginRes.status}`);
      // Include nextAction so the manager can retry with a different proxy.
      return {ok: false, status: _warmupLoginRes.status, nextAction: "rotate_proxy"};
    }
    _runLog.entry("F_warmup: logged in");

    if (idleStep === "login") {
      F6_keepSession(tabId, "login");
      await _runLog.finish("ok idleStep=login");
      return {ok: true, idleStep: "login"};
    }

    _runLog.entry("F_warmup: navigating to questionnaire");
    let pageReady = waitForPageReady(tabId, 30000);
    await sendTabCmd(tabId, "cmd-go-questionnaire").catch(() => {});
    const {state: qState} = await pageReady;
    if (qState !== "questionnaire") {
      const e = new Error(`F_warmup: expected questionnaire, got ${qState}`);
      e.proxyStatus = "unknown"; e.nextAction = "rotate_proxy"; throw e;
    }
    _runLog.entry("F_warmup: filling questionnaire");

    pageReady = waitForPageReady(tabId, 60000);
    await sendTabCmd(tabId, "cmd-fill-questionnaire").catch(() => {});
    const {state: fState} = await pageReady;
    if (fState !== "form") {
      const e = new Error(`F_warmup: expected form, got ${fState}`);
      e.proxyStatus = "unknown"; e.nextAction = "rotate_proxy"; throw e;
    }
    _runLog.entry("F_warmup: on form page");

    if (idleStep === "form") {
      await sendTabCmd(tabId, "cmd-fill-form-tabs").catch(() => {});
      F6_keepSession(tabId, "form");
      await _runLog.finish("ok idleStep=form");
      return {ok: true, idleStep: "form"};
    }

    if (idleStep === "form-monitor") {
      await sendTabCmd(tabId, "cmd-fill-form-tabs").catch(() => {});
      await chrome.storage.local.set({
        "trigger-mode":   config.triggerMode  ?? "AUTO_TRIGGER",
        "target-post-id": String(config.targetPostId ?? config.consulPost ?? ""),
      });
      _applyTriggerLock = false;
      _waitingForSignal = false;
      await sendTabCmd(tabId, "cmd-start-post-monitor").catch(() => {});
      F6_keepSession(tabId, "form");
      _sendStatusUpdate("READY_FOR_APPLY_IDLE");
      await _runLog.finish("ok idleStep=form-monitor");
      return {ok: true, idleStep: "form-monitor"};
    }

    if (idleStep === "schedule") {
      _runLog.entry("F_warmup: submitting form → schedule");
      pageReady = waitForPageReady(tabId, 90000);
      await sendTabCmd(tabId, "cmd-fill-form").catch(() => {});
      const {state: sState} = await pageReady;
      if (sState !== "schedule") {
        const e = new Error(`F_warmup: expected schedule, got ${sState}`);
        e.proxyStatus = "unknown"; e.nextAction = "rotate_proxy"; throw e;
      }
      F6_keepSession(tabId, "schedule");
      await _runLog.finish("ok idleStep=schedule");
      return {ok: true, idleStep: "schedule"};
    }

    throw new Error(`F_warmup: unknown idleStep "${idleStep}"`);
  } catch(e) {
    _stopSwKeepalive();
    chrome.tabs.remove(tabId).catch(() => {});
    throw e;
  }
}

async function F_apply() {
  _runLog.start("apply");
  _startSwKeepalive();
  stopF6();
  const {"warmup-idle-state": idleState = "login", "warmup-tab-id": tabId} =
    await chrome.storage.local.get(["warmup-idle-state", "warmup-tab-id"]);
  const _tabId = tabId ?? await _getActiveTab();
  _runLog.entry(`F_apply: idleState=${idleState}`);

  try {
    let r;
    if (idleState === "login") {
      _runLog.entry("F_apply: running F4 form filling");
      await F4_formFilling(_tabId);
      _runLog.entry("F_apply: running F5 scheduling");
      r = await F5_scheduling(_tabId, {});
    } else if (idleState === "form" || idleState === "form-monitor") {
      _runLog.entry("F_apply: submitting form from idle");
      const pageReady = waitForPageReady(_tabId, 30000);
      await sendTabCmd(_tabId, "cmd-submit-form").catch(() => {});
      const {state: sState} = await pageReady;
      if (sState !== "schedule") {
        const e = new Error(`F_apply: expected schedule, got ${sState}`);
        e.proxyStatus = "unknown"; e.nextAction = "rotate_proxy"; throw e;
      }
      _runLog.entry("F_apply: running F5 scheduling");
      r = await F5_scheduling(_tabId, {});
    } else if (idleState === "schedule") {
      _runLog.entry("F_apply: running F5 scheduling from idle");
      r = await F5_scheduling(_tabId, {});
    } else {
      throw new Error(`F_apply: unknown idleState "${idleState}"`);
    }

    await _runLog.finish(r.ok !== false
      ? `ok status=${r.status ?? "ok"}`
      : `error: F5 failed — ${r.error ?? r.status ?? "unknown"}`);
    return r;
  } finally {
    _stopSwKeepalive();
    chrome.tabs.remove(_tabId).catch(() => {});
    await chrome.storage.local.remove(["warmup-idle-state", "warmup-tab-id"]);
  }
}

async function F_allInOne(config) {
  _runLog.start("all_in_one");
  _startSwKeepalive();
  stopF6();
  await _resetWorkflow();

  // Build person data. Use manager-supplied realPerson if provided; fall back to local generator.
  const rp = config.realPerson;
  let person;
  if (rp && (rp.firstName || rp.name)) {
    const gRaw = String(rp.gender ?? "").trim().toUpperCase();
    const firstName = String(rp.firstName ?? rp.name ?? "").trim();
    const lastName  = String(rp.lastName  ?? rp.surname ?? "").trim();
    person = {
      name:        firstName,
      surname:     lastName,
      username:    String(rp.username ?? "").trim() || _genUsername(firstName, lastName),
      password:    String(rp.password ?? "").trim() || _genPassword(),
      birth_date:  _parseDOB(rp.dob ?? rp.birth_date),
      gender:      gRaw === "F" || gRaw === "FEMALE" || gRaw === "FEMININO" ? "F" : "M",
      nationality: String(rp.nationality ?? "CPV").trim(),
      traveldoc:   String(rp.traveldoc  ?? "").trim(),
    };
  } else {
    person = generatePerson();
  }
  const _supplied = config.emailAccount ?? config.emailAcct;
  const emailAcct = _supplied?.email
    ? {
        email: _supplied.email,
        password: _supplied.password ?? "",
        jwt: _supplied.jwt ?? _supplied.email,
      }
    : await createTempEmail().catch(err => {
        err.proxyStatus = err.proxyStatus ?? "email_provider_error";
        err.nextAction  = err.nextAction  ?? "rotate_proxy";
        throw err;
      });
  await chrome.storage.local.set({
    "register-person":  person,
    "register-email":   emailAcct,
    "email-token":      null,
    "email-code-token": null,
  });

  // ── Phase 1: Registration (dedicated tab, warmup → register) ────────────
  _runLog.entry(`all_in_one: person=${person.name} ${person.surname} email=${emailAcct.email}`);
  const regTabId = await _createSessionTab();
  try {
    await F0_warmupBrowse(regTabId);
    await F1_openAuthPage(regTabId);
    await sendTabCmd(regTabId, "cmd-log-ip").catch(() => {});
    await F2_register(regTabId, person, emailAcct);
    _runLog.entry("all_in_one: registration done");
  } finally {
    // Always close registration tab — its session context is spent.
    chrome.tabs.remove(regTabId).catch(() => {});
  }

  // ── Phase 2: Login + F4 + F5 (fresh tab, fresh warmup → fresh session) ──
  // Brief gap lets the server-side account settle before login attempt.
  _runLog.entry("all_in_one: opening fresh session for login");
  await new Promise(r => setTimeout(r, 3000 + Math.random() * 3000));

  const loginTabId = await _createSessionTab();
  try {
    await F0_warmupBrowse(loginTabId);
    await F1_openAuthPage(loginTabId);

    // Retry login up to 3 times on "rejected" — the server may not have activated
    // the account yet (activation lag is typically 5–30s after registration completes).
    // Non-retriable failures (login_failed = wrong credentials) exit immediately.
    let _loginRes;
    for (let _attempt = 0; _attempt < 3; _attempt++) {
      if (_attempt > 0) {
        const waitMs = 30000 + (_attempt - 1) * 30000; // 30 s, 60 s
        _runLog.entry(`all_in_one: login rejected — waiting ${waitMs / 1000}s then hard-reloading (attempt ${_attempt + 1}/3)`);
        await new Promise(r => setTimeout(r, waitMs));
        // Hard reload (Ctrl+F5 equivalent) — clears server-side captcha failure session
        // state and issues a fresh reCAPTCHA widget with a new challenge ID.
        const _freshPage = waitForPageReady(loginTabId, 30000);
        await chrome.tabs.reload(loginTabId, {bypassCache: true});
        await _freshPage.catch(() => {});
      }
      _loginRes = await F3_login(loginTabId, {username: person.username, password: person.password});
      if (_loginRes.ok || _loginRes.status !== "rejected") break;
    }
    if (!_loginRes.ok) {
      const e = new Error(`F3: login failed — status=${_loginRes.status}`);
      e.proxyStatus = "login_rejected";
      e.nextAction  = "rotate_proxy";
      throw e;
    }
    _runLog.entry("all_in_one: logged in — running F4+F5");

    await F4_formFilling(loginTabId);
    const r = await F5_scheduling(loginTabId, {});
    await _runLog.finish(r.ok !== false
      ? `ok status=${r.status ?? "ok"}`
      : `error: F5 failed — ${r.error ?? r.status ?? "unknown"}`);
    return r;
  } finally {
    _stopSwKeepalive();
    chrome.tabs.remove(loginTabId).catch(() => {});
  }
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


function _runRegister(realPerson, emailAccount) {
  (async () => {
    let tabId;
    try {
      _startSwKeepalive();
      stopF6();
      await _resetWorkflow();
      if (!realPerson) throw new Error('_runRegister: realPerson is required');
      _runLog.start("register_real");
      tabId = await _createSessionTab();
      const gRaw = String(realPerson.gender ?? "").trim().toUpperCase();
      const person = {
        name:        String(realPerson.firstName ?? "").trim(),
        surname:     String(realPerson.lastName  ?? "").trim(),
        username:    String(realPerson.username  ?? "").trim(),
        password:    String(realPerson.password  ?? "").trim(),
        birth_date:  _parseDOB(realPerson.dob),
        gender:      gRaw === "F" || gRaw === "FEMALE" || gRaw === "FEMININO" ? "F" : "M",
        nationality: String(realPerson.nationality ?? "").trim(),
        traveldoc:   String(realPerson.traveldoc  ?? "").trim(),
      };
      if (!person.username || !person.password) {
        throw new Error('_runRegister: username and password are required in realPerson');
      }
      _runLog.entry(`person: ${person.name} ${person.surname} ${person.nationality} ${person.traveldoc}`);
      const emailAcct = emailAccount?.email
        ? {
            email: emailAccount.email,
            password: emailAccount.password ?? "",
            jwt: emailAccount.jwt ?? emailAccount.email,
          }
        : await createTempEmail();
      _runLog.entry(`email: ${emailAcct.email}`);
      await chrome.storage.local.set({
        "register-person":  person,
        "register-email":   emailAcct,
        "email-token":      null,
        "email-code-token": null,
      });
      await F1_openAuthPage(tabId);
      await sendTabCmd(tabId, "cmd-log-ip").catch(() => {});
      const r = await F2_register(tabId, person, emailAcct);
      await _runLog.finish(`ok status=${r.status}`);
      self.Comm?.send({type: "register-done", ...r, email: emailAcct.email, username: person.username, password: person.password});
    } catch(e) {
      await _runLog.finish(`error: ${e.message ?? String(e)}`);
      self.Comm?.send({
        type: "error",
        reason: e.message ?? String(e),
        proxyStatus: e.proxyStatus ?? null,
        nextAction: e.nextAction ?? null,
      });
    } finally {
      _stopSwKeepalive();
      if (tabId) chrome.tabs.remove(tabId).catch(() => {});
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

// WS keepalive alarm — wakes the SW periodically so reconnectIfNeeded() can fire
// if the WS dropped while the SW was suspended.
// Hub sends protocol-level WS pings every 5 s (server-side) which already keeps
// the connection alive; this alarm handles the rare case where the SW is killed
// and needs to reconnect from stored credentials.
chrome.alarms.create("ws-ping", {periodInMinutes: 0.4});
chrome.alarms.onAlarm.addListener((alarm) => {
  if (alarm.name !== "ws-ping") return;
  if (self.Comm?.isConnected()) {
    self.Comm.send({type: "ping"});
  } else {
    // SW may have been suspended — WS dropped silently without onclose firing.
    self.Comm?.reconnectIfNeeded();
  }
});

// _withDebugger removed: chrome.debugger.attach creates a measurable timing spike
// in performance.timing that bd.js detects. CDP-based clicks are no longer used.

const _MSG_HANDLERS = {

  "page-ready": (msg, sender, respond) => {
    const _prr = _pageReadyResolvers.get(sender.tab?.id);
    if (_prr) _prr.resolve({state: msg.state, url: msg.url});
    // Restore post monitor after a page reload when in form-monitor idle state
    if (msg.state === "form" && sender.tab?.id) {
      chrome.storage.local.get(["warmup-idle-state"]).then(d => {
        if (d["warmup-idle-state"] === "form-monitor")
          sendTabCmd(sender.tab.id, "cmd-start-post-monitor").catch(() => {});
      });
    }
    respond({ok: true});
  },

  "WORKER_INIT": (msg, _sender, respond) => {
    if (self.Comm?.connectHub) self.Comm.connectHub(msg.botId, msg.hubUrl);
    respond({ok: true});
  },

  "comm-dispatch": (msg, _sender, respond) => {
    if (self.Comm) self.Comm.dispatch(msg.payload).catch(e => console.error("[OctoProbe BG] comm-dispatch error:", e));
    respond({ok: true});
  },

  // ── Slot intelligence relay ───────────────────────────────────────────────
  // content.js → background → manager (fire-and-forget)
  "slot-observation": (msg, _sender, respond) => {
    if (self.Comm) self.Comm.send({ type: "slot-observation", postId: msg.postId, slots: msg.slots, timestamp: Date.now() });
    respond({ok: true});
  },

  // content.js → background → manager → background → content.js (3s timeout)
  "slot-assignment-request": (msg, _sender, respond) => {
    if (!self.Comm) { respond({ok: false}); return; }
    self.Comm.requestSlotAssignment({ postId: msg.postId, visibleSlots: msg.visibleSlots }, 3000)
      .then(assignment => respond({ ok: true, ...assignment }))
      .catch(() => respond({ ok: false }));
    return true; // async response
  },

  // content.js → background → manager (fire-and-forget)
  "slot-failure": (msg, _sender, respond) => {
    if (self.Comm) self.Comm.send({ type: "slot-failure", slotKey: msg.slotKey, reason: msg.reason });
    respond({ok: true});
  },

  "slot-success": (msg, _sender, respond) => {
    if (self.Comm) self.Comm.send({ type: "slot-success", slotKey: msg.slotKey });
    respond({ok: true});
  },

  // ── Dual trigger ─────────────────────────────────────────────────────────
  // Fired by content.js when target post appears in #f0sf1 options.
  "target-post-available": (msg, _sender, respond) => {
    if (_applyTriggerLock) { respond({ok: true}); return; }
    _applyTriggerLock = true;
    stopF6();
    chrome.storage.local.get(["trigger-mode"]).then(d => {
      const triggerMode = d["trigger-mode"] ?? "AUTO_TRIGGER";
      _runLog.entry(`target-post-available: postId=${msg.postId} triggerMode=${triggerMode}`);
      if (triggerMode === "AUTO_TRIGGER") {
        _sendStatusUpdate("AUTO_TRIGGER_PENDING");
        F_apply()
          .then(r => { _sendStatusUpdate("DONE"); self.Comm?.send({ type: "apply-done", ...r }); })
          .catch(_onCommandError);
      } else {
        // EXTERNAL_SIGNAL — notify manager and wait for signal-apply
        _waitingForSignal = true;
        _applyTriggerLock = false;
        self.Comm?.send({ type: "target-post-available", postId: msg.postId });
        _sendStatusUpdate("WAITING_SIGNAL");
      }
    });
    respond({ok: true});
    return true;
  },

  "get-session-keys": (_msg, _sender, respond) => {
    respond({sk: _SK, evtAlert: _EVT_ALERT, evtRcp: _EVT_RCP});
  },

  "ping": (_msg, _sender, respond) => {
    respond({type: "pong", version: chrome.runtime.getManifest().version});
  },

  // Sent by content.js every 20 s to reset Chrome's 30 s SW idle timer.
  // Keeps the SW alive in Chrome 110+ where open ports alone are insufficient.
  "sw-keepalive-ping": (_msg, _sender, respond) => {
    respond({ok: true});
  },

  "get-creds": (_msg, _sender, respond) => {
    chrome.storage.local.get(["username","password"], (d) => {
      respond({username: d.username||"", password: d.password||""});
    });
    return true;
  },

  "save-creds": (msg, _sender, respond) => {
    chrome.storage.local.set({username: msg.username, password: msg.password}, () => {
      respond({ok: true});
    });
    return true;
  },

  "log-entry": (msg, _sender, respond) => {
    _runLog.entry(msg.msg);
    respond({ok: true});
  },

  "run-register": (msg, _sender, respond) => {
    _runRegister(msg.realPerson);
    respond({ok: true});
  },

  "run-F6": (_msg, _sender, respond) => {
    if (_activeTabId) F6_keepSession(_activeTabId);
    respond({ok: !!_activeTabId});
  },

  "start-email-poll": (msg, sender, respond) => {
    _startEmailPoll(msg.jwt, sender.tab?.id);
    respond({ok: true});
  },

  "stop-email-poll": (_msg, _sender, respond) => {
    clearInterval(_pollInterval);
    _pollInterval = null;
    chrome.storage.local.remove("emailPoll");
    respond({ok: true});
  },

  "sleep": (msg, _sender, respond) => {
    setTimeout(() => respond({ok: true}), msg.ms);
    return true;
  },

  "dispatch-proxy-check": (_msg, _sender, respond) => {
    _runCheckProxy();
    respond({ok: !!_activeTabId});
  },

  "dispatch-abort": (_msg, _sender, respond) => {
    _runDispatchAbort();
    respond({ok: true});
  },

  "save-account": (msg, _sender, respond) => {
    const account = msg.account;
    const ts = new Date().toISOString().replace(/[:.]/g, "-").slice(0, 19);
    // Include sessionId prefix for traceability when multiple sessions run concurrently
    const _sid = msg.sessionId ?? account.sessionId ?? null;
    const filename = _sid ? `account_${_sid.slice(0, 8)}_${ts}.csv` : `account_${ts}.csv`;
    (async () => {
      try {
        const {accounts} = await chrome.storage.local.get("accounts");
        const list = Array.isArray(accounts) ? accounts : [];
        if (!list.some(a => a.username === account.username)) {
          list.push(account);
          await chrome.storage.local.set({accounts: list});
        }
        const headers = Object.keys(account);
        const values  = headers.map(k => `"${String(account[k]).replace(/"/g, '""')}"`);
        const csv     = headers.join(",") + "\n" + values.join(",") + "\n";
        const dataUrl = "data:text/csv;charset=utf-8," + encodeURIComponent(csv);
        chrome.downloads.download({url: dataUrl, filename, saveAs: false}, (id) => {
          console.log(`[OctoProbe BG] Account saved → ${filename} (download id ${id})`);
        });
        respond({ok: true, filename});
      } catch(e) {
        console.error("[OctoProbe BG] save-account error:", e);
        respond({ok: false, error: String(e)});
      }
    })();
    return true;
  },

  "close-tab": (_msg, sender, respond) => {
    const tabId = sender.tab?.id;
    if (tabId) chrome.tabs.remove(tabId);
    respond({ok: true});
  },

  "clear-workflow": (_msg, _sender, respond) => {
    chrome.storage.local.remove([
      "workflow-type","workflow-step","register-person","register-email","email-token","email-code-token","emailPoll","warmup-idle-state"
    ]);
    respond({ok: true});
  },

  "inject-alert-capture": (msg, sender, respond) => {
    const tabId = sender.tab?.id;
    if (!tabId) { respond({ok: false, error: "no sender tab"}); return; }
    chrome.scripting.executeScript({
      target: {tabId},
      world: "MAIN",
      args: [!!msg.confirmToo, _SK, _EVT_ALERT],
      func: (confirmToo, sk, evtAlert) => {
        function _native(fn, name) {
          // Use non-enumerable, non-configurable toString so getOwnPropertyDescriptor(fn,'toString')
          // returns undefined — matching real native behaviour.
          try {
            Object.defineProperty(fn, 'toString', {
              value: function() { return 'function ' + name + '() { [native code] }'; },
              writable: false, enumerable: false, configurable: false,
            });
            Object.defineProperty(fn, 'name', {value: name, writable: false, enumerable: false, configurable: true});
          } catch(_) {}
          return fn;
        }
        const _hookedKey  = '_' + sk + 'h';
        const _alertKey   = '_' + sk + 'a';
        const _cHookedKey = '_' + sk + 'ch';
        const _cKey       = '_' + sk + 'c';
        if (!window[_hookedKey]) {
          // Store the hook flag as a non-enumerable property — won't appear in Object.keys(window)
          // or Object.getOwnPropertyNames(window), reducing fingerprint surface.
          try { Object.defineProperty(window, _hookedKey, {value: true, writable: false, enumerable: false, configurable: false}); } catch(_) { window[_hookedKey] = true; }
          window.alert = _native(function alert(m) {
            try { Object.defineProperty(window, _alertKey, {value: String(m), writable: true, enumerable: false, configurable: true}); } catch(_) { window[_alertKey] = String(m); }
            document.dispatchEvent(new CustomEvent(evtAlert, {detail: {msg: String(m)}}));
          }, 'alert');
        }
        if (confirmToo && !window[_cHookedKey]) {
          try { Object.defineProperty(window, _cHookedKey, {value: true, writable: false, enumerable: false, configurable: false}); } catch(_) { window[_cHookedKey] = true; }
          window.confirm = _native(function confirm(m) {
            try { Object.defineProperty(window, _cKey, {value: String(m), writable: true, enumerable: false, configurable: true}); } catch(_) { window[_cKey] = String(m); }
            document.dispatchEvent(new CustomEvent(evtAlert, {detail: {msg: String(m)}}));
            return true;
          }, 'confirm');
          window.prompt = _native(function prompt(_m, def) { return def ?? ''; }, 'prompt');
        }
      },
    })
    .then(() => respond({ok: true}))
    .catch(e => respond({ok: false, error: String(e)}));
    return true;
  },

  "reset-recaptcha": (_msg, sender, respond) => {
    const tabId = sender.tab?.id;
    if (!tabId) { respond({ok: false, error: "no sender tab"}); return; }
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
      },
    })
    .then(() => respond({ok: true}))
    .catch(e => respond({ok: false, error: String(e)}));
    return true;
  },

  "fill-token": (msg, sender, respond) => {
    const tabId = sender.tab?.id;
    const {token} = msg;
    if (!tabId) { respond({ok: false, error: "no sender tab"}); return; }
    chrome.scripting.executeScript({
      target: {tabId}, world: "MAIN", args: [token],
      func: async (token) => {
        const sels = [
          "input[name='tokenInput']",
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
        function _gr() { let u,v; do{u=Math.random();}while(!u); do{v=Math.random();}while(!v); return Math.sqrt(-2*Math.log(u))*Math.cos(2*Math.PI*v); }
        for (const ch of token) {
          const delay = Math.max(25, Math.round(Math.exp(Math.log(95) + 0.45 * _gr())));
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
        return {ok: true, value: el.value};
      },
    })
    .then(([r]) => respond(r?.result ?? {ok: false}))
    .catch(e => respond({ok: false, error: String(e)}));
    return true;
  },

  "solve-recaptcha-api": (msg, _sender, respond) => {
    const {pageUrl, siteKey, action = null} = msg;
    _doSolveRecaptcha(pageUrl, siteKey, action)
      .then(token => respond({ok: true, token}))
      .catch(e   => respond({ok: false, error: String(e)}));
    return true;
  },

  // Combines solve + inject into one round-trip. Background solves, pads to minSolveMs,
  // injects the token via executeScript (which fires the _evtRcp event in MAIN world),
  // then responds. Content.js drifts the mouse concurrently while awaiting this response.
  "solve-and-inject-recaptcha": (msg, sender, respond) => {
    const tabId = sender.tab?.id;
    if (!tabId) { respond({ok: false, error: "no sender tab"}); return; }
    const {pageUrl, siteKey, action = null, minSolveMs = 12000} = msg;
    (async () => {
      try {
        const solveStart = Date.now();
        const token = await _doSolveRecaptcha(pageUrl, siteKey, action);
        const elapsed = Date.now() - solveStart;
        if (elapsed < minSolveMs) await new Promise(r => setTimeout(r, minSolveMs - elapsed));
        const totalMs = Date.now() - solveStart;
        console.log(`[OctoProbe BG] Token obtained after ${totalMs}ms — injecting`);
        await chrome.scripting.executeScript({
          target: {tabId}, world: "MAIN", args: [token, _EVT_RCP],
          func: _recaptchaInjectFunc,
        });
        respond({ok: true, totalMs});
      } catch(e) {
        respond({ok: false, error: String(e)});
      }
    })();
    return true;
  },

  "inject-recaptcha-token": (msg, sender, respond) => {
    const tabId = sender.tab?.id;
    const {token} = msg;
    if (!tabId) { respond({ok: false, error: "no sender tab"}); return; }
    chrome.scripting.executeScript({
      target: {tabId}, world: "MAIN", args: [token, _EVT_RCP],
      func: _recaptchaInjectFunc,
    })
    .then(() => respond({ok: true}))
    .catch(e => respond({ok: false, error: String(e)}));
    return true;
  },

  // cdp-click removed: chrome.debugger.attach creates a detectable latency spike
  // in performance.timing. CDP mouse events are no longer used for reCAPTCHA.

  "get-recaptcha-sitekey": (_msg, sender, respond) => {
    const tabId = sender.tab?.id;
    if (!tabId) { respond({ok: false, siteKey: null}); return; }
    chrome.scripting.executeScript({
      target: {tabId}, world: "MAIN",
      func: () => {
        const el = document.querySelector("[data-sitekey]");
        if (el?.dataset?.sitekey) return el.dataset.sitekey;
        const fr = document.querySelector("iframe[src*='recaptcha']");
        if (fr) { const m = fr.src.match(/[?&]k=([A-Za-z0-9_-]+)/); if (m) return m[1]; }
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
    .then(([r]) => respond({ok: true, siteKey: r?.result ?? null}))
    .catch(() => respond({ok: false, siteKey: null}));
    return true;
  },

  "exec-page-script": (msg, sender, respond) => {
    const tabId = sender.tab?.id;
    if (!tabId) { respond({ok: false, error: "no sender tab"}); return; }
    const code = msg.code ?? "";
    chrome.scripting.executeScript({
      target: {tabId}, world: "MAIN", args: [code],
      func: (c) => { try { return (new Function(c))(); } catch(e) { return {error: String(e)}; } },
    })
    .then(([r]) => respond({ok: true, result: r?.result ?? null}))
    .catch(e => respond({ok: false, error: String(e)}));
    return true;
  },

  "ws-send": (msg, _sender, respond) => {
    if (self.Comm) self.Comm.send(msg.data);
    respond({ok: true});
  },

};

chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  const h = _MSG_HANDLERS[msg.type];
  if (h) return h(msg, sender, sendResponse) ?? false;
  return false;
});
