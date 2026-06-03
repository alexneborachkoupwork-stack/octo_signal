// Service worker — credential storage, workflow dispatch, mail.tm email polling.

importScripts('constants.js');
importScripts('communication.js');

// Load static config into storage so CF email settings are available without popup interaction.
async function _loadExtensionConfig() {
  try {
    const cfg = await fetch(chrome.runtime.getURL("config.json")).then(r => {
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      return r.json();
    });
    const update = {};
    if (cfg.cfMailDomain)    update[SK.CF_MAIL_DOMAIN]     = cfg.cfMailDomain;
    if (cfg.cfWorkerUrl)     update[SK.CF_WORKER_URL]      = cfg.cfWorkerUrl;
    if (cfg.cfWorkerSecret)  update[SK.CF_WORKER_SECRET]   = cfg.cfWorkerSecret;
    if (cfg.anticaptchaKeys) update[SK.ANTICAPTCHA_KEYS]   = cfg.anticaptchaKeys;
    if (cfg.twocaptchaKeys)  update[SK.TWOCAPTCHA_KEYS]    = cfg.twocaptchaKeys;
    if (cfg.capmonsterKeys)  update[SK.CAPMONSTER_KEYS]    = cfg.capmonsterKeys;
    if (cfg.capsolverKey)    update[SK.CAPSOLVER_KEY]      = cfg.capsolverKey;
    if (Object.keys(update).length) await skSet(update);
    return cfg;
  } catch (e) {
    console.warn("[OctoProbe] config.json CF load failed:", e);
    return null;
  }
}

async function _ensureCfEmailConfig() {
  const stored = await skGet(SK.CF_MAIL_DOMAIN, SK.CF_WORKER_URL, SK.CF_WORKER_SECRET);
  if (String(stored[SK.CF_MAIL_DOMAIN] ?? "").trim()) return stored;
  await _loadExtensionConfig();
  return skGet(SK.CF_MAIL_DOMAIN, SK.CF_WORKER_URL, SK.CF_WORKER_SECRET);
}

self._ensureCfEmailConfig = _ensureCfEmailConfig;

(async () => { await _loadExtensionConfig(); })();

// TARGET_URL, TARGET_HOST, MAILTM — defined in constants.js

// Session-unique key for MAIN-world property names and custom event names.
// Regenerated on every SW start — avoids detectable static "_octo*" fingerprint.
const _SK       = Math.random().toString(36).slice(2, 10);
const _EVT_ALERT = 'a' + _SK;
const _EVT_RCP   = 'r' + _SK;

// In-memory active tab ID; persisted to storage for SW restart survival.
let _activeTabId = null;
skGet(SK.ACTIVE_TAB_ID).then(d => { if (d[SK.ACTIVE_TAB_ID]) _activeTabId = d[SK.ACTIVE_TAB_ID]; });

// ---------------------------------------------------------------------------
// Run logger — accumulates timestamped entries during a single command run
// and saves to a .log file via chrome.downloads when the run ends.
// One file per command: register_auto_YYYY-MM-DDTHH-MM-SS.log etc.
// ---------------------------------------------------------------------------

const _runLog = (() => {
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
      const d   = await skGet(SK.BOT_ID);
      const sid = d[SK.BOT_ID] ? `_${d[SK.BOT_ID].slice(0, 8)}` : "";
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
        const timer = setTimeout(resolve, TIMER_MS.misc.DOWNLOAD_CAP); // safety cap — never hang
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

function _genCredentials(firstName, lastName) {
  // Normalize accents (é→e, ã→a, ç→c) before extracting chars to avoid "srg" instead of "ser".
  const norm = s => s.normalize("NFD").replace(_COMB,"").split(" ")[0].toLowerCase().replace(/[^a-z]/g,"");
  const f = norm(firstName), l = norm(lastName);
  const uf = f.slice(0, 3);
  const ul = l.slice(0, 3);
  const username = uf + ul + String(_rand(90000000)+10000000); // e.g. "serlim56027114"
  const sp="!@#$%&*_+", di="0123456789";
  const password = uf.toUpperCase() + ul.toLowerCase() + sp[_rand(sp.length)] + String(_rand(90000000) + 10000000);
  const emailLocal = ul + uf + String(_rand(900000) + 100000);
  return {username, password, emailLocal};
}

function generatePerson() {
  const gender    = _rand(2) === 0 ? "M" : "F";
  const firstName = _genGivenName(gender);
  const lastName  = _genSurname();
  const year      = 1964 + _rand(41);
  const month     = 1   + _rand(12);
  const day       = 1   + _rand(28);
  const pad       = n => String(n).padStart(2,"0");
  const {username, password, emailLocal} = _genCredentials(firstName, lastName);
  return {
    name:       firstName,
    surname:    lastName,
    username:   username,
    password:   password,
    email:      emailLocal,
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

async function _createMailTM(emailLocal, retries = 5) {
  let lastErr;

  for (let i = 0; i < retries; i++) {
    try {
      const domain = await _mtDomain();
      const local = emailLocal || _randomstring(10);
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

async function _createCloudflareMail(emailLocal) {
  await _ensureCfEmailConfig();
  const {[SK.CF_MAIL_DOMAIN]: domain} = await skGet(SK.CF_MAIL_DOMAIN);
  if (!String(domain ?? "").trim()) {
    const err = new Error(
      "Cloudflare email domain not configured — set cfMailDomain in octo_signal/extension/config.json (copy config.json.example), rebuild the extension, or let the hub send cfDomain on the register command",
    );
    err.proxyStatus = ERR_STATUS.EMAIL_PROVIDER;
    err.nextAction = NEXT_ACTION.ROTATE_PROXY;
    throw err;
  }
  const local = emailLocal || Math.random().toString(36).slice(2, 12);
  const email = `${local}@${domain}`;
  return {email, password: "", jwt: email};  // jwt = full address used as polling key
}

async function _pollCloudflare(fullEmail) {
  const {
    [SK.CF_WORKER_URL]:    workerUrl,
    [SK.CF_WORKER_SECRET]: secret,
  } = await skGet(SK.CF_WORKER_URL, SK.CF_WORKER_SECRET);

  if (!workerUrl || !secret) throw new Error("Cloudflare Worker not configured");

  const url = `${workerUrl.replace(/\/$/, "")}?email=${encodeURIComponent(fullEmail)}`;
  const r = await fetch(url, {headers: {"x-secret": secret}});
  if (!r.ok) throw new Error(`CF Worker error ${r.status}`);

  const data = await r.json();
  if (!data.found) return null;
  return data;  // {found, linkToken, codeToken}
}

// ---- unified facade --------------------------------------------------------

async function createTempEmail(emailLocal) {
  const {[SK.EMAIL_PROVIDER]: provider = "mailtm"} = await skGet(SK.EMAIL_PROVIDER);
  return provider === "cloudflare" ? _createCloudflareMail(emailLocal) : _createMailTM(emailLocal);
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
    await new Promise(r => setTimeout(r, TIMER_MS.captcha.POLL_INTERVAL));
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

async function _solveCapSolver(apiKey, pageUrl, siteKey, action) {
  const task = {type:"ReCaptchaV2EnterpriseTaskProxyless", websiteURL:pageUrl, websiteKey:siteKey};
  if (action) task.enterprisePayload = {action};
  const cr = await fetch("https://api.capsolver.com/createTask", {
    method: "POST", headers: {"Content-Type":"application/json"},
    body: JSON.stringify({clientKey: apiKey, task}),
  });
  const cd = await cr.json();
  if (cd.errorId !== 0) throw new Error(`CapSolver createTask err: ${cd.errorDescription}`);

  for (let i = 0; i < 40; i++) {
    await new Promise(r => setTimeout(r, TIMER_MS.captcha.POLL_INTERVAL));
    const rr = await fetch("https://api.capsolver.com/getTaskResult", {
      method: "POST", headers: {"Content-Type":"application/json"},
      body: JSON.stringify({clientKey: apiKey, taskId: cd.taskId}),
    });
    const rd = await rr.json();
    if (rd.errorId !== 0) throw new Error(`CapSolver getTaskResult err: ${rd.errorDescription}`);
    if (rd.status === "ready") return rd.solution.gRecaptchaResponse;
  }
  throw new Error("CapSolver timed out");
}

// Race all solvers — loads API keys from storage (set by _loadExtensionConfig from config.json).
// Resolves on first successful token, rejects only if ALL fail.
async function _raceSolvers(pageUrl, siteKey, action) {
  const stored = await skGet(SK.ANTICAPTCHA_KEYS, SK.TWOCAPTCHA_KEYS, SK.CAPMONSTER_KEYS, SK.CAPSOLVER_KEY);
  const acKeys  = stored[SK.ANTICAPTCHA_KEYS] ?? [];
  const tcKeys  = stored[SK.TWOCAPTCHA_KEYS]  ?? [];
  const cmKeys  = stored[SK.CAPMONSTER_KEYS]  ?? [];
  const csKey   = stored[SK.CAPSOLVER_KEY]    ?? "";

  return new Promise((resolve, reject) => {
    let settled = false; let remaining = 4;
    function got(token) {
      if (settled) return;
      if (token) { settled = true; resolve(token); return; }
      if (--remaining === 0) reject(new Error("All solvers failed"));
    }
    _solveACFormat("https://api.anti-captcha.com", _pick(acKeys), pageUrl, siteKey, action).then(got).catch(() => got(null));
    _solveACFormat("https://api.2captcha.com",     _pick(tcKeys), pageUrl, siteKey, action).then(got).catch(() => got(null));
    _solveACFormat("https://api.capmonster.cloud", _pick(cmKeys), pageUrl, siteKey, action).then(got).catch(() => got(null));
    _solveCapSolver(csKey, pageUrl, siteKey, action).then(got).catch(() => got(null));
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
  p6_stop();
  clearInterval(_pollInterval);
  _pollInterval = null;
  _activeTabId = null;

  await skRemove(
    SK.WORKFLOW_TYPE, SK.WORKFLOW_STEP,
    SK.REGISTER_PERSON, SK.REGISTER_EMAIL,
    SK.EMAIL_TOKEN, SK.EMAIL_CODE_TOKEN, SK.EMAIL_POLL,
    SK.LOGIN_PENDING, SK.PENDING_ACCOUNT,
    SK.ACTIVE_TAB_ID, SK.REGISTER_RETRIED,
    SK.WARMUP_IDLE_STATE, SK.WARMUP_TAB_ID,
    SK.CHALLENGE_COUNT,
    SK.RUN_STATUS, SK.RUN_ERROR,
  );
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
  await skSet({[SK.ACTIVE_TAB_ID]: _activeTabId});
  return _activeTabId;
}

// Creates a dedicated background tab for one concurrent session.
// visibilityState is patched by antidetect.js to always return "visible",
// and CDP mouse events (reCAPTCHA click) work on background tabs.
async function _createSessionTab() {
  const tab = await chrome.tabs.create({url: TARGET_URL, active: false});
  const tabId = tab.id;
  _activeTabId = tabId; // keep for abort/proxy-check only
  await skSet({[SK.ACTIVE_TAB_ID]: tabId});
  return tabId;
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
  const {[SK.EMAIL_POLL]: emailPoll} = await skGet(SK.EMAIL_POLL);
  if (!emailPoll) { clearInterval(_pollInterval); _pollInterval = null; return; }

  try {
    const {[SK.EMAIL_PROVIDER]: provider = "mailtm"} = await skGet(SK.EMAIL_PROVIDER);
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
    await skRemove(SK.EMAIL_POLL);
    const storageUpdate = {};
    if (linkToken) storageUpdate[SK.EMAIL_TOKEN]      = linkToken;
    if (codeToken) storageUpdate[SK.EMAIL_CODE_TOKEN] = codeToken;
    await skSet(storageUpdate);

    // Notify the session's content script — prefer codeToken (UUID) over linkToken (hex).
    // emailPoll.tabId was stored when _startEmailPoll() was called — routes to the correct session.
    const _pollTabId = emailPoll.tabId ?? _activeTabId;
    if (_pollTabId) chrome.tabs.sendMessage(_pollTabId, {type: "email-token", token: codeToken ?? linkToken}).catch(() => {});
  } catch (e) {
    if (e.authExpired) {
      clearInterval(_pollInterval);
      _pollInterval = null;
      await skRemove(SK.EMAIL_POLL);
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
  skSet({[SK.EMAIL_POLL]: {jwt, tabId}});
  if (!_pollInterval) _pollInterval = setInterval(_doPollTick, TIMER_MS.email.POLL_INTERVAL);
}

async function _waitForEmailCodeToken(maxMs = TIMER_MS.email.CODE_TIMEOUT) {
  const deadline = Date.now() + maxMs;
  while (Date.now() < deadline) {
    const d = await skGet(SK.EMAIL_CODE_TOKEN, SK.EMAIL_TOKEN);
    if (d[SK.EMAIL_CODE_TOKEN]) return d[SK.EMAIL_CODE_TOKEN];
    if (d[SK.EMAIL_TOKEN]) return d[SK.EMAIL_TOKEN];
    await new Promise(r => setTimeout(r, TIMER_MS.email.POLL_RETRY));
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

//#region Content Interactions

// Low-level bridge between the service worker and content scripts.
// Every function here either SENDS a command to content (sendTabCmd) or WAITS
// for a signal FROM content (_waitForPageReady / page-ready message).
//
// Functions in this region:
//   sendTabCmd(tabId, type, params)   — defined above (before polling setup)
//   _waitForPageReady(tabId, ms)      — defined below in Shared Utilities (pre-migration)
//   _domStateToStep(domState)         — defined below in Shared Utilities (pre-migration)
//   _currentWfStep(tabId)             — defined below in Shared Utilities (pre-migration)
//   ctEnsureLangChangeToEN(tabId)     — stub below (TODO: implement)
//
// TODO: physically move sendTabCmd, _waitForPageReady, _domStateToStep, _currentWfStep
//       here once the region refactor is complete.

// Ensure the page language is set to English before proceeding.
// Sends cmd-switch-lang, then waits for page-ready to confirm the reload.
async function ctEnsureLangChangeToEN(tabId) {
  const {[SK.LANG_SWITCH_ENABLED]: _langEnabled = false} = await skGet(SK.LANG_SWITCH_ENABLED);
  if (!_langEnabled) return;
  const langReady = _waitForPageReady(tabId, TIMER_MS.pageReady.LANG_SWITCH);
  await sendTabCmd(tabId, "cmd-switch-lang").catch(() => {});
  await langReady.catch(() => {}); // ignore timeout if already English
}

//#endregion

//#region StepHandlers
// Atomic navigation units. Each handler:
//   • takes tabId as first parameter
//   • writes SK.WORKFLOW_STEP before resolving
//   • throws {proxyStatus, nextAction} errors — phase handlers catch and relay them

// Visit warmup URLs to build a browsing-history baseline before opening the target site.
// Skipped when PRE_VISIT_WARMUP is false (checked by p0_openAuthPage before calling).
async function step0_warmup(tabId) {
  for (const url of _pickWarmupUrls()) {
    await chrome.tabs.update(tabId, {url});
    await new Promise(resolve => {
      const check = setInterval(async () => {
        const tab = await chrome.tabs.get(tabId).catch(() => null);
        if (!tab || tab.status === "complete") { clearInterval(check); resolve(); }
      }, TIMER_MS.warmup.CHECK_INTERVAL);
      setTimeout(() => { clearInterval(check); resolve(); }, TIMER_MS.warmup.READY_TIMEOUT);
    });
    await new Promise(r => setTimeout(r,
      TIMER_MS.warmup.DWELL_MIN + Math.random() * TIMER_MS.warmup.DWELL_MARGIN));
  }
  await skSet({[SK.WORKFLOW_STEP]: WF_STEPS.WARMUP});
}

// Navigate to TARGET_URL and resolve WAF challenges.
// Returns the page-state string on arrival ("not-logged-in", "auth", "logged-in", etc.).
// Skips navigation when already on a known post-home state.
async function step1_homeReady(tabId) {
  let state;
  try { ({state} = await sendTabCmd(tabId, "cmd-get-state")); } catch (_) { state = "unknown"; }

  // Already past the unauthenticated home — caller can skip step2.
  const _postHome = new Set(["auth","reg-form","logged-in","token",
    "questionnaire","form","schedule","schedule-captcha",
    "schedule-calendar","schedule-submitted"]);
  if (_postHome.has(state)) {
    await skSet({[SK.WORKFLOW_STEP]: WF_STEPS.HOME_READY});
    return state;
  }

  if (state !== "not-logged-in") {
    const pageReady = _waitForPageReady(tabId, TIMER_MS.pageReady.SSO);
    await chrome.tabs.update(tabId, {url: TARGET_URL});
    ({state} = await pageReady.catch(() => ({state: "timeout"})));
  }

  if (state === "waf-challenge") {
    const e = new Error("step1: WAF challenge — rotate proxy");
    e.proxyStatus = ERR_STATUS.BLOCKED; e.nextAction = NEXT_ACTION.ROTATE_PROXY; throw e;
  }
  if (state === "waf-challenge-active") {
    const pr2 = _waitForPageReady(tabId, TIMER_MS.pageReady.SSO);
    state = (await pr2.catch(() => ({state: "timeout"}))).state ?? "timeout";
    if (state === "waf-challenge") {
      const e = new Error("step1: WAF challenge not resolved — rotate proxy");
      e.proxyStatus = ERR_STATUS.BLOCKED; e.nextAction = NEXT_ACTION.ROTATE_PROXY; throw e;
    }
  }

  await skSet({[SK.WORKFLOW_STEP]: WF_STEPS.HOME_READY});
  return state;
}

// From the unauthenticated home page, click the login link and reach the "auth" state.
// Handles SSO redirects, WAF-challenge-active auto-redirects, and lang-switch reloads.
// Retries up to 3 times; on retry re-navigates via step1_homeReady if page drifted.
async function step2_authReady(tabId) {
  let state;
  try { ({state} = await sendTabCmd(tabId, "cmd-get-state")); } catch (_) { state = "unknown"; }
  if (state === "auth") {
    await skSet({[SK.WORKFLOW_STEP]: WF_STEPS.AUTH_READY});
    return "auth";
  }

  for (let attempt = 0; attempt < 3; attempt++) {
    if (attempt > 0) {
      await new Promise(r => setTimeout(r, TIMER_MS.auth.RETRY_BASE + attempt * TIMER_MS.auth.RETRY_STEP));
      try { ({state} = await sendTabCmd(tabId, "cmd-get-state")); } catch (_) { state = "unknown"; }
      if (state === "auth") { await skSet({[SK.WORKFLOW_STEP]: WF_STEPS.AUTH_READY}); return "auth"; }
      // Drifted off-site or into an unknown state — re-navigate to home first.
      if (state !== "not-logged-in" && state !== "off-site") {
        state = await step1_homeReady(tabId);
        if (state === "auth") return "auth";
      }
    }

    // Lang switch may reload the page to English — must fire before login-link click.
    const {[SK.LANG_SWITCH_ENABLED]: langEnabled = false} = await skGet(SK.LANG_SWITCH_ENABLED);
    if (langEnabled) {
      const langReady = _waitForPageReady(tabId, TIMER_MS.pageReady.LANG_SWITCH);
      await sendTabCmd(tabId, "cmd-switch-lang").catch(() => {});
      await langReady.catch(() => {});
      try { ({state} = await sendTabCmd(tabId, "cmd-get-state")); } catch (_) {}
      if (state === "auth") { await skSet({[SK.WORKFLOW_STEP]: WF_STEPS.AUTH_READY}); return "auth"; }
    }

    const authReady = _waitForPageReady(tabId, TIMER_MS.pageReady.AUTH_CLICK);
    await sendTabCmd(tabId, "cmd-click-login-link").catch(() => {});
    let authState;
    try { ({state: authState} = await authReady); } catch (_) { authState = "timeout"; }

    if (authState === "auth") {
      await skSet({[SK.WORKFLOW_STEP]: WF_STEPS.AUTH_READY});
      return "auth";
    }

    if (authState === "off-site" || authState === "timeout" || authState === "waf-challenge-active") {
      _runLog.entry(`step2: login led to ${authState} — waiting for redirect (120s)`);
      const ssoReady = _waitForPageReady(tabId, TIMER_MS.pageReady.SSO);
      authState = (await ssoReady.catch(() => ({state: "sso-timeout"}))).state ?? "sso-timeout";
      if (authState === "auth") { await skSet({[SK.WORKFLOW_STEP]: WF_STEPS.AUTH_READY}); return "auth"; }
      if (authState === "not-logged-in") { state = authState; continue; }
      if (authState === "waf-challenge") {
        const e = new Error("step2: WAF challenge on auth page — rotate proxy");
        e.proxyStatus = ERR_STATUS.BLOCKED; e.nextAction = NEXT_ACTION.ROTATE_PROXY; throw e;
      }
    }

    state = authState;
    _runLog.entry(`step2: attempt ${attempt + 1} state=${state} — retrying`);
  }

  const e = new Error(`step2: expected auth, got ${state} after 3 attempts`);
  e.proxyStatus = ERR_STATUS.UNKNOWN; e.nextAction = NEXT_ACTION.ROTATE_PROXY; throw e;
}

// Navigate from auth page to registration form and wait for all AJAX fields to load.
async function step3_regFormReady(tabId) {
  let state;
  try { ({state} = await sendTabCmd(tabId, "cmd-get-state")); } catch (_) { state = "unknown"; }
  if (state === "reg-form") {
    await skSet({[SK.WORKFLOW_STEP]: WF_STEPS.REG_FORM_READY});
    return;
  }

  const regPageReady = _waitForPageReady(tabId, TIMER_MS.pageReady.REG_FORM);
  regPageReady.catch(() => {});

  let openResult;
  try {
    openResult = await sendTabCmd(tabId, "cmd-register-open-form");
  } catch (e) {
    if (!String(e).includes("message channel closed")) throw e;
    const {state: navState} = await regPageReady.catch(() => ({state: "timeout"}));
    _runLog.entry(`step3: page navigated → state=${navState}`);
    openResult = await sendTabCmd(tabId, "cmd-register-open-form");
  }

  if (!openResult?.ok) {
    const e = new Error(`step3: reg form open failed — ${openResult?.status ?? "unknown"}`);
    e.proxyStatus = ERR_STATUS.PROXY_SLOW; e.nextAction = NEXT_ACTION.ROTATE_PROXY; throw e;
  }
  await skSet({[SK.WORKFLOW_STEP]: WF_STEPS.REG_FORM_READY});
}

// Fill the registration form, check burned proxies, then submit (cmd-register-submit handles
// two RGPD+captcha cycles internally). Arms tokenPageReady BEFORE the submit call to avoid
// the race where the server redirect arrives before we could arm the listener after submit.
// Returns {ok:true, tokenPageReady} on success; {ok:false, status} on captcha failure.
// Throws immediately for unrecoverable errors (IP blocked, username/email collision).
async function step4_regFormSubmit(tabId, person, emailAcct) {
  const fillResult = await sendTabCmd(tabId, "cmd-register-fill", {person, email: emailAcct.email});
  if (!fillResult?.ok) {
    await skSet({[SK.WORKFLOW_STEP]: WF_STEPS.REG_FORM_FAILED});
    return {ok: false, status: fillResult?.status ?? "fill_failed"};
  }

  let _proxyIp = null;
  try { const r = await sendTabCmd(tabId, "cmd-log-ip"); _proxyIp = r?.ip ?? null; } catch (_) {}
  if (_proxyIp) {
    const {[SK.BURNED_PROXIES]: _burned = []} = await skGet(SK.BURNED_PROXIES);
    const _now = Date.now();
    const _active = _burned.filter(e => (_now - e.burnedAt) < TIMER_MS.proxy.BURN_TTL);
    if (_active.length !== _burned.length) await skSet({[SK.BURNED_PROXIES]: _active});
    if (_active.some(e => e.ip === _proxyIp)) {
      const e = new Error(`step4: proxy ${_proxyIp} already burned — rotate`);
      e.proxyStatus = ERR_STATUS.BURNED; e.nextAction = NEXT_ACTION.ROTATE_PROXY; throw e;
    }
    _runLog.entry(`step4: proxy ${_proxyIp} — not burned, proceeding`);
  }

  const tokenPageReady = _waitForPageReady(tabId, TIMER_MS.pageReady.TOKEN_PAGE);
  tokenPageReady.catch(() => {});

  await skSet({[SK.WORKFLOW_STEP]: WF_STEPS.REG_FORM_SUBMITTED});
  const result = await sendTabCmd(tabId, "cmd-register-submit").catch(() => ({ok: true, status: "navigated"}));
  _runLog.entry(`step4: submit → ${result.status}`);

  if (result.status === "ip_blocked") {
    const e = new Error("step4: IP blocked by captcha rate-limit");
    e.proxyStatus = ERR_STATUS.BLOCKED; e.nextAction = NEXT_ACTION.ROTATE_PROXY; throw e;
  }
  if (result.status === "username_taken") {
    const e = new Error("step4: username already taken");
    e.proxyStatus = ERR_STATUS.USERNAME_COLLISION; e.nextAction = NEXT_ACTION.ROTATE_PROXY; throw e;
  }
  if (result.status === "email_taken") {
    const e = new Error("step4: email already registered");
    e.proxyStatus = ERR_STATUS.EMAIL_TAKEN; e.nextAction = NEXT_ACTION.ROTATE_PROXY; throw e;
  }
  if (result.status === "captcha_fail") {
    if (_proxyIp) {
      const {[SK.BURNED_PROXIES]: _bl = []} = await skGet(SK.BURNED_PROXIES);
      const _now = Date.now();
      const _al = _bl.filter(e => (_now - e.burnedAt) < TIMER_MS.proxy.BURN_TTL);
      if (!_al.some(e => e.ip === _proxyIp)) { _al.push({ip: _proxyIp, burnedAt: _now}); await skSet({[SK.BURNED_PROXIES]: _al}); }
      _runLog.entry(`step4: proxy ${_proxyIp} marked as burned`);
    }
    await skSet({[SK.WORKFLOW_STEP]: WF_STEPS.REG_FORM_FAILED});
    return {ok: false, status: "captcha_fail"};
  }
  if (result.status !== "navigated" && result.status !== "submitted") {
    await skSet({[SK.WORKFLOW_STEP]: WF_STEPS.REG_FORM_FAILED});
    return {ok: false, status: result.status ?? "unknown_failure"};
  }

  _startEmailPoll(emailAcct.jwt, tabId);
  return {ok: true, tokenPageReady};
}

// External retry prep: navigate home for a fresh registration form, re-auth, re-open, and
// pre-fill so that step6 can submit immediately. Stops the stale email poll from step4 since
// captcha failure means the form was never actually submitted.
async function step5_regFormReady2(tabId, person, emailAcct) {
  clearInterval(_pollInterval); _pollInterval = null;
  _runLog.entry("step5: navigating home for fresh registration form");

  const homeReady = _waitForPageReady(tabId, TIMER_MS.pageReady.DEFAULT);
  homeReady.catch(() => {});
  await chrome.tabs.update(tabId, {url: TARGET_URL});
  await homeReady.catch(() => {});

  const state = await step1_homeReady(tabId);
  if (state !== "not-logged-in") {
    return {ok: false, status: `unexpected state after home nav: ${state}`};
  }
  await step2_authReady(tabId);
  await step3_regFormReady(tabId);

  const fillResult = await sendTabCmd(tabId, "cmd-register-fill", {person, email: emailAcct.email});
  if (!fillResult?.ok) return {ok: false, status: fillResult?.status ?? "fill_failed_on_retry"};

  await skSet({[SK.WORKFLOW_STEP]: WF_STEPS.REG_FORM_FAILED});
  return {ok: true};
}

// Second external submit attempt. Arms a fresh tokenPageReady before the submit call.
async function step6_regFormSubmit2(tabId, emailAcct) {
  const tokenPageReady = _waitForPageReady(tabId, TIMER_MS.pageReady.TOKEN_PAGE);
  tokenPageReady.catch(() => {});

  await skSet({[SK.WORKFLOW_STEP]: WF_STEPS.REG_FORM_SUBMITTED2});
  const result = await sendTabCmd(tabId, "cmd-register-submit").catch(() => ({ok: true, status: "navigated"}));
  _runLog.entry(`step6: submit → ${result.status}`);

  if (result.status === "ip_blocked") {
    const e = new Error("step6: IP blocked — rotate proxy");
    e.proxyStatus = ERR_STATUS.BLOCKED; e.nextAction = NEXT_ACTION.ROTATE_PROXY; throw e;
  }
  if (result.status === "username_taken") {
    const e = new Error("step6: username taken on retry");
    e.proxyStatus = ERR_STATUS.USERNAME_COLLISION; e.nextAction = NEXT_ACTION.ROTATE_PROXY; throw e;
  }
  if (result.status === "email_taken") {
    const e = new Error("step6: email taken on retry");
    e.proxyStatus = ERR_STATUS.EMAIL_TAKEN; e.nextAction = NEXT_ACTION.ROTATE_PROXY; throw e;
  }
  if (result.status === "captcha_fail") {
    await skSet({[SK.WORKFLOW_STEP]: WF_STEPS.REG_FORM_FAILED2});
    return {ok: false, status: "captcha_fail"};
  }
  if (result.status !== "navigated" && result.status !== "submitted") {
    await skSet({[SK.WORKFLOW_STEP]: WF_STEPS.REG_FORM_FAILED2});
    return {ok: false, status: result.status ?? "unknown_failure"};
  }

  _startEmailPoll(emailAcct.jwt, tabId);
  return {ok: true, tokenPageReady};
}

// All submit attempts exhausted — mark step and throw for the workflow handler to catch.
async function step7_regFormFailed2(tabId, status) {
  await skSet({[SK.WORKFLOW_STEP]: WF_STEPS.REG_FORM_FAILED2});
  const e = new Error(`step7: registration failed after all attempts — ${status}`);
  e.proxyStatus = ERR_STATUS.BURNED; e.nextAction = NEXT_ACTION.ROTATE_PROXY; throw e;
}

// Await the token page redirect that was armed before the last submit call.
// If the promise rejects (600 s timeout) or resolves with an unexpected state, throw.
async function step8_regVerifyReady(tabId, tokenPageReady) {
  _runLog.entry("step8: waiting for token page redirect");
  const {state} = await tokenPageReady.catch(() => ({state: "timeout"}));
  if (state !== "token") {
    await skSet({[SK.WORKFLOW_STEP]: WF_STEPS.REG_VERIFY_FAILED});
    const e = new Error(`step8: token page never reached — state=${state}`);
    e.proxyStatus = ERR_STATUS.PROXY_SLOW; e.nextAction = NEXT_ACTION.ROTATE_PROXY; throw e;
  }
  await skSet({[SK.WORKFLOW_STEP]: WF_STEPS.REG_VERIFY_READY});
  _runLog.entry("step8: token page reached");
}

// Token verification failed — mark step and throw so the workflow handler can relay the error.
async function step9_regVerifyFailed(tabId, reason) {
  await skSet({[SK.WORKFLOW_STEP]: WF_STEPS.REG_VERIFY_FAILED});
  const e = new Error(`step9: email verification failed — ${reason}`);
  e.proxyStatus = ERR_STATUS.PROXY_SLOW; e.nextAction = NEXT_ACTION.ROTATE_PROXY; throw e;
}

// Poll for the email verification code, write SK.PENDING_ACCOUNT (before navigate — content.js
// dies on navigation), then fill + submit the token form.
async function step10_regVerify(tabId, person, emailAcct) {
  const codeToken = await _waitForEmailCodeToken(TIMER_MS.email.CODE_TIMEOUT);
  if (!codeToken) return await step9_regVerifyFailed(tabId, "email code token timeout");
  _runLog.entry(`step10: email code received: ${codeToken}`);

  const {[SK.BOT_ID]: _sessionId} = await skGet(SK.BOT_ID);
  await skSet({
    [SK.PENDING_ACCOUNT]: {
      username:      person.username,  password:   person.password,
      name:          person.name,      surname:    person.surname,
      email:         emailAcct.email,  birth_date: person.birth_date,
      gender:        person.gender,    nationality: person.nationality ?? "CPV",
      traveldoc:     person.traveldoc, registered_at: new Date().toISOString(),
      sessionId:     _sessionId ?? null,
    }
  });

  await sendTabCmd(tabId, "cmd-token-fill", {token: codeToken});
  _runLog.entry("step10: submitting email token");
  const verifyReady = _waitForPageReady(tabId, TIMER_MS.pageReady.DEFAULT);
  const tokenResult = await sendTabCmd(tabId, "cmd-token-submit")
    .catch(() => ({ok: true, status: "navigated"}));
  const {state: finalState} = await verifyReady;

  if (!tokenResult.ok) {
    await skRemove(SK.PENDING_ACCOUNT);
    return await step9_regVerifyFailed(tabId,
      `${tokenResult.status}${tokenResult.alert ? ` — ${tokenResult.alert}` : ""}`);
  }

  try {
    const {[SK.ACCOUNTS]: _accs} = await skGet(SK.ACCOUNTS);
    const _list = Array.isArray(_accs) ? _accs : [];
    if (!_list.some(a => a.username === person.username)) {
      _list.push({
        username: person.username, password:     person.password,
        name:     person.name,     surname:       person.surname,
        email:    emailAcct.email, birth_date:    person.birth_date,
        gender:   person.gender,   nationality:   person.nationality ?? "CPV",
        traveldoc: person.traveldoc, registered_at: new Date().toISOString(),
      });
      await skSet({[SK.ACCOUNTS]: _list});
      _runLog.entry(`step10: credentials saved (username=${person.username})`);
    }
  } catch (_) {}

  await skSet({[SK.WORKFLOW_STEP]: WF_STEPS.REG_VERIFY_SUCCESS});
  return {ok: true, status: finalState === "auth" ? "verified" : finalState};
}

// Fill login credentials, solve reCAPTCHA, and submit. cmd-login-submit handles RGPD consent
// and waits for the redirect internally. Returns {ok:true} on logged-in/navigated.
async function step11_loginSubmit(tabId, creds) {
  await sendTabCmd(tabId, "cmd-login-fill", creds);
  await skSet({[SK.WORKFLOW_STEP]: WF_STEPS.LOGIN_SUBMITTED});

  const result = await sendTabCmd(tabId, "cmd-login-submit").catch(async () => {
    await _waitForPageReady(tabId, TIMER_MS.pageReady.LOGIN_SUBMIT).catch(() => {});
    return {ok: true, status: "navigated"};
  });

  if (result.status === "logged-in" || result.status === "navigated") return {ok: true, status: "logged-in"};
  await skSet({[SK.WORKFLOW_STEP]: WF_STEPS.LOGIN_FAILED});
  return {ok: false, status: result.status ?? "rejected"};
}

// Recovery between login attempts: settle, verify we are still on the auth page
// (re-navigate via step1+step2 if the page drifted), then re-fill credentials so
// step13 can submit without a redundant fill call.
async function step12_loginReady2(tabId, creds) {
  await new Promise(r => setTimeout(r, TIMER_MS.auth.LOGIN_RETRY));

  let state;
  try { ({state} = await sendTabCmd(tabId, "cmd-get-state")); } catch (_) { state = "unknown"; }

  if (state !== "auth") {
    state = await step1_homeReady(tabId);
    if (state !== "not-logged-in") {
      return {ok: false, status: `step12: unexpected state ${state}`};
    }
    await step2_authReady(tabId);
  }

  await sendTabCmd(tabId, "cmd-login-fill", creds).catch(() => {});
  await skSet({[SK.WORKFLOW_STEP]: WF_STEPS.LOGIN_FAILED});
  return {ok: true};
}

// Second login submit attempt — credentials are already filled by step12.
async function step13_loginSubmit2(tabId) {
  await skSet({[SK.WORKFLOW_STEP]: WF_STEPS.LOGIN_SUBMITTED2});

  const result = await sendTabCmd(tabId, "cmd-login-submit").catch(async () => {
    await _waitForPageReady(tabId, TIMER_MS.pageReady.LOGIN_SUBMIT).catch(() => {});
    return {ok: true, status: "navigated"};
  });

  if (result.status === "logged-in" || result.status === "navigated") return {ok: true, status: "logged-in"};
  await skSet({[SK.WORKFLOW_STEP]: WF_STEPS.LOGIN_FAILED2});
  return {ok: false, status: result.status ?? "rejected"};
}

// All login attempts exhausted — mark step and throw.
async function step14_loginFailed2(tabId, status) {
  await skSet({[SK.WORKFLOW_STEP]: WF_STEPS.LOGIN_FAILED2});
  const e = new Error(`step14: login failed after all attempts — ${status}`);
  e.proxyStatus = ERR_STATUS.LOGIN_REJECTED; e.nextAction = NEXT_ACTION.ROTATE_PROXY; throw e;
}

// Confirm the index page is loaded and the session is authenticated.
// cmd-login-submit already waited for navigation; this is a sanity check + WF_STEP commit.
// If the state is still transitioning, wait one more page-ready signal.
async function step15_loginSuccess(tabId) {
  let state;
  try { ({state} = await sendTabCmd(tabId, "cmd-get-state")); } catch (_) { state = "unknown"; }

  if (state !== "logged-in") {
    const {state: finalState} = await _waitForPageReady(tabId, TIMER_MS.pageReady.DEFAULT)
      .catch(() => ({state: "timeout"}));
    state = finalState;
  }

  if (state !== "logged-in") {
    const e = new Error(`step15: expected logged-in, got ${state}`);
    e.proxyStatus = ERR_STATUS.LOGIN_REJECTED; e.nextAction = NEXT_ACTION.ROTATE_PROXY; throw e;
  }
  await skSet({[SK.WORKFLOW_STEP]: WF_STEPS.LOGIN_SUCCESS});
  return {ok: true, status: "logged-in"};
}

// Navigate from the index page to the questionnaire. Skips if already on questionnaire
// or deeper. Returns {ok:false, status:"session_expired"} if auth redirect detected.
async function step16_queryReady(tabId) {
  let state;
  try { ({state} = await sendTabCmd(tabId, "cmd-get-state")); } catch (_) { state = "unknown"; }
  if (state === "questionnaire") { await skSet({[SK.WORKFLOW_STEP]: WF_STEPS.QUERY_READY}); return {ok: true}; }
  if (state === "form")  { await skSet({[SK.WORKFLOW_STEP]: WF_STEPS.REQ_FORM_READY}); return {ok: true}; }
  if (state === "schedule" || state === "schedule-captcha" || state === "schedule-calendar" || state === "schedule-submitted") {
    return {ok: true};
  }
  if (state === "auth") return {ok: false, status: "session_expired"};

  const qNavReady = _waitForPageReady(tabId, TIMER_MS.pageReady.DEFAULT);
  await sendTabCmd(tabId, "cmd-go-questionnaire").catch(() => {});
  const {state: qState} = await qNavReady.catch(() => ({state: "timeout"}));

  if (qState === "questionnaire") { await skSet({[SK.WORKFLOW_STEP]: WF_STEPS.QUERY_READY}); return {ok: true}; }
  if (qState === "auth") return {ok: false, status: "session_expired"};
  const e = new Error(`step16: expected questionnaire, got ${qState}`);
  e.proxyStatus = ERR_STATUS.UNKNOWN; e.nextAction = NEXT_ACTION.ROTATE_PROXY; throw e;
}

// Fill and submit the questionnaire. Arms pageReady BEFORE the fill command (the submit
// at the end of the questionnaire navigates to the form page). Handles session expiry.
async function step17_querySubmit(tabId) {
  const qFillReady = _waitForPageReady(tabId, TIMER_MS.pageReady.QUESTIONNAIRE);
  await sendTabCmd(tabId, "cmd-fill-questionnaire").catch(() => {});
  const {state: fState} = await qFillReady.catch(() => ({state: "timeout"}));

  if (fState === "form") { await skSet({[SK.WORKFLOW_STEP]: WF_STEPS.QUERY_SUBMITTED}); return {ok: true}; }
  if (fState === "auth") return {ok: false, status: "session_expired"};
  if (fState === "questionnaire") return {ok: false, status: "questionnaire_reload"};
  return {ok: false, status: fState ?? "timeout"};
}

// All questionnaire attempts exhausted — always throws.
async function step18_queryFailed(tabId, status) {
  await skSet({[SK.WORKFLOW_STEP]: WF_STEPS.QUERY_FAILED});
  const e = new Error(`step18: questionnaire failed after all attempts — ${status}`);
  e.proxyStatus = ERR_STATUS.UNKNOWN; e.nextAction = NEXT_ACTION.ROTATE_PROXY; throw e;
}

// Confirm the request form page is ready. Waits one page-ready signal if still transitioning.
async function step19_reqFormReady(tabId) {
  let state;
  try { ({state} = await sendTabCmd(tabId, "cmd-get-state")); } catch (_) { state = "unknown"; }
  if (state === "form") { await skSet({[SK.WORKFLOW_STEP]: WF_STEPS.REQ_FORM_READY}); return {ok: true}; }
  if (state === "auth") return {ok: false, status: "session_expired"};

  // May still be transitioning from questionnaire submit.
  const {state: s2} = await _waitForPageReady(tabId, TIMER_MS.pageReady.DEFAULT).catch(() => ({state: "timeout"}));
  if (s2 === "form") { await skSet({[SK.WORKFLOW_STEP]: WF_STEPS.REQ_FORM_READY}); return {ok: true}; }
  if (s2 === "auth") return {ok: false, status: "session_expired"};
  const e = new Error(`step19: expected form, got ${s2}`);
  e.proxyStatus = ERR_STATUS.UNKNOWN; e.nextAction = NEXT_ACTION.ROTATE_PROXY; throw e;
}

// Fill all form tabs without submitting (consular post is NOT selected here).
// cmd-fill-form-tabs uses submitAfter=false so the page stays on the form.
async function step20_reqFormFill(tabId) {
  await sendTabCmd(tabId, "cmd-fill-form-tabs").catch(() => {});
  await skSet({[SK.WORKFLOW_STEP]: WF_STEPS.REQ_FORM_FILLED});
  return {ok: true};
}

// Wait for the consular post to become available and signal submission can proceed.
// AUTO_TRIGGER: start post monitor, then wait via _postAvailableResolver until content.js
//   fires target-post-available (post in dropdown) — up to 15 min.
// EXTERNAL_SIGNAL: wf_apply is only called after the external signal arrives, so the post
//   is already available; just start the monitor for the MutationObserver refresh and return.
async function step21_reqFormSignal(tabId) {
  const {[SK.TRIGGER_MODE]: triggerMode = "AUTO_TRIGGER"} = await skGet(SK.TRIGGER_MODE);
  await skSet({[SK.WORKFLOW_STEP]: WF_STEPS.REQ_FORM_SIGNAL});
  await sendTabCmd(tabId, "cmd-start-post-monitor").catch(() => {});

  if (triggerMode === "EXTERNAL_SIGNAL") {
    return {ok: true}; // signal already received — wf_apply was the trigger
  }

  // AUTO_TRIGGER: park on a promise that the target-post-available handler resolves.
  return new Promise(resolve => {
    const tid = setTimeout(() => {
      _postAvailableResolver = null;
      resolve({ok: false, status: "post_not_available"});
    }, 900_000); // 15 min max wait
    _postAvailableResolver = () => {
      clearTimeout(tid);
      _postAvailableResolver = null;
      resolve({ok: true});
    };
  });
}

// Submit the request form. Arms pageReady BEFORE the command (submit navigates to schedule).
async function step22_reqFormSubmit(tabId) {
  const schReady = _waitForPageReady(tabId, TIMER_MS.pageReady.FORM_FILL);
  await sendTabCmd(tabId, "cmd-submit-form").catch(() => {});
  const {state: sState} = await schReady.catch(() => ({state: "timeout"}));

  if (sState === "schedule" || sState === "schedule-captcha") {
    await skSet({[SK.WORKFLOW_STEP]: WF_STEPS.REQ_FORM_SUBMITTED});
    return {ok: true};
  }
  if (sState === "form") return {ok: false, status: "form_rejected"};
  if (sState === "auth") return {ok: false, status: "session_expired"};
  return {ok: false, status: sState ?? "timeout"};
}

// All form submission attempts exhausted — always throws.
async function step23_reqFormFailed(tabId, status) {
  await skSet({[SK.WORKFLOW_STEP]: WF_STEPS.QUERY_FAILED});
  const e = new Error(`step23: form submission failed after all attempts — ${status}`);
  e.proxyStatus = ERR_STATUS.UNKNOWN; e.nextAction = NEXT_ACTION.ROTATE_PROXY; throw e;
}

// Confirm the schedule page is loaded and the reCAPTCHA challenge is visible.
async function step24_schCaptchaReady(tabId) {
  let state;
  try { ({state} = await sendTabCmd(tabId, "cmd-get-state")); } catch (_) { state = "unknown"; }

  if (state === "schedule" || state === "schedule-captcha") {
    await skSet({[SK.WORKFLOW_STEP]: WF_STEPS.SCHEDULE_RECAPTCHA_READY});
    return {ok: true};
  }
  if (state === "schedule-calendar" || state === "schedule-submitted") {
    return {ok: true}; // already past captcha
  }
  if (state === "auth") return {ok: false, status: "session_expired"};

  // Page may still be loading — wait one page-ready signal.
  const {state: s2} = await _waitForPageReady(tabId, TIMER_MS.pageReady.DEFAULT).catch(() => ({state: "timeout"}));
  if (s2 === "schedule" || s2 === "schedule-captcha") {
    await skSet({[SK.WORKFLOW_STEP]: WF_STEPS.SCHEDULE_RECAPTCHA_READY});
    return {ok: true};
  }
  if (s2 === "auth") return {ok: false, status: "session_expired"};
  return {ok: false, status: `captcha_page_not_ready:${s2}`};
}

// Call cmd-schedule (monolithic: captcha solve + slot pick + form submit + PDF download).
// Parses the error message from _clearWorkflowFailed to classify failure type.
async function step25_schCaptchaSolve(tabId) {
  await skSet({[SK.WORKFLOW_STEP]: WF_STEPS.SCHEDULE_RECAPTCHA_READY});
  const result = await sendTabCmd(tabId, "cmd-schedule").catch(e => ({ok: false, error: e.message}));

  if (result.ok !== false) {
    await skSet({[SK.WORKFLOW_STEP]: WF_STEPS.SCHEDULE_SUBMITTED});
    return {ok: true, status: "submitted"};
  }

  const err = result.error ?? "";
  if (err.includes("no available slots") || err.includes("no valid period") || err.includes("no period options")) {
    return {ok: false, status: "slot_empty"};
  }
  if (err.includes("reCAPTCHA failed") || err.includes("captcha not found")) {
    await skSet({[SK.WORKFLOW_STEP]: WF_STEPS.SCHEDULE_RECAPTCHA_FAILED});
    return {ok: false, status: "captcha_fail"};
  }
  if (err.includes("calendar did not appear") || err.includes("calendar trigger not found")) {
    // "token rejected or no slots" ambiguity — treat as captcha failure for retry.
    await skSet({[SK.WORKFLOW_STEP]: WF_STEPS.SCHEDULE_RECAPTCHA_FAILED});
    return {ok: false, status: "captcha_fail"};
  }
  if (err.includes("booking failed") || err.includes("confirmation popup") || err.includes("previstoSubmit")) {
    await skSet({[SK.WORKFLOW_STEP]: WF_STEPS.SCHEDULE_FAILED});
    return {ok: false, status: "booking_failed"};
  }
  await skSet({[SK.WORKFLOW_STEP]: WF_STEPS.SCHEDULE_RECAPTCHA_FAILED});
  return {ok: false, status: `schedule_error:${err}`};
}

// Navigate back to the schedule page for a captcha retry. Refreshes the target URL so the
// server issues a fresh captcha challenge.
async function step26_schCaptchaFailed(tabId) {
  const schReady = _waitForPageReady(tabId, TIMER_MS.pageReady.DEFAULT);
  schReady.catch(() => {});
  await chrome.tabs.update(tabId, {url: TARGET_URL + "Agendamento"}).catch(async () => {
    await chrome.tabs.update(tabId, {url: TARGET_URL});
  });
  const {state: s} = await schReady.catch(() => ({state: "timeout"}));
  // Let step24 re-verify on next iteration.
  _runLog.entry(`step26: schedule page refreshed → state=${s}`);
}

// Slot pool is empty — return a structured result so p4 can decide to idle (w4) or throw.
async function step27_schSlotEmpty(tabId) {
  await skSet({[SK.WORKFLOW_STEP]: WF_STEPS.SCHEDULE_SLOT_EMPTY});
  return {ok: false, status: "slot_empty"};
}

// Mark slot picked milestone (handled internally by cmd-schedule; this is a state checkpoint).
async function step28_schSlotPick(tabId) {
  await skSet({[SK.WORKFLOW_STEP]: WF_STEPS.SCHEDULE_SLOT_PICKED});
  return {ok: true};
}

// Mark schedule-submitted milestone (reached inside cmd-schedule before PDF download).
async function step29_schSubmit(tabId) {
  await skSet({[SK.WORKFLOW_STEP]: WF_STEPS.SCHEDULE_SUBMITTED});
  return {ok: true};
}

// All schedule attempts exhausted — always throws.
async function step30_schFailed(tabId, status) {
  await skSet({[SK.WORKFLOW_STEP]: WF_STEPS.SCHEDULE_FAILED});
  const e = new Error(`step30: schedule failed after all attempts — ${status}`);
  e.proxyStatus = ERR_STATUS.UNKNOWN; e.nextAction = NEXT_ACTION.ROTATE_PROXY; throw e;
}

// Confirm the schedule was submitted (page state = schedule-submitted).
// The PDF download was already triggered inside cmd-schedule (visaStepSchedule).
async function step31_pdfReady(tabId) {
  let state;
  try { ({state} = await sendTabCmd(tabId, "cmd-get-state")); } catch (_) { state = "unknown"; }
  if (state !== "schedule-submitted") {
    const {state: s2} = await _waitForPageReady(tabId, TIMER_MS.pageReady.DEFAULT).catch(() => ({state: "timeout"}));
    state = s2;
  }
  if (state !== "schedule-submitted") {
    const e = new Error(`step31: expected schedule-submitted, got ${state}`);
    e.proxyStatus = ERR_STATUS.UNKNOWN; e.nextAction = NEXT_ACTION.ROTATE_PROXY; throw e;
  }
  await skSet({[SK.WORKFLOW_STEP]: WF_STEPS.PDF_READY});
  return {ok: true};
}

// PDF was already downloaded by cmd-schedule (visaStepSchedule). Mark milestone.
async function step32_pdfDownload(tabId) {
  await skSet({[SK.WORKFLOW_STEP]: WF_STEPS.PDF_DOWNLOAD});
  return {ok: true};
}

// Final completion: mark COMPLETED, send status, return success.
async function step33_completed(tabId) {
  await skSet({[SK.WORKFLOW_STEP]: WF_STEPS.COMPLETED});
  _sendStatusUpdate("DONE");
  return {ok: true, status: "completed"};
}

//#endregion

//#region PhaseHandlers
// Each phase handler orchestrates step handlers for one workflow milestone.
// Phases are reusable across workflows:
//   wf_register → p0 + p1
//   wf_login    → p0 + p2 + w2 (idle) | p0 + p2 + p3 + w3 (monitor)
//   wf_apply    → p3 (if from login idle) + p4 + p5
//
// Phase map:
//   p0_openAuthPage   → [step0_warmup] → step1_homeReady → step2_authReady
//   w1_atAuth         → keep-alive at auth page (rare manual-step scenario)
//   p1_registerAccount→ F2: reg form open → captcha submit → email token verify
//   p2_loginAccount   → F3: login form fill → submit → logged-in
//   w2_atIndex        → F6 keep-alive at index; parks until wf_apply is signalled
//   p3_fillForm       → F4 (apply mode) | questionnaire + fill-tabs (monitor mode)
//   w3_atForm         → arm post monitor + F6 keep-alive; parks until signal
//   p4_scheduleSlot   → F5: reCAPTCHA solve → slot pick → submit
//   w4_atSlot         → F6 keep-alive at schedule (external slot signal)
//   p5_downloadPDF    → PDF ready → download

// opts.skipWarmup — bypass step0 even when SK.PRE_VISIT_WARMUP is set (e.g. fresh-tab retry).
async function p0_openAuthPage(tabId, opts = {}) {
  const {[SK.PRE_VISIT_WARMUP]: preWarmup = false} = await skGet(SK.PRE_VISIT_WARMUP);
  if (preWarmup && !opts.skipWarmup) await step0_warmup(tabId);
  const state = await step1_homeReady(tabId);
  // Already logged in or deeper — caller decides whether to continue or skip p2.
  if (state !== "not-logged-in") return {ok: true, status: state};
  await step2_authReady(tabId);
  return {ok: true, status: "auth"};
}

// Idle at auth page with keep-alives. Rarely used (manual step pending before login/register).
// Sets WARMUP_IDLE_STATE="auth" so wf_apply can detect and re-login if triggered.
async function w1_atAuth(tabId) {
  await skSet({[SK.WARMUP_IDLE_STATE]: "auth"});
  p6_keepSession(tabId);
  _sendStatusUpdate("WAITING_AT_AUTH");
}

// Fill and submit registration form, verify email token.
// Crash-recovery: if stored step is already in the auth_token bucket, skip form stages and
// resume directly at the email-poll wait.
async function p1_registerAccount(tabId, person, emailAcct) {
  const {step: curStep} = await _currentWfStep(tabId);

  if (STEP_BUCKETS.auth_token.has(curStep)) {
    _runLog.entry("p1: resuming at token page — skipping form stages");
    const {[SK.EMAIL_CODE_TOKEN]: existingCode} = await skGet(SK.EMAIL_CODE_TOKEN);
    if (!existingCode) _startEmailPoll(emailAcct.jwt, tabId);
    return await step10_regVerify(tabId, person, emailAcct);
  }

  await step3_regFormReady(tabId);

  const r4 = await step4_regFormSubmit(tabId, person, emailAcct);
  let tokenPageReady = r4.tokenPageReady;

  if (!r4.ok) {
    // step4 exhausted both internal captcha attempts — try one external retry cycle.
    const r5 = await step5_regFormReady2(tabId, person, emailAcct);
    if (!r5.ok) return await step7_regFormFailed2(tabId, r5.status);
    const r6 = await step6_regFormSubmit2(tabId, emailAcct);
    if (!r6.ok) return await step7_regFormFailed2(tabId, r6.status);
    tokenPageReady = r6.tokenPageReady;
  }

  await step8_regVerifyReady(tabId, tokenPageReady);
  return await step10_regVerify(tabId, person, emailAcct);
}

// Fill and submit login form. Tries once (step11), recovers to auth page (step12), retries
// once more (step13). Throws on final failure so the workflow handler catches it.
async function p2_loginAccount(tabId, creds) {
  const r11 = await step11_loginSubmit(tabId, creds);

  if (!r11.ok) {
    const r12 = await step12_loginReady2(tabId, creds);
    if (!r12.ok) return await step14_loginFailed2(tabId, r12.status);
    const r13 = await step13_loginSubmit2(tabId);
    if (!r13.ok) return await step14_loginFailed2(tabId, r13.status);
  }

  return await step15_loginSuccess(tabId);
}

// Idle at logged-in index with keep-alives until wf_apply is triggered externally.
// Session recovery on wf_apply: see _recoverSession(). F6 keep-ticks scroll the page,
// which prevents the server session from expiring due to inactivity.
async function w2_atIndex(tabId) {
  p6_keepSession(tabId);
  _sendStatusUpdate("READY_FOR_APPLY_IDLE");
  await _runLog.finish("ok idleStep=login");
}

// Fill form from logged-in index to either:
//   monitorMode=true  → questionnaire + form fill, stop here (w3 arms the monitor)
//   monitorMode=false → questionnaire + form fill + consular-post signal + form submit → schedule page
//
// Crash-recovery: _currentWfStep() determines the entry point based on URL bucket.
// Session-expiry recovery: re-logins using stored credentials and retries from questionnaire.
async function p3_fillForm(tabId, opts = {}) {
  const {step: curStep} = await _currentWfStep(tabId);

  // Already on schedule page — nothing left to do in p3.
  if (STEP_BUCKETS.schedule.has(curStep)) {
    _runLog.entry("p3: resuming — already on schedule page");
    return {ok: true, status: "form-ready"};
  }

  // ── Questionnaire phase ───────────────────────────────────────────────────
  const _skipQuestionnaire = STEP_BUCKETS.form.has(curStep);
  if (!_skipQuestionnaire) {
    const MAX_Q = 3;
    for (let attempt = 1; attempt <= MAX_Q; attempt++) {
      if (attempt > 1) {
        _runLog.entry(`p3: questionnaire retry ${attempt}`);
        const homeReady = _waitForPageReady(tabId, TIMER_MS.pageReady.DEFAULT);
        homeReady.catch(() => {});
        await chrome.tabs.update(tabId, {url: TARGET_URL});
        await homeReady.catch(() => {});
      }

      const r16 = await step16_queryReady(tabId);
      if (!r16?.ok && r16?.status === "session_expired") {
        await _reloginFromStorage(tabId);
        continue;
      }

      // skip step17 if already on form
      let fState;
      try { ({state: fState} = await sendTabCmd(tabId, "cmd-get-state")); } catch(_) { fState = "unknown"; }
      if (fState === "form") break;

      const r17 = await step17_querySubmit(tabId);
      if (r17.ok) break;

      if (r17.status === "session_expired") { await _reloginFromStorage(tabId); continue; }
      if (attempt === MAX_Q) await step18_queryFailed(tabId, r17.status); // throws
    }
  } else {
    _runLog.entry("p3: resuming — already on form page, skipping questionnaire");
  }

  // ── Form-ready confirmation ───────────────────────────────────────────────
  const r19 = await step19_reqFormReady(tabId);
  if (!r19?.ok && r19?.status === "session_expired") {
    await _reloginFromStorage(tabId);
    // After re-login return to start of questionnaire (safe fallback).
    return await p3_fillForm(tabId, opts);
  }

  // ── Form fill ─────────────────────────────────────────────────────────────
  await step20_reqFormFill(tabId);

  if (opts.monitorMode) {
    _runLog.entry("p3: form filled (monitor mode) — handing off to w3");
    return {ok: true, status: "form-filled"};
  }

  // ── Full apply: signal + submit ───────────────────────────────────────────
  const r21 = await step21_reqFormSignal(tabId);
  if (!r21.ok) {
    const e = new Error(`p3: consular post not available — ${r21.status}`);
    e.proxyStatus = ERR_STATUS.UNKNOWN; e.nextAction = NEXT_ACTION.ROTATE_PROXY; throw e;
  }

  const MAX_SUBMIT = 2;
  for (let attempt = 1; attempt <= MAX_SUBMIT; attempt++) {
    const r22 = await step22_reqFormSubmit(tabId);
    if (r22.ok) return {ok: true, status: "form-ready"};
    if (r22.status === "session_expired") {
      await _reloginFromStorage(tabId);
      return await p3_fillForm(tabId, opts); // restart from top
    }
    if (attempt === MAX_SUBMIT) await step23_reqFormFailed(tabId, r22.status); // throws
    _runLog.entry(`p3: form submit ${r22.status} — retrying (${attempt}/${MAX_SUBMIT})`);
  }
}

// Arm the post-availability monitor and park with keep-alives.
// When content.js detects the target post, it fires target-post-available → MSG handler →
// AUTO_TRIGGER: wf_apply() | EXTERNAL_SIGNAL: notify manager, which sends signal-apply.
// Session recovery: F6 keep-ticks keep the session alive. If session expires between
// w3 and the incoming wf_apply trigger, _recoverSession() in wf_apply re-logs in.
async function w3_atForm(tabId, opts = {}) {
  await skSet({
    [SK.TRIGGER_MODE]:    opts.triggerMode ?? "AUTO_TRIGGER",
    [SK.TARGET_POST_ID]:  String(opts.consulPost ?? ""),
    [SK.WARMUP_IDLE_STATE]: "form-monitor",
  });
  _applyTriggerLock = false;
  _waitingForSignal  = false;
  await sendTabCmd(tabId, "cmd-start-post-monitor").catch(() => {});
  p6_keepSession(tabId);
  _sendStatusUpdate("READY_FOR_APPLY_IDLE");
  await _runLog.finish("ok idleStep=form-monitor");
}

// Solve reCAPTCHA, pick a slot, submit the schedule form, and download the PDF.
// cmd-schedule (visaStepSchedule) handles captcha+slot+submit+PDF download monolithically.
// Step handlers 24–29 track milestones and handle retry / failure classification.
// Returns {ok:true} on success. Returns {ok:false, status:"slot_empty"} for empty pool (w4 idles).
// Throws on unrecoverable errors (captcha repeatedly failed, booking rejected).
async function p4_scheduleSlot(tabId, config = {}) {
  const MAX_SCH = 3;

  for (let attempt = 1; attempt <= MAX_SCH; attempt++) {
    const r24 = await step24_schCaptchaReady(tabId);
    if (!r24?.ok && r24?.status === "session_expired") {
      await _reloginFromStorage(tabId);
      const pageReady = _waitForPageReady(tabId, TIMER_MS.pageReady.DEFAULT);
      pageReady.catch(() => {});
      continue;
    }
    if (!r24?.ok) {
      if (attempt === MAX_SCH) await step30_schFailed(tabId, r24?.status ?? "captcha_page_not_ready");
      await step26_schCaptchaFailed(tabId);
      continue;
    }

    const r25 = await step25_schCaptchaSolve(tabId);

    if (r25.ok) {
      await step29_schSubmit(tabId); // milestone marker
      return {ok: true, status: "submitted"};
    }

    if (r25.status === "slot_empty") {
      return await step27_schSlotEmpty(tabId); // returns {ok:false, status:"slot_empty"}
    }

    _runLog.entry(`p4: schedule attempt ${attempt}/${MAX_SCH} failed — ${r25.status}`);
    if (attempt === MAX_SCH) await step30_schFailed(tabId, r25.status); // throws

    await step26_schCaptchaFailed(tabId); // navigate to fresh schedule page
  }
}

// Idle at schedule page with keep-alives (waiting for an external slot-availability signal).
// When wf_apply is triggered externally with idleState="schedule", p4 will run again from
// the schedule page. Session recovery is handled by _recoverSession() in wf_apply.
async function w4_atSlot(tabId) {
  await skSet({[SK.WARMUP_IDLE_STATE]: "schedule"});
  p6_keepSession(tabId);
  _sendStatusUpdate("READY_FOR_APPLY_IDLE");
  await _runLog.finish("ok idleStep=schedule");
}

// Confirm PDF was downloaded (cmd-schedule/visaStepSchedule handles the actual download).
// step31 verifies schedule-submitted state, step32+33 mark milestones and send DONE.
async function p5_downloadPDF(tabId) {
  await step31_pdfReady(tabId);
  await step32_pdfDownload(tabId);
  return await step33_completed(tabId);
}

let _p6Interval            = null;

function p6_keepSession(tabId) {
  if (_p6Interval) clearTimeout(_p6Interval);
  // Use a truly random interval (60–180s) with no fixed base per idle state.
  // Fixed base values (15/40/90s ± jitter) created a statistically detectable
  // pattern — statistical analysis of request inter-arrival times would flag it.
  function _nextMs() { return TIMER_MS.keepAlive.PING_MIN + Math.random() * TIMER_MS.keepAlive.PING_MARGIN; } // 1–3 min, uniform
  const _f6Tick = () => {
    sendTabCmd(tabId, "cmd-keep-tick").catch(() => {});
    if (_p6Interval) {
      clearTimeout(_p6Interval);
      _p6Interval = setTimeout(_f6Tick, _nextMs());
    }
  };
  _p6Interval = setTimeout(_f6Tick, _nextMs());
}

function p6_stop() {
  if (_p6Interval) { clearTimeout(_p6Interval); _p6Interval = null; }
}

//#endregion

//#region WorkflowHandlers
// Each workflow handler maps one user-facing operation to a sequence of phase handlers.
//
//   wf_register(realPerson)                → p0 + p1
//   wf_login(username, password, idle)     → p0 + p2 + w2  (idle=true)
//                                         → p0 + p2 + p3 + w3  (idle=false, monitor mode)
//   wf_apply()                             → [p3] + p4 + p5  (entry depends on idle state)
//
// Popup message → workflow handler:
//   MSG.LOGIN_IDLE    → wf_login(…, idle=true)
//   MSG.LOGIN_APPLY   → wf_login(…, idle=false) → wf_apply()
//   MSG.REGISTER_ONLY → wf_register(realPerson)
//   MSG.REGISTER_LOGIN→ wf_register(realPerson) → wf_login(null, null, true)
//   MSG.REGISTER_APPLY→ wf_register(realPerson) → wf_login(null, null, false) → wf_apply()

async function wf_register(realPerson = null) {
  let tabId;
  try {
    _startSwKeepalive();
    p6_stop();
    await _resetWorkflow();
    let person;
    if (realPerson && (realPerson.firstName || realPerson.name)) {
      const gRaw = String(realPerson.gender ?? "").trim().toUpperCase();
      const firstName = String(realPerson.firstName ?? realPerson.name ?? "").trim();
      const lastName  = String(realPerson.lastName  ?? realPerson.surname ?? "").trim();
      const {username, password, emailLocal} = _genCredentials(firstName, lastName);
      person = {
        name:        firstName,
        surname:     lastName,
        username,
        password,
        email:       emailLocal,
        birth_date:  _parseDOB(realPerson.dob ?? realPerson.birth_date),
        gender:      gRaw === "F" || gRaw === "FEMALE" || gRaw === "FEMININO" ? "F" : "M",
        nationality: String(realPerson.nationality ?? "CPV").trim(),
        traveldoc:   String(realPerson.traveldoc  ?? "").trim(),
      };
    } else {
      person = generatePerson();
    }
    _runLog.start(realPerson ? "register_real" : "register_fake");
    _runLog.entry(`person: ${person.name} ${person.surname} ${person.nationality} ${person.traveldoc}`);

    const emailAcct = await createTempEmail(person.email);
    _runLog.entry(`email: ${emailAcct.email}`);

    await skSet({
      [SK.USERNAME]:         person.username,
      [SK.PASSWORD]:         person.password,
      [SK.REGISTER_PERSON]:  person,
      [SK.REGISTER_EMAIL]:   emailAcct,
      [SK.EMAIL_TOKEN]:      null,
      [SK.EMAIL_CODE_TOKEN]: null,
    });

    tabId = await _createSessionTab();
    await p0_openAuthPage(tabId);
    await sendTabCmd(tabId, "cmd-log-ip").catch(() => {});
    const r = await p1_registerAccount(tabId, person, emailAcct);

    await _runLog.finish(`ok status=${r.status}`);
    self.Comm?.send({type: "register-done", ...r, email: emailAcct.email, username: person.username, password: person.password});
  } catch(e) {
    await _runLog.finish(`error: ${e.message ?? String(e)}`);
    self.Comm?.send({type: "error", reason: e.message ?? String(e), proxyStatus: e.proxyStatus ?? null, nextAction: e.nextAction ?? null});
  } finally {
    _stopSwKeepalive();
    if (tabId) chrome.tabs.remove(tabId).catch(() => {});
  }
}

// idleOnLoggedIn=true  → park at logged-in index (w2) waiting for external wf_apply signal.
// idleOnLoggedIn=false → fill form in monitor mode (p3+w3) waiting for post-available signal.
async function wf_login(username, password, idleOnLoggedIn = false) {
  if (!username || !password) {
    const stored = await skGet(SK.USERNAME, SK.PASSWORD);
    username = stored[SK.USERNAME];
    password = stored[SK.PASSWORD];
    if (!username || !password) throw new Error("wf_login: credentials not found in storage");
  }

  _runLog.start(`login_${idleOnLoggedIn ? "idle" : "monitor"}`);
  _runLog.entry(`username=${username}`);
  _startSwKeepalive();
  p6_stop();
  await _resetWorkflow();
  const tabId = await _createSessionTab();
  await skSet({
    [SK.USERNAME]:          username,
    [SK.PASSWORD]:          password,
    [SK.WARMUP_TAB_ID]:     tabId,
    [SK.WARMUP_IDLE_STATE]: idleOnLoggedIn ? "login" : "form-monitor",
  });

  try {
    await p0_openAuthPage(tabId);
    await p2_loginAccount(tabId, {username, password});

    if (idleOnLoggedIn) {
      await w2_atIndex(tabId);    // logs finish + sets keepalive; parks until wf_apply
    } else {
      const {
        [SK.TRIGGER_MODE]:       triggerMode,
        [SK.VISA_CONSULAR_POST]: consulPost,
      } = await skGet(SK.TRIGGER_MODE, SK.VISA_CONSULAR_POST);
      await p3_fillForm(tabId, {monitorMode: true});
      await w3_atForm(tabId, {
        triggerMode: triggerMode ?? "AUTO_TRIGGER",
        consulPost:  consulPost  ?? "",
      });
    }
  } catch(e) {
    await _runLog.finish(`error: ${e.message ?? String(e)}`);
    _stopSwKeepalive();
    chrome.tabs.remove(tabId).catch(() => {});
    throw e;
  }
}

// Re-login using stored credentials (SK.USERNAME / SK.PASSWORD) after a session expiry.
// Called by p3, wf_apply, and any other phase that detects auth redirect during idle.
async function _reloginFromStorage(tabId) {
  const {[SK.USERNAME]: username, [SK.PASSWORD]: password} = await skGet(SK.USERNAME, SK.PASSWORD);
  if (!username || !password) {
    const e = new Error("session recovery: no stored credentials");
    e.proxyStatus = ERR_STATUS.UNKNOWN; e.nextAction = NEXT_ACTION.CHANGE_DEVICE; throw e;
  }
  _runLog.entry("session recovery: re-logging in with stored credentials");
  await p0_openAuthPage(tabId, {skipWarmup: true});
  await p2_loginAccount(tabId, {username, password});
}

// Verify the idle tab is still alive and the session is valid. If not, re-logins and
// re-navigates to the page expected for the given idleState.
// Called at the top of wf_apply before any phase handler runs.
async function _recoverSession(tabId, idleState) {
  const tab = await chrome.tabs.get(tabId).catch(() => null);
  if (!tab) {
    const e = new Error(`wf_apply: idle tab ${tabId} no longer exists`);
    e.proxyStatus = ERR_STATUS.UNKNOWN; e.nextAction = NEXT_ACTION.ROTATE_PROXY; throw e;
  }

  let state;
  try { ({state} = await sendTabCmd(tabId, "cmd-get-state")); } catch (_) { state = "unknown"; }
  _runLog.entry(`wf_apply: session check — idleState=${idleState} pageState=${state}`);

  const sessionLost = state === "not-logged-in" || state === "auth" || state === "unknown";
  if (!sessionLost) return; // session still valid — proceed normally

  _runLog.entry("wf_apply: session expired — recovering");
  await _reloginFromStorage(tabId);

  // After re-login, restore the page to the correct starting point for this idleState.
  if (idleState === "form-monitor" || idleState === "form") {
    // Re-fill form in monitor mode so step22 can submit immediately.
    await p3_fillForm(tabId, {monitorMode: true});
  } else if (idleState === "schedule") {
    // Can't restore schedule page without re-submitting the form.
    await p3_fillForm(tabId, {monitorMode: false});
    // p3 in full mode will run step21+step22 and land on schedule. Update idleState.
    await skSet({[SK.WARMUP_IDLE_STATE]: "schedule"});
  }
  // "login" idleState: logged in at index — p3 (full mode) handles the rest in wf_apply.
}

// Resume from the current idle state and run the apply pipeline.
// Entry point is read from SK.WARMUP_IDLE_STATE:
//   "login"              → session check → p3 (questionnaire+form+signal+submit) + p4 + p5
//   "form"/"form-monitor"→ session check → step22 (form submit) + p4 + p5
//   "schedule"           → session check → p4 + p5
async function wf_apply() {
  _runLog.start("apply");
  _startSwKeepalive();
  p6_stop();
  const {
    [SK.WARMUP_IDLE_STATE]: idleState = "login",
    [SK.WARMUP_TAB_ID]:     tabId,
  } = await skGet(SK.WARMUP_IDLE_STATE, SK.WARMUP_TAB_ID);
  const _tabId = tabId ?? await _getActiveTab();
  _runLog.entry(`wf_apply: idleState=${idleState}`);

  try {
    // Session recovery: verify tab is alive and session is valid before running any phase.
    if (tabId) await _recoverSession(_tabId, idleState);

    if (idleState === "login") {
      await p3_fillForm(_tabId);          // questionnaire + form fill + signal + submit → schedule
    } else if (idleState === "form" || idleState === "form-monitor") {
      const r22 = await step22_reqFormSubmit(_tabId);
      if (!r22.ok) {
        if (r22.status === "session_expired") {
          await _reloginFromStorage(_tabId);
          await p3_fillForm(_tabId, {monitorMode: false});
        } else {
          const e = new Error(`wf_apply: form submit failed — ${r22.status}`);
          e.proxyStatus = ERR_STATUS.UNKNOWN; e.nextAction = NEXT_ACTION.ROTATE_PROXY; throw e;
        }
      }
    } else if (idleState !== "schedule") {
      throw new Error(`wf_apply: unknown idleState "${idleState}"`);
    }

    const r4 = await p4_scheduleSlot(_tabId, {});
    if (!r4?.ok && r4?.status === "slot_empty") {
      // No slots available — idle at schedule page and wait for a fresh trigger.
      await w4_atSlot(_tabId);
      return; // w4 called _runLog.finish; don't remove WARMUP_TAB_ID (tab stays open)
    }

    await p5_downloadPDF(_tabId);
    await _runLog.finish("ok status=completed");
  } catch(e) {
    await _runLog.finish(`error: ${e.message ?? String(e)}`);
    throw e;
  } finally {
    _stopSwKeepalive();
    // Don't remove tab / idle state when w4 is active (w4 already called _runLog.finish above).
    const {[SK.WARMUP_IDLE_STATE]: postState} = await skGet(SK.WARMUP_IDLE_STATE);
    if (postState !== "schedule") {
      chrome.tabs.remove(_tabId).catch(() => {});
      await skRemove(SK.WARMUP_IDLE_STATE, SK.WARMUP_TAB_ID);
    }
  }
}

//#endregion

//#region ContentInteractions
// TODO: split this region into two:
//   • ContentInteractions  → _pageReadyResolvers, _waitForPageReady, _domStateToStep, _currentWfStep
//   • StepHandlers         → WARMUP_URL_POOL, _pickWarmupUrls  (append to existing StepHandlers region)
//
// Left here temporarily so git history stays clean and the region refactor can be
// reviewed as a separate commit.

// Per-tab page-ready resolvers — replaces the three scalar globals that only allowed
// one concurrent waitForPageReady at a time.
const _pageReadyResolvers = new Map(); // tabId → {resolve, reject, timer}

function _waitForPageReady(tabId, timeoutMs = TIMER_MS.pageReady.DEFAULT) {
  return new Promise((resolve, reject) => {
    const prev = _pageReadyResolvers.get(tabId);
    if (prev?.timer) clearTimeout(prev.timer);
    const timer = setTimeout(() => {
      _pageReadyResolvers.delete(tabId);
      const err = new Error("page-ready timeout");
      err.proxyStatus = ERR_STATUS.PROXY_SLOW;
      err.nextAction  = NEXT_ACTION.ROTATE_PROXY;
      reject(err);
    }, timeoutMs);
    _pageReadyResolvers.set(tabId, {
      resolve: (data) => { clearTimeout(timer); _pageReadyResolvers.delete(tabId); resolve(data); },
      reject:  (err)  => { clearTimeout(timer); _pageReadyResolvers.delete(tabId); reject(err);  },
      timer,
    });
  });
}

// Returns {step: WF_STEPS integer, tabId}.
// Gathers cur_page + last_step + page_state, then delegates to resolveStep() (constants.js).
// Idempotent: reads only, never writes. Always resolves (never throws).
// Maps the coarse string returned by content.js _detectPageState() to a WF_STEPS integer.
// Used only when storage has no valid step for the current URL bucket (crash/restart recovery).
// "session-lost" means the page shell loaded but the server has no auth session — treat as HOME_READY
// so the orchestrator re-authenticates from the beginning rather than continuing mid-flow.
function _domStateToStep(domState) {
  switch (domState) {
    case "not-logged-in":      return WF_STEPS.HOME_READY;
    case "logged-in":          return WF_STEPS.LOGIN_SUCCESS;
    case "auth":               return WF_STEPS.AUTH_READY;
    case "reg-form":           return WF_STEPS.REG_FORM_READY;
    case "token":              return WF_STEPS.REG_VERIFY_READY;
    case "questionnaire":      return WF_STEPS.QUERY_READY;
    case "form":               return WF_STEPS.REQ_FORM_READY;
    case "schedule":           return WF_STEPS.SCHEDULE_RECAPTCHA_READY;
    case "schedule-captcha":   return WF_STEPS.SCHEDULE_RECAPTCHA_READY;
    case "schedule-calendar":  return WF_STEPS.SCHEDULE_RECAPTCHA_SOLVED;
    case "schedule-submitted": return WF_STEPS.SCHEDULE_SUBMITTED;
    case "session-lost":       return WF_STEPS.HOME_READY;
    default:                   return null;
  }
}

// Returns {step: WF_STEPS integer, tabId}.
// Guarantees tabId is always a non-hub-init tab (never a worker-init URL).
// Resolution order: passed tabId → _activeTabId → query TARGET_HOST tabs → create new tab.
// Hot path (active workflow): stored step is valid for the bucket → return immediately, no IPC.
// Cold path (crash recovery / first call): stored step is stale/missing → query content.js DOM
// state for a finer starting point than the coarse bucket default.
// Always resolves — never throws.
async function _currentWfStep(tabId = null) {
  // ── Resolve a valid non-hub-init tab ───────────────────────────────────────
  const _isHubTab = url => (url ?? "").includes("worker-init");
  let resolvedId = null;

  // 1. Validate caller-supplied tabId.
  if (tabId != null) {
    try {
      const t = await chrome.tabs.get(tabId);
      if (!_isHubTab(t.url)) resolvedId = tabId;
    } catch (_) {}
  }

  // 2. Fall back to the cached active tab id.
  if (resolvedId == null && _activeTabId != null) {
    try {
      const t = await chrome.tabs.get(_activeTabId);
      if (!_isHubTab(t.url)) resolvedId = _activeTabId;
    } catch (_) {}
  }

  // 3. Scan open tabs for any target-host tab (SW restart, no in-memory state).
  if (resolvedId == null) {
    const [found] = await chrome.tabs.query({url: `*://${TARGET_HOST}/*`});
    if (found && !_isHubTab(found.url)) resolvedId = found.id;
  }

  // 4. Nothing found — create a new target tab and track it.
  if (resolvedId == null) {
    const tab = await chrome.tabs.create({url: TARGET_URL, active: false});
    resolvedId = tab.id;
  }

  // Persist whichever tab we resolved so subsequent calls don't re-scan.
  if (resolvedId !== _activeTabId) {
    _activeTabId = resolvedId;
    skSet({[SK.ACTIVE_TAB_ID]: resolvedId});
  }

  // ── Classify URL and determine step ────────────────────────────────────────
  try {
    const [tab, stored] = await Promise.all([
      chrome.tabs.get(resolvedId),
      skGet(SK.WORKFLOW_STEP),
    ]);
    const url      = new URL(tab.url ?? "about:blank");
    const bucket   = classifyUrlPath(url.pathname, url.search);
    const lastStep = stored[SK.WORKFLOW_STEP] ?? null;

    // Stored step is valid for this bucket — authoritative, no DOM query needed.
    if (lastStep !== null && STEP_BUCKETS[bucket]?.has(lastStep)) {
      return { step: lastStep, tabId: resolvedId };
    }

    // Storage is stale or missing — ask content.js for DOM-observable state.
    let step = BUCKET_DEFAULT_STEP[bucket] ?? WF_STEPS.NONE;
    try {
      const { state: domState } = await sendTabCmd(resolvedId, "cmd-get-state");
      const domStep = _domStateToStep(domState);
      if (domStep !== null) step = domStep;
    } catch (_) {
      // Content script not yet injected or tab not responding — keep bucket default.
    }

    return { step, tabId: resolvedId };
  } catch (_) {
    return { step: WF_STEPS.NONE, tabId: resolvedId };
  }
}

const WARMUP_URL_POOL = [
  "https://en.wikipedia.org/wiki/Visa_policy_of_Portugal",
  "https://en.wikipedia.org/wiki/Schengen_Area",
  "https://en.wikipedia.org/wiki/Portugal",
  "https://www.bbc.com/news/world-europe",
  "https://www.bbc.com/travel/article/20200127-visiting-portugal",
  "https://www.reuters.com/world/europe/",
  "https://www.theguardian.com/world/europe-news",
  "https://en.wikipedia.org/wiki/Visa_requirements_for_Cape_Verdean_citizens",
]

function _pickWarmupUrls() {
  const pool = WARMUP_URL_POOL.slice();
  const a = Math.floor(Math.random() * pool.length);
  pool.splice(a, 1);
  const b = Math.floor(Math.random() * pool.length);
  return [WARMUP_URL_POOL[a], pool[b]];
}

//#endregion

async function F0_warmupBrowse(tabId) {
  await step0_warmup(tabId);
  _runLog.entry("F0: warmup browse done");
}

async function F1_openAuthPage(tabId) {
  let state;
  try { ({state} = await sendTabCmd(tabId, "cmd-get-state")); } catch (_) { state = "unknown"; }

  // Already on auth page — login form or AJAX-opened reg form.
  if (state === "auth" || state === "reg-form") return {ok: true, status: "ready"};

  // Already past auth — logged in or deeper into the workflow.
  // Callers (F3, F4) have their own entry-point checks for these states.
  if (state === "logged-in" || state === "token" ||
      state === "questionnaire" || state === "form" ||
      state === "schedule" || state === "schedule-captcha" ||
      state === "schedule-calendar" || state === "schedule-submitted") {
    return {ok: true, status: "past-auth"};
  }

  if (state !== "not-logged-in") {
    // Set up listener BEFORE navigation so page-ready can't arrive before we're listening.
    // 120s: each bd.js challenge dwell is 20s; 2 challenges + redirects + page loads ≈ 60–90s.
    // The old 35s was too tight — bd1623aa showed 2 challenges consuming ~34s on a fast session.
    const pageReady = _waitForPageReady(tabId, TIMER_MS.pageReady.SSO);
    await chrome.tabs.update(tabId, {url: TARGET_URL});
    ({state} = await pageReady);
  }

  if (state === "waf-challenge") {
    // content.js already waited 20s for bd.js to auto-redirect — it didn't.
    const e = new Error("F1: WAF bot-challenge not resolved — IP or fingerprint flagged");
    e.proxyStatus = ERR_STATUS.BLOCKED; e.nextAction = NEXT_ACTION.ROTATE_PROXY; throw e;
  }

  // waf-challenge-active = challenge is in progress; auto-redirect may still happen.
  // Re-arm a fresh long wait so we don't miss the redirect completing.
  if (state === "waf-challenge-active") {
    const pr2 = _waitForPageReady(tabId, TIMER_MS.pageReady.SSO);
    state = (await pr2.catch(() => ({state: "timeout"}))).state ?? "timeout";
    if (state === "waf-challenge") {
      const e = new Error("F1: WAF bot-challenge not resolved after extended wait");
      e.proxyStatus = ERR_STATUS.BLOCKED; e.nextAction = NEXT_ACTION.ROTATE_PROXY; throw e;
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
      await new Promise(r => setTimeout(r, TIMER_MS.auth.RETRY_BASE + attempt * TIMER_MS.auth.RETRY_STEP));
      try { ({state} = await sendTabCmd(tabId, "cmd-get-state")); } catch (_) { state = "unknown"; }
      if (state === "auth") return {ok: true, status: "ready"};
      // Don't re-navigate if we're off-site (SSO in progress) — just re-check state.
      if (state !== "not-logged-in" && state !== "off-site") {
        const pr = _waitForPageReady(tabId, TIMER_MS.pageReady.SSO);
        await chrome.tabs.update(tabId, {url: TARGET_URL});
        ({state} = await pr);
        if (state === "auth") return {ok: true, status: "ready"};
        if (state === "waf-challenge") {
          const e = new Error("F1: WAF bot-challenge — IP or fingerprint flagged");
          e.proxyStatus = ERR_STATUS.BLOCKED; e.nextAction = NEXT_ACTION.ROTATE_PROXY; throw e;
        }
        if (state === "waf-challenge-active") {
          // bd.js is running — wait for its auto-redirect
          const prWaf = _waitForPageReady(tabId, TIMER_MS.pageReady.SSO);
          state = (await prWaf.catch(() => ({state: "timeout"}))).state ?? "timeout";
          if (state === "auth") return {ok: true, status: "ready"};
          if (state === "waf-challenge") {
            const e = new Error("F1: WAF bot-challenge — IP or fingerprint flagged");
            e.proxyStatus = ERR_STATUS.BLOCKED; e.nextAction = NEXT_ACTION.ROTATE_PROXY; throw e;
          }
        }
      }
    }

    // Language switch may reload the page — 15 s gives ample room past the reload time.
    // Skipped entirely when lang-switch-enabled is false (default) to avoid the 15 s wait.
    const {[SK.LANG_SWITCH_ENABLED]: _langEnabled = false} =
      await skGet(SK.LANG_SWITCH_ENABLED);
    if (_langEnabled) {
      const langCmd = sendTabCmd(tabId, "cmd-switch-lang").catch(() => {});
      const langReady = _waitForPageReady(tabId, TIMER_MS.pageReady.LANG_SWITCH);
      await langCmd;
      await langReady.catch(() => {}); // ignore timeout (already English) or navigation
    }

    // Confirm still on not-logged-in before arming the login listener.
    try { ({state} = await sendTabCmd(tabId, "cmd-get-state")); } catch (_) { state = "unknown"; }
    if (state === "auth") return {ok: true, status: "ready"};

    // Login-link click always navigates — arm listener first.
    // If the site uses SSO (off-site auth), the click navigates to another domain and
    // page-ready never fires.  In that case wait longer for the SSO to redirect back.
    const authReady = _waitForPageReady(tabId, TIMER_MS.pageReady.AUTH_CLICK);
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
        const ssoReady = _waitForPageReady(tabId, TIMER_MS.pageReady.SSO);
        ({state: authState} = await ssoReady);
        if (authState === "auth") return {ok: true, status: "ready"};
        if (authState === "not-logged-in") {
          state = authState; // let the retry loop click login again
          continue;
        }
        if (authState === "waf-challenge") {
          const _e = new Error("F1: WAF bot-challenge — IP or fingerprint flagged");
          _e.proxyStatus = ERR_STATUS.BLOCKED; _e.nextAction = NEXT_ACTION.ROTATE_PROXY; throw _e;
        }
      } catch (_) {
        authState = "sso-timeout";
      }
    }

    state = authState;
    _runLog.entry(`F1: attempt ${attempt + 1} got state=${state} — retrying`);
  }
  const _f1e = new Error(`F1: expected auth, got ${state}`);
  _f1e.proxyStatus = ERR_STATUS.UNKNOWN; _f1e.nextAction = NEXT_ACTION.ROTATE_PROXY; throw _f1e;
}

async function F2_register(tabId, person, emailAcct) {
  _runLog.entry(`F2: person=${person.name} ${person.surname} email=${emailAcct.email}`);

  const { step: _f2Step } = await _currentWfStep(tabId);
  const _skipToTokenPage = STEP_BUCKETS.auth_token.has(_f2Step);

  if (!_skipToTokenPage) {
    // ── Form open + fill ──────────────────────────────────────────────────────
    // Retry loop: on fill failure navigate back to home for a fresh form.
    // Does NOT burn retryCount — fill errors are transient, not proxy burns.
    const MAX_FORM_ATTEMPTS = 3;
    for (let _fa = 1; _fa <= MAX_FORM_ATTEMPTS; _fa++) {
      if (_fa > 1) {
        _runLog.entry(`F2: form retry ${_fa} — navigating home for a fresh form`);
        const _homeReady = _waitForPageReady(tabId, TIMER_MS.pageReady.DEFAULT);
        _homeReady.catch(() => {});
        await chrome.tabs.update(tabId, {url: TARGET_URL});
        await _homeReady.catch(() => {});
      }

      // Pre-arm page-ready BEFORE the link click. If the click causes a full navigation
      // the old context dies (channel close) and this listener catches the new page signal
      // without a gap.
      const regPageReady = _waitForPageReady(tabId, TIMER_MS.pageReady.REG_FORM);
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
        const _os = openResult?.status ?? openResult?.error ?? "form_open_failed";
        _runLog.entry(`F2: open failed — ${_os} (attempt ${_fa}/${MAX_FORM_ATTEMPTS})`);
        if (_fa < MAX_FORM_ATTEMPTS) continue; // navigate home and retry
        const _oe = new Error(`F2: ${_os} after ${MAX_FORM_ATTEMPTS} attempts`);
        _oe.proxyStatus = ERR_STATUS.PROXY_SLOW; _oe.nextAction = NEXT_ACTION.ROTATE_PROXY; throw _oe;
      }
      _runLog.entry(`F2: form opened (attempt ${_fa})`);

      const fillResult = await sendTabCmd(tabId, "cmd-register-fill", {person, email: emailAcct.email});
      if (fillResult?.ok) break;

      const _fs = fillResult?.status ?? "form_incomplete";
      _runLog.entry(`F2: fill ${_fs} (attempt ${_fa}/${MAX_FORM_ATTEMPTS})`);
      if (_fa === MAX_FORM_ATTEMPTS) {
        const _fe = new Error(`F2: ${_fs} after ${MAX_FORM_ATTEMPTS} attempts`);
        _fe.proxyStatus = ERR_STATUS.PROXY_SLOW; _fe.nextAction = NEXT_ACTION.ROTATE_PROXY; throw _fe;
      }
    }
    _runLog.entry("F2: form filled");

    // ── IP check ──────────────────────────────────────────────────────────────
    // Check whether this proxy IP is already burned before spending captcha quota.
    // Entries older than 24h are pruned (server block is incremental, not permanent).
    let _proxyIp = null;
    try {
      const _ipRes = await sendTabCmd(tabId, "cmd-log-ip");
      _proxyIp = _ipRes?.ip ?? null;
    } catch (_) {}
    if (_proxyIp) {
      const {[SK.BURNED_PROXIES]: _burned = []} = await skGet(SK.BURNED_PROXIES);
      const _now = Date.now();
      const _active = _burned.filter(e => (_now - e.burnedAt) < TIMER_MS.proxy.BURN_TTL);
      if (_active.length !== _burned.length) await skSet({[SK.BURNED_PROXIES]: _active});
      if (_active.some(e => e.ip === _proxyIp)) {
        const _be = new Error(`F2: proxy ${_proxyIp} already burned — rotate`);
        _be.proxyStatus = ERR_STATUS.BURNED; _be.nextAction = NEXT_ACTION.ROTATE_PROXY; throw _be;
      }
      _runLog.entry(`F2: proxy ${_proxyIp} — not burned, proceeding`);
    }

    // ── Submit ────────────────────────────────────────────────────────────────
    // Arm BEFORE the submit loop — the server redirect to the token page can arrive
    // within milliseconds of the RGPD submit, before the loop exits and we could call
    // waitForPageReady.  If we armed it after, the page-ready signal would be lost.
    // 600s budget: timer starts here (before submit) to avoid the race where the server
    // redirects before we arm the listener.  Pre-captcha dwell (≤40s) + two captcha
    // attempts (≤140s) consume up to ~180s before submit, leaving ≥420s for the redirect.
    const tokenPageReady = _waitForPageReady(tabId, TIMER_MS.pageReady.TOKEN_PAGE);
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
      e.proxyStatus = ERR_STATUS.BLOCKED; e.nextAction = NEXT_ACTION.ROTATE_PROXY; throw e;
    }
    if (result.status === "username_taken") {
      // Account already exists — safety net for the username randomization in F_allInOne.
      // nextAction: NEXT_ACTION.ROTATE_PROXY lets the manager retry; F_allInOne will re-randomize
      // the suffix on the next call so the new attempt uses a fresh username.
      const e = new Error("F2: username already taken (collision on retry)");
      e.proxyStatus = ERR_STATUS.USERNAME_COLLISION; e.nextAction = NEXT_ACTION.ROTATE_PROXY; throw e;
    }
    if (result.status === "email_taken") {
      // Email already registered — happens when retry reuses an email from a prior attempt.
      const e = new Error("F2: email already registered");
      e.proxyStatus = ERR_STATUS.EMAIL_TAKEN; e.nextAction = NEXT_ACTION.ROTATE_PROXY; throw e;
    }
    if (result.status === "captcha_fail") {
      if (_proxyIp) {
        const {[SK.BURNED_PROXIES]: _bl = []} = await skGet(SK.BURNED_PROXIES);
        const _now = Date.now();
        const _al = _bl.filter(e => (_now - e.burnedAt) < TIMER_MS.proxy.BURN_TTL);
        if (!_al.some(e => e.ip === _proxyIp)) {
          _al.push({ip: _proxyIp, burnedAt: _now});
          await skSet({[SK.BURNED_PROXIES]: _al});
          _runLog.entry(`F2: proxy ${_proxyIp} marked as burned`);
        }
      }
      const e = new Error("F2: register failed: captcha_fail");
      e.proxyStatus = ERR_STATUS.BURNED; e.nextAction = NEXT_ACTION.ROTATE_PROXY; throw e;
    }
    if (result.status !== "navigated" && result.status !== "submitted") {
      const e = new Error(`F2: register failed: ${result.status}`);
      e.proxyStatus = ERR_STATUS.UNKNOWN; e.nextAction = NEXT_ACTION.ROTATE_PROXY; throw e;
    }

    _runLog.entry("F2: waiting for token page redirect + email code");
    _startEmailPoll(emailAcct.jwt, tabId);
    await tokenPageReady; // wait for token page navigation
    _runLog.entry("F2: token page reached — polling email");
  } else {
    // ── Recovery: already on token page ──────────────────────────────────────
    // Form was submitted and the page navigated before the SW restarted.
    // Skip form-open/fill/submit — resume directly at email-code wait.
    _runLog.entry("F2: resuming at token page — skipping form stages");
    const {[SK.EMAIL_CODE_TOKEN]: _existingCode} = await skGet(SK.EMAIL_CODE_TOKEN);
    if (!_existingCode) _startEmailPoll(emailAcct.jwt, tabId);
  }

  const codeToken = await _waitForEmailCodeToken(TIMER_MS.email.CODE_TIMEOUT);
  if (!codeToken) {
    const e = new Error("F2: email code token timeout");
    e.proxyStatus = ERR_STATUS.PROXY_SLOW; e.nextAction = NEXT_ACTION.ROTATE_PROXY; throw e;
  }
  _runLog.entry(`F2: email code token received: ${codeToken}`);

  // Write pending-account BEFORE submit click — page navigates on success,
  // killing the content script; main() on the next page picks this up and saves the CSV.
  const {[SK.BOT_ID]: _sessionId} = await skGet(SK.BOT_ID);
  await skSet({
    [SK.PENDING_ACCOUNT]: {
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
  const verifyReady = _waitForPageReady(tabId, TIMER_MS.pageReady.DEFAULT); // arm before submit navigates
  // port-closed error = page navigated = success; {ok:false} = error alert = failure
  const tokenResult = await sendTabCmd(tabId, "cmd-token-submit")
    .catch(() => ({ok: true, status: "navigated"}));
  const {state: finalState} = await verifyReady;
  _runLog.entry(`F2: verification done → result=${tokenResult.ok ? "success" : `failed(${tokenResult.status})`} state=${finalState}`);

  if (!tokenResult.ok) {
    await skRemove(SK.PENDING_ACCOUNT);
    const _f2e = new Error(`F2: email verification failed: ${tokenResult.status}${tokenResult.alert ? ` — ${tokenResult.alert}` : ""}`);
    _f2e.proxyStatus = ERR_STATUS.UNKNOWN; _f2e.nextAction = NEXT_ACTION.ROTATE_PROXY; throw _f2e;
  }

  // Belt-and-suspenders: save directly from background in case the content.js
  // save-account round-trip failed (navigation timing, WS disconnect, etc).
  // Deduplication by username prevents double entries when content.js also saved.
  try {
    const {[SK.ACCOUNTS]: _accs} = await skGet(SK.ACCOUNTS);
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
      await skSet({[SK.ACCOUNTS]: _list});
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
      await _waitForPageReady(tabId, TIMER_MS.pageReady.LOGIN_SUBMIT).catch(() => {});
      return {ok: true, status: "navigated"};
    });
    if (result.status === "logged-in" || result.status === "navigated") {
      return {ok: true, status: "logged-in"};
    }
    if (result.status === "rejected") return {ok: false, status: "rejected"};
    if (attempt < 2) await new Promise(r => setTimeout(r, TIMER_MS.auth.LOGIN_RETRY));
  }
  return {ok: false, status: "login_failed"};
}

async function F4_formFilling(tabId) {
  const { step: _f4Step } = await _currentWfStep(tabId);

  // Already on schedule page — F4's pipeline is complete.
  if (STEP_BUCKETS.schedule.has(_f4Step)) {
    _runLog.entry("F4: already on schedule page — skipping");
    return {ok: true, status: "form-ready"};
  }

  // Skip questionnaire navigation if already on questionnaire or form page.
  const _skipQNav  = STEP_BUCKETS.query.has(_f4Step) || STEP_BUCKETS.form.has(_f4Step);
  // Skip questionnaire fill if already on form page.
  const _skipQFill = STEP_BUCKETS.form.has(_f4Step);

  if (_skipQNav)  _runLog.entry(`F4: resuming — already on ${_skipQFill ? "form" : "questionnaire"} page`);

  if (!_skipQNav) {
    // Arm listener BEFORE the command that triggers navigation.
    const _qNavReady = _waitForPageReady(tabId, TIMER_MS.pageReady.DEFAULT);
    await sendTabCmd(tabId, "cmd-go-questionnaire").catch(() => {});
    const {state: qState} = await _qNavReady;
    if (qState !== "questionnaire") {
      const e = new Error(`F4: expected questionnaire, got ${qState}`);
      e.proxyStatus = ERR_STATUS.UNKNOWN; e.nextAction = NEXT_ACTION.ROTATE_PROXY; throw e;
    }
  }

  if (!_skipQFill) {
    let fState;
    for (let attempt = 0; attempt < 2; attempt++) {
      const _qFillReady = _waitForPageReady(tabId, TIMER_MS.pageReady.QUESTIONNAIRE);
      await sendTabCmd(tabId, "cmd-fill-questionnaire").catch(() => {});
      ({state: fState} = await _qFillReady);
      if (fState === "form") break;
      if (fState === "auth") break;  // server-side session expiry — handle below
      if (fState !== "questionnaire") {
        const e = new Error(`F4: expected form, got ${fState}`);
        e.proxyStatus = ERR_STATUS.UNKNOWN; e.nextAction = NEXT_ACTION.ROTATE_PROXY; throw e;
      }
      // questionnaire page reloaded — retry
    }

    if (fState === "auth") {
      // Server redirected to auth mid-questionnaire (session expired within ~24s).
      // Re-login once using stored credentials and retry questionnaire.
      const {[SK.REGISTER_PERSON]: creds} = await skGet(SK.REGISTER_PERSON);
      if (!creds?.username || !creds?.password) {
        const e = new Error("F4: session expired, no stored credentials for re-login");
        e.proxyStatus = ERR_STATUS.UNKNOWN; e.nextAction = NEXT_ACTION.CHANGE_DEVICE; throw e;
      }
      const _reloginRes = await F3_login(tabId, {username: creds.username, password: creds.password});
      if (!_reloginRes.ok) {
        const e = new Error(`F4: re-login failed — status=${_reloginRes.status}`);
        e.proxyStatus = ERR_STATUS.LOGIN_REJECTED; e.nextAction = NEXT_ACTION.ROTATE_PROXY; throw e;
      }
      const _reQNavReady = _waitForPageReady(tabId, TIMER_MS.pageReady.DEFAULT);
      await sendTabCmd(tabId, "cmd-go-questionnaire").catch(() => {});
      const {state: qState2} = await _reQNavReady;
      if (qState2 !== "questionnaire") {
        const e = new Error(`F4: after re-login, expected questionnaire, got ${qState2}`);
        e.proxyStatus = ERR_STATUS.UNKNOWN; e.nextAction = NEXT_ACTION.ROTATE_PROXY; throw e;
      }
      const _reQFillReady = _waitForPageReady(tabId, TIMER_MS.pageReady.QUESTIONNAIRE);
      await sendTabCmd(tabId, "cmd-fill-questionnaire").catch(() => {});
      ({state: fState} = await _reQFillReady);
    }

    if (fState !== "form") {
      const e = new Error(`F4: questionnaire never reached form after retries`);
      e.proxyStatus = ERR_STATUS.UNKNOWN; e.nextAction = NEXT_ACTION.ROTATE_PROXY; throw e;
    }
  }

  // Fill visa application form → navigates to schedule page.
  const _formFillReady = _waitForPageReady(tabId, TIMER_MS.pageReady.FORM_FILL);
  await sendTabCmd(tabId, "cmd-fill-form").catch(() => {});
  const {state: sState} = await _formFillReady;
  if (sState !== "schedule") {
    const e = new Error(`F4: expected schedule, got ${sState}`);
    e.proxyStatus = ERR_STATUS.UNKNOWN; e.nextAction = NEXT_ACTION.ROTATE_PROXY; throw e;
  }

  return {ok: true, status: "form-ready"};
}

async function F5_scheduling(tabId, config) {
  return sendTabCmd(tabId, "cmd-schedule", config).catch(e => ({ok: false, error: e.message}));
}

let _applyTriggerLock      = false; // debounce: prevent duplicate apply starts
let _waitingForSignal      = false; // EXTERNAL_SIGNAL mode: waiting for signal-apply
let _postAvailableResolver = null;  // set by step21 (AUTO_TRIGGER inline wait); called by TARGET_POST_AVAILABLE handler

function _sendStatusUpdate(state) {
  self.Comm?.send({ type: "status-update", state });
  skSet({[SK.RUN_STATUS]: state, [SK.RUN_ERROR]: null}).catch(() => {});
}

function _writeRunError(e) {
  const msg = e?.message ?? String(e);
  skSet({[SK.RUN_STATUS]: "error", [SK.RUN_ERROR]: msg}).catch(() => {});
}

async function F_warmup(config) {
  const idleStep = config.idleStep ?? "login";
  _runLog.start(`warmup_${idleStep}`);
  _runLog.entry(`username=${config.username} idleStep=${idleStep}`);
  _startSwKeepalive();
  p6_stop();
  await _resetWorkflow();
  const tabId = await _createSessionTab();
  await skSet({[SK.WARMUP_IDLE_STATE]: idleStep, [SK.WARMUP_TAB_ID]: tabId});

  try {
    _runLog.entry("F_warmup: opening auth page");
    await F1_openAuthPage(tabId);
    _runLog.entry("F_warmup: logging in");
    const _warmupLoginRes = await F3_login(tabId, {username: config.username, password: config.password});
    if (!_warmupLoginRes.ok) {
      await _runLog.finish(`error: F_warmup: login failed — status=${_warmupLoginRes.status}`);
      // Include nextAction so the manager can retry with a different proxy.
      return {ok: false, status: _warmupLoginRes.status, nextAction: NEXT_ACTION.ROTATE_PROXY};
    }
    _runLog.entry("F_warmup: logged in");

    if (idleStep === "login") {
      p6_keepSession(tabId, "login");
      await _runLog.finish("ok idleStep=login");
      return {ok: true, idleStep: "login"};
    }

    _runLog.entry("F_warmup: navigating to questionnaire");
    let pageReady = _waitForPageReady(tabId, TIMER_MS.pageReady.DEFAULT);
    await sendTabCmd(tabId, "cmd-go-questionnaire").catch(() => {});
    const {state: qState} = await pageReady;
    if (qState !== "questionnaire") {
      const e = new Error(`F_warmup: expected questionnaire, got ${qState}`);
      e.proxyStatus = ERR_STATUS.UNKNOWN; e.nextAction = NEXT_ACTION.ROTATE_PROXY; throw e;
    }
    _runLog.entry("F_warmup: filling questionnaire");

    pageReady = _waitForPageReady(tabId, TIMER_MS.pageReady.WARMUP_Q);
    await sendTabCmd(tabId, "cmd-fill-questionnaire").catch(() => {});
    const {state: fState} = await pageReady;
    if (fState !== "form") {
      const e = new Error(`F_warmup: expected form, got ${fState}`);
      e.proxyStatus = ERR_STATUS.UNKNOWN; e.nextAction = NEXT_ACTION.ROTATE_PROXY; throw e;
    }
    _runLog.entry("F_warmup: on form page");

    if (idleStep === "form") {
      await sendTabCmd(tabId, "cmd-fill-form-tabs").catch(() => {});
      p6_keepSession(tabId, "form");
      await _runLog.finish("ok idleStep=form");
      return {ok: true, idleStep: "form"};
    }

    if (idleStep === "form-monitor") {
      await sendTabCmd(tabId, "cmd-fill-form-tabs").catch(() => {});
      await skSet({
        [SK.TRIGGER_MODE]:   config.triggerMode  ?? "AUTO_TRIGGER",
        [SK.TARGET_POST_ID]: String(config.targetPostId ?? config.consulPost ?? ""),
      });
      _applyTriggerLock = false;
      _waitingForSignal = false;
      await sendTabCmd(tabId, "cmd-start-post-monitor").catch(() => {});
      p6_keepSession(tabId, "form");
      _sendStatusUpdate("READY_FOR_APPLY_IDLE");
      await _runLog.finish("ok idleStep=form-monitor");
      return {ok: true, idleStep: "form-monitor"};
    }

    if (idleStep === "schedule") {
      _runLog.entry("F_warmup: submitting form → schedule");
      pageReady = _waitForPageReady(tabId, TIMER_MS.pageReady.WARMUP_FORM);
      await sendTabCmd(tabId, "cmd-fill-form").catch(() => {});
      const {state: sState} = await pageReady;
      if (sState !== "schedule") {
        const e = new Error(`F_warmup: expected schedule, got ${sState}`);
        e.proxyStatus = ERR_STATUS.UNKNOWN; e.nextAction = NEXT_ACTION.ROTATE_PROXY; throw e;
      }
      p6_keepSession(tabId, "schedule");
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

async function F_allInOne(config) {
  _runLog.start("all_in_one");
  _startSwKeepalive();
  p6_stop();
  await _resetWorkflow();

  // Build person data. Use manager-supplied realPerson if provided; fall back to local generator.
  const rp = config.realPerson;
  let person;
  if (rp && (rp.firstName || rp.name)) {
    const gRaw = String(rp.gender ?? "").trim().toUpperCase();
    const firstName = String(rp.firstName ?? rp.name ?? "").trim();
    const lastName  = String(rp.lastName  ?? rp.surname ?? "").trim();
    const {username, password, emailLocal} = _genCredentials(firstName, lastName);
    person = {
      name:        firstName,
      surname:     lastName,
      username,
      password,
      email:       emailLocal,
      birth_date:  _parseDOB(rp.dob ?? rp.birth_date),
      gender:      gRaw === "F" || gRaw === "FEMALE" || gRaw === "FEMININO" ? "F" : "M",
      nationality: String(rp.nationality ?? "CPV").trim(),
      traveldoc:   String(rp.traveldoc  ?? "").trim(),
    };
  } else {
    person = generatePerson();
  }
  const emailAcct = await createTempEmail(person.email).catch(err => {
    err.proxyStatus = err.proxyStatus ?? ERR_STATUS.EMAIL_PROVIDER;
    err.nextAction  = err.nextAction  ?? NEXT_ACTION.ROTATE_PROXY;
    throw err;
  });
  await skSet({
    [SK.REGISTER_PERSON]:  person,
    [SK.REGISTER_EMAIL]:   emailAcct,
    [SK.EMAIL_TOKEN]:      null,
    [SK.EMAIL_CODE_TOKEN]: null,
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
  await new Promise(r => setTimeout(r, TIMER_MS.auth.SETTLE_MIN + Math.random() * TIMER_MS.auth.SETTLE_MARGIN));

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
        const waitMs = TIMER_MS.auth.REJECTION_BASE + (_attempt - 1) * TIMER_MS.auth.REJECTION_STEP; // 30 s, 60 s
        _runLog.entry(`all_in_one: login rejected — waiting ${waitMs / 1000}s then hard-reloading (attempt ${_attempt + 1}/3)`);
        await new Promise(r => setTimeout(r, waitMs));
        // Hard reload (Ctrl+F5 equivalent) — clears server-side captcha failure session
        // state and issues a fresh reCAPTCHA widget with a new challenge ID.
        const _freshPage = _waitForPageReady(loginTabId, TIMER_MS.pageReady.DEFAULT);
        await chrome.tabs.reload(loginTabId, {bypassCache: true});
        await _freshPage.catch(() => {});
      }
      _loginRes = await F3_login(loginTabId, {username: person.username, password: person.password});
      if (_loginRes.ok || _loginRes.status !== "rejected") break;
    }
    if (!_loginRes.ok) {
      const e = new Error(`F3: login failed — status=${_loginRes.status}`);
      e.proxyStatus = ERR_STATUS.LOGIN_REJECTED;
      e.nextAction  = NEXT_ACTION.ROTATE_PROXY;
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

async function _runCheckProxy() {
  if (!_activeTabId) return;
  const result = await sendTabCmd(_activeTabId, "cmd-run-proxy-check").catch(() => ({}));
  self.Comm?.send({ type: "proxy-check", ...result, timestamp: new Date().toISOString() });
}

function _runDispatchAbort() {
  p6_stop();
  if (_activeTabId) chrome.tabs.sendMessage(_activeTabId, {type: "abort"}).catch(() => {});
}

//#region Message Handlers
// All chrome.runtime.onMessage handlers are keyed by MSG.* constants (constants.js).
// Handler contract:
//   • Fire-and-forget workflows (login, register, apply): call respond({ok:true}) FIRST,
//     then start the async work — popup closes on ACK, does not wait for completion.
//   • Synchronous helpers (ping, save-account, inject scripts): call respond() inline
//     or return true for async response.
//   • All workflow errors are logged via console.error — not surfaced back to the popup.
//
// Popup → Background commands (MSG.LOGIN_IDLE … MSG.REGISTER_APPLY) delegate to
// WorkflowHandlers (wf_login, wf_register, etc.) defined in the region above.
// Manager WS commands arrive via Comm.dispatch → communication.js, not here.

chrome.runtime.onInstalled.addListener(() => {
  skSet({[SK.INSTALLED_AT]: new Date().toISOString()});
  console.log("[OctoProbe] Extension installed.");
});

// WS keepalive alarm — wakes the SW periodically so reconnectIfNeeded() can fire
// if the WS dropped while the SW was suspended.
// Hub sends protocol-level WS pings every 5 s (server-side) which already keeps
// the connection alive; this alarm handles the rare case where the SW is killed
// and needs to reconnect from stored credentials.
chrome.alarms.create("ws-ping", {periodInMinutes: TIMER_MS.ws.PING_MINUTES});
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

  // Login with credentials, then idle at logged-in state with keep-alives until apply arrives.
  [MSG.LOGIN_IDLE]: (msg, _sender, respond) => {
    const { username, password } = msg.payload ?? msg;
    if (!username || !password) { respond({ok: false, error: "credentials required"}); return; }
    respond({ok: true});
    wf_login(username, password, true).catch(_writeRunError);
  },

  // Login with credentials, then immediately run F4 form-fill + F5 scheduling.
  [MSG.LOGIN_APPLY]: (msg, _sender, respond) => {
    const { username, password } = msg.payload ?? msg;
    if (!username || !password) { respond({ok: false, error: "credentials required"}); return; }
    respond({ok: true});
    wf_login(username, password, false)
      .then(() => wf_apply())
      .catch(_writeRunError);
  },

  // Register account only — no login after.
  [MSG.REGISTER_ONLY]: (msg, _sender, respond) => {
    respond({ok: true});
    wf_register(msg.realPerson ?? null).catch(_writeRunError);
  },

  // Register then idle at logged-in state — credentials written to storage by wf_register.
  [MSG.REGISTER_LOGIN]: (msg, _sender, respond) => {
    respond({ok: true});
    wf_register(msg.realPerson ?? null)
      .then(() => wf_login(null, null, true))
      .catch(_writeRunError);
  },

  // Full pipeline: register → login → apply.
  [MSG.REGISTER_APPLY]: (msg, _sender, respond) => {
    respond({ok: true});
    wf_register(msg.realPerson ?? null)
      .then(() => wf_login(null, null, false))
      .then(() => wf_apply())
      .catch(_writeRunError);
  },

  // message handler triggered when content.js sends "page-ready" to service worker
  // content.js fires this whenever it detects a page has finished loading and has classified its DOM state.
  // 1. resolves a pending promise.
  // 2. restores post monitor after a page reload. if the page landed on "form" state AND the idel state stored in WARMUP_IDEL_STATE is form-monitor, it re-sends "cmd-start-post-monitor" to the tab. This covers the case where the extension was in a "waiting for a slot signal" idle and the page reloaded (session expiry, manual refresh) -- the monitor restarts automatically without needing a manual command.
  // 3. responds to acknowledge the message.
  [MSG.PAGE_READY]: (msg, sender, respond) => {
    const _prr = _pageReadyResolvers.get(sender.tab?.id);
    if (_prr) _prr.resolve({state: msg.state, url: msg.url});
    // Restore post monitor after a page reload when in form-monitor idle state
    if (msg.state === "form" && sender.tab?.id) {
      skGet(SK.WARMUP_IDLE_STATE).then(d => {
        if (d[SK.WARMUP_IDLE_STATE] === "form-monitor")
          sendTabCmd(sender.tab.id, "cmd-start-post-monitor").catch(() => {});
      });
    }
    respond({ok: true});
  },

  //   This is the handler that bootstraps the WebSocket connection to the hub manager.
  // What calls it: The content script injected into the worker-init page sends this message. The manifest registers a content script that matches *://*/*worker-init* — when Octo Browser opens a profile and navigates to the hub-init URL, that content script runs and immediately sends {type: "WORKER_INIT", botId: ..., hubUrl: ...} to the service worker. The bot ID and hub URL are embedded in the page (either in the URL query string or the page DOM) and extracted by the content script before sending.
  // What it does:
  // Initiates the WebSocket connection — calls Comm.connectHub(botId, hubUrl) which opens the WS connection to the manager server, identifies this browser profile as a specific bot slot, and starts listening for commands (register, all-in-one, etc.).
  // Responds immediately — {ok: true} just closes the message channel; the content script doesn't wait for the WS to actually connect, only for the background to acknowledge it received the init signal.
  // The self.Comm?.connectHub guard means if communication.js failed to load (e.g., extension update mid-session), the message is silently dropped rather than throwing.
  [MSG.WORKER_INIT]: (msg, _sender, respond) => {
    if (self.Comm?.connectHub) self.Comm.connectHub(msg.botId, msg.hubUrl);
    respond({ok: true});
  },

  // ── Slot intelligence relay ───────────────────────────────────────────────
  // content.js → background → manager (fire-and-forget)
  //   What calls it: content.js — the post monitor (cmd-start-post-monitor) runs a polling loop on the visa form page watching for available appointment slots. When it detects slots, it sends {type: "slot-observation", postId, slots} up to the service worker.
  // What it does:
  // Relays the slot observation straight out to the manager over WebSocket via Comm.send(). The service worker has no direct WS access from content scripts — content can only use chrome.runtime.sendMessage — so this handler acts as a passthrough: content detects slots → background relays to manager → manager decides whether to signal back with target-post-available.
  // Same fire-and-forget pattern: respond({ok: true}) immediately, the WS send is best-effort (no error surfaced to content).
  [MSG.SLOT_OBSERVATION]: (msg, _sender, respond) => {
    if (self.Comm) self.Comm.send({ type: "slot-observation", postId: msg.postId, slots: msg.slots, timestamp: Date.now() });
    respond({ok: true});
  },

  // content.js → background → manager → background → content.js (3s timeout)
  [MSG.SLOT_ASSIGNMENT_REQUEST]: (msg, _sender, respond) => {
    if (!self.Comm) { respond({ok: false}); return; }
    self.Comm.requestSlotAssignment({ postId: msg.postId, visibleSlots: msg.visibleSlots }, TIMER_MS.ws.SLOT_ASSIGN)
      .then(assignment => respond({ ok: true, ...assignment }))
      .catch(() => respond({ ok: false }));
    return true; // async response
  },

  // content.js → background → manager (fire-and-forget)
  [MSG.SLOT_FAILURE]: (msg, _sender, respond) => {
    if (self.Comm) self.Comm.send({ type: "slot-failure", slotKey: msg.slotKey, reason: msg.reason });
    respond({ok: true});
  },

  [MSG.SLOT_SUCCESS]: (msg, _sender, respond) => {
    if (self.Comm) self.Comm.send({ type: "slot-success", slotKey: msg.slotKey });
    respond({ok: true});
  },

  // ── Dual trigger ─────────────────────────────────────────────────────────
  // Fired by content.js when target post appears in #f0sf1 options.
  [MSG.TARGET_POST_AVAILABLE]: (msg, _sender, respond) => {
    // step21 (inline AUTO_TRIGGER wait inside p3) owns the flow — resolve its promise directly.
    if (_postAvailableResolver) {
      const res = _postAvailableResolver;
      _postAvailableResolver = null;
      res();
      respond({ok: true});
      return;
    }
    if (_applyTriggerLock) { respond({ok: true}); return; }
    _applyTriggerLock = true;
    p6_stop();
    skGet(SK.TRIGGER_MODE).then(d => {
      const triggerMode = d[SK.TRIGGER_MODE] ?? "AUTO_TRIGGER";
      _runLog.entry(`target-post-available: postId=${msg.postId} triggerMode=${triggerMode}`);
      if (triggerMode === "AUTO_TRIGGER") {
        _sendStatusUpdate("AUTO_TRIGGER_PENDING");
        wf_apply()
          .then(() => { _sendStatusUpdate("DONE"); })
          .catch(_writeRunError);
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

  [MSG.GET_SESSION_KEYS]: (_msg, _sender, respond) => {
    respond({sk: _SK, evtAlert: _EVT_ALERT, evtRcp: _EVT_RCP});
  },

  [MSG.PING]: (_msg, _sender, respond) => {
    respond({type: "pong", version: chrome.runtime.getManifest().version});
  },

  [MSG.GENERATE_PERSON]: (_msg, _sender, respond) => {
    respond({ok: true, person: generatePerson()});
  },

  // Sent by content.js every 20 s to reset Chrome's 30 s SW idle timer.
  // Keeps the SW alive in Chrome 110+ where open ports alone are insufficient.
  [MSG.SW_KEEPALIVE_PING]: (_msg, _sender, respond) => {
    respond({ok: true});
  },

  [MSG.LOG_ENTRY]: (msg, _sender, respond) => {
    _runLog.entry(msg.msg);
    respond({ok: true});
  },

  [MSG.START_EMAIL_POLL]: (msg, sender, respond) => {
    _startEmailPoll(msg.jwt, sender.tab?.id);
    respond({ok: true});
  },

  [MSG.STOP_EMAIL_POLL]: (_msg, _sender, respond) => {
    clearInterval(_pollInterval);
    _pollInterval = null;
    skRemove(SK.EMAIL_POLL);
    respond({ok: true});
  },

  [MSG.DISPATCH_PROXY_CHECK]: (_msg, _sender, respond) => {
    _runCheckProxy();
    respond({ok: !!_activeTabId});
  },

  [MSG.DISPATCH_ABORT]: (_msg, _sender, respond) => {
    _runDispatchAbort();
    respond({ok: true});
  },

  [MSG.SAVE_ACCOUNT]: (msg, _sender, respond) => {
    const account = msg.account;
    const ts = new Date().toISOString().replace(/[:.]/g, "-").slice(0, 19);
    // Include sessionId prefix for traceability when multiple sessions run concurrently
    const _sid = msg.sessionId ?? account.sessionId ?? null;
    const filename = _sid ? `account_${_sid.slice(0, 8)}_${ts}.csv` : `account_${ts}.csv`;
    (async () => {
      try {
        const {[SK.ACCOUNTS]: accounts} = await skGet(SK.ACCOUNTS);
        const list = Array.isArray(accounts) ? accounts : [];
        if (!list.some(a => a.username === account.username)) {
          list.push(account);
          await skSet({[SK.ACCOUNTS]: list});
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

  [MSG.CLOSE_TAB]: (_msg, sender, respond) => {
    const tabId = sender.tab?.id;
    if (tabId) chrome.tabs.remove(tabId);
    respond({ok: true});
  },

  [MSG.CLEAR_WORKFLOW]: (_msg, _sender, respond) => {
    skRemove(SK.WORKFLOW_TYPE, SK.WORKFLOW_STEP, SK.REGISTER_PERSON, SK.REGISTER_EMAIL, SK.EMAIL_TOKEN, SK.EMAIL_CODE_TOKEN, SK.EMAIL_POLL, SK.WARMUP_IDLE_STATE);
    respond({ok: true});
  },

  [MSG.INJECT_ALERT_CAPTURE]: (msg, sender, respond) => {
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

  [MSG.RESET_RECAPTCHA]: (_msg, sender, respond) => {
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

  [MSG.FILL_TOKEN]: (msg, sender, respond) => {
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

  [MSG.SOLVE_RECAPTCHA_API]: (msg, _sender, respond) => {
    const {pageUrl, siteKey, action = null} = msg;
    _doSolveRecaptcha(pageUrl, siteKey, action)
      .then(token => respond({ok: true, token}))
      .catch(e   => respond({ok: false, error: String(e)}));
    return true;
  },

  // Combines solve + inject into one round-trip. Background solves, pads to minSolveMs,
  // injects the token via executeScript (which fires the _evtRcp event in MAIN world),
  // then responds. Content.js drifts the mouse concurrently while awaiting this response.
  [MSG.SOLVE_AND_INJECT_RECAPTCHA]: (msg, sender, respond) => {
    const tabId = sender.tab?.id;
    if (!tabId) { respond({ok: false, error: "no sender tab"}); return; }
    const {pageUrl, siteKey, action = null, minSolveMs = TIMER_MS.captcha.MIN_SOLVE} = msg;
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

  [MSG.INJECT_RECAPTCHA_TOKEN]: (msg, sender, respond) => {
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

  [MSG.GET_RECAPTCHA_SITEKEY]: (_msg, sender, respond) => {
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

  [MSG.EXEC_PAGE_SCRIPT]: (msg, sender, respond) => {
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

  [MSG.WS_SEND]: (msg, _sender, respond) => {
    if (self.Comm) self.Comm.send(msg.data);
    respond({ok: true});
  },

};

chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  const h = _MSG_HANDLERS[msg.type];
  if (h) return h(msg, sender, sendResponse) ?? false;
  return false;
});
//#endregion