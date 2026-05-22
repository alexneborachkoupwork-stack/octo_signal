// Service worker — credential storage, workflow dispatch, mail.tm email polling.

const TARGET_URL = "https://pedidodevistos.mne.gov.pt/VistosOnline/";
const MAILTM    = "https://api.mail.tm";

// ---------------------------------------------------------------------------
// Fake person generator (JS port of data/person.py)
// ---------------------------------------------------------------------------

const _FIRST = ["Ana","Maria","João","Carlos","Pedro","Sofia","Luísa","Miguel","Rita","Filipe",
                 "Sara","Rui","Inês","Tiago","Beatriz","André","Catarina","Nuno","Mariana","Diogo"];
const _LAST  = ["Silva","Santos","Oliveira","Costa","Ferreira","Pereira","Rodrigues","Alves",
                 "Martins","Carvalho","Gomes","Lopes","Sousa","Marques","Mendes","Correia"];
const _NAT   = ["BRA","IND","CHN","MOZ","ANG","CPV","PHL","PAK","MAR","COL"];

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
  const uname = (first.slice(0,3) + last.slice(0,3) + (100 + _rand(9900))).toLowerCase()
                .normalize("NFD").replace(/[̀-ͯ]/g,"");
  const traveldoc = Array.from({length:16}, () => _rand(10)).join("");
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
  return {email: `${local}@${domain}`, password: "", jwt: local};
}

async function _pollCloudflare(localPart) {
  const {
    "cf-worker-url":    workerUrl,
    "cf-worker-secret": secret,
  } = await chrome.storage.local.get(["cf-worker-url", "cf-worker-secret"]);

  if (!workerUrl || !secret) throw new Error("Cloudflare Worker not configured");

  const r = await fetch(`${workerUrl.replace(/\/$/, "")}/${localPart}`, {
    headers: {"X-Secret": secret},
  });
  if (!r.ok) throw new Error(`CF Worker error ${r.status}`);

  const messages = await r.json();
  if (!messages.length) return null;

  // Return in the same shape _extractToken expects.
  return {text: messages[0].text, html: ""};
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
// Tab helper — reuse an existing target tab rather than always opening a new one.
// ---------------------------------------------------------------------------

// Tear down any in-flight workflow completely before starting a new one.
// Closes all target tabs, cancels email polling, and wipes workflow storage.
// Awaiting this guarantees a clean slate regardless of how many times the
// user clicked the button or what state a previous run left behind.
async function _resetWorkflow() {
  clearInterval(_pollInterval);
  _pollInterval = null;

  const {"active-tab-id": savedTabId} = await chrome.storage.local.get("active-tab-id");

  await chrome.storage.local.remove([
    "workflow-type", "workflow-step",
    "register-person", "register-email",
    "email-token", "emailPoll",
    "login-pending", "pending-account",
    "active-tab-id", "register-retried",
  ]);

  // Primary: close by stored tab ID (reliable — survives any URL change during workflow).
  if (savedTabId) {
    await chrome.tabs.remove(savedTabId).catch(() => {});
  }

  // Sweep: URL-based fallback catches tabs from crashed/untracked runs.
  const targetHost = new URL(TARGET_URL).hostname;
  const allTabs    = await chrome.tabs.query({});
  const stragglers = allTabs.filter(t => {
    try { return new URL(t.url ?? "").hostname === targetHost; } catch (_) { return false; }
  });
  if (stragglers.length > 0) {
    await chrome.tabs.remove(stragglers.map(t => t.id)).catch(() => {});
  }
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

    const token = _extractToken(msg);
    if (!token) return;

    clearInterval(_pollInterval);
    _pollInterval = null;
    await chrome.storage.local.remove("emailPoll");
    await chrome.storage.local.set({"email-token": token});

    // Notify content script in the target tab
    chrome.tabs.sendMessage(emailPoll.tabId, {type: "email-token", token})
      .catch(() => {}); // tab may have navigated
  } catch (e) {
    console.error("[OctoProbe BG] email poll error:", e);
  }
}

// ---------------------------------------------------------------------------
// Message handler
// ---------------------------------------------------------------------------

chrome.runtime.onInstalled.addListener(() => {
  chrome.storage.local.set({installedAt: new Date().toISOString()});
  console.log("[OctoProbe] Extension installed.");
});

chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {

  if (msg.type === "ping") {
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

  } else if (msg.type === "go-to-site") {
    (async () => {
      await _resetWorkflow();
      await chrome.storage.local.set({"workflow-type":"login","workflow-step":"home"});
      const tab = await chrome.tabs.create({url: TARGET_URL});
      await chrome.storage.local.set({"active-tab-id": tab.id});
      sendResponse({ok: true});
    })();
    return true;

  } else if (msg.type === "go-visa") {
    (async () => {
      await _resetWorkflow();
      await chrome.storage.local.set({"workflow-type": "visa"});
      // Navigate directly to Questionario — if the session cookie is still valid the
      // content script lands on the questionnaire immediately and skips login entirely.
      // If the session has expired the server redirects to the auth page and the
      // content script detects "auth" state and logs in from there.
      const tab = await chrome.tabs.create({url: TARGET_URL + "Questionario"});
      await chrome.storage.local.set({"active-tab-id": tab.id});
      sendResponse({ok: true});
    })();
    return true;

  } else if (msg.type === "start-register") {
    (async () => {
      try {
        await _resetWorkflow();
        let person;
        if (msg.realPerson) {
          // Real passport data from popup form — generate only account credentials.
          const acct = generatePerson();
          const rp   = msg.realPerson;
          const gRaw = String(rp.gender ?? "").trim().toUpperCase();
          person = {
            name:        String(rp.firstName ?? "").trim(),
            surname:     String(rp.lastName  ?? "").trim(),
            username:    acct.username,
            password:    acct.password,
            birth_date:  _parseDOB(rp.dob),
            gender:      gRaw === "F" || gRaw === "FEMALE" || gRaw === "FEMININO" ? "F" : "M",
            nationality: String(rp.nationality ?? "").trim(),
            traveldoc:   String(rp.traveldoc  ?? "").trim(),
          };
        } else {
          person = generatePerson();
        }
        const emailAcct = await createTempEmail();
        const {["warmup-enabled"]: warmupEnabled = false} =
          await new Promise(r => chrome.storage.local.get("warmup-enabled", r));
        await chrome.storage.local.set({
          "workflow-type":     warmupEnabled ? "warmup"  : "register",
          "workflow-step":     warmupEnabled ? null      : "home",
          "warmup-end-time":   warmupEnabled ? Date.now() + 10 * 60 * 1000 : null,
          "warmup-site-index": 0,
          "register-person":   person,
          "register-email":    emailAcct,
          "email-token":       null,
        });
        const tab = await chrome.tabs.create({url: TARGET_URL});
        await chrome.storage.local.set({"active-tab-id": tab.id});
        sendResponse({ok: true, email: emailAcct.email, password: person.password, username: person.username});
      } catch (e) {
        sendResponse({ok: false, error: String(e)});
      }
    })();
    return true;

  } else if (msg.type === "start-email-poll") {
    const tabId = sender.tab?.id;
    const {jwt} = msg;
    chrome.storage.local.set({emailPoll: {jwt, tabId}});
    if (!_pollInterval) {
      _pollInterval = setInterval(_doPollTick, 6000);
    }
    sendResponse({ok: true});

  } else if (msg.type === "stop-email-poll") {
    clearInterval(_pollInterval);
    _pollInterval = null;
    chrome.storage.local.remove("emailPoll");
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
      "workflow-type","workflow-step","register-person","register-email","email-token","emailPoll"
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

        const solverFns = {
          "anti-captcha": () => _solveACFormat("https://api.anti-captcha.com", _pick(_ANTICAPTCHA_KEYS), pageUrl, siteKey, action),
          "2captcha":     () => _solveACFormat("https://api.2captcha.com",     _pick(_TWOCAPTCHA_KEYS),  pageUrl, siteKey, action),
          "capmonster":   () => _solveACFormat("https://api.capmonster.cloud", _pick(_CAPMONSTER_KEYS),  pageUrl, siteKey, action),
          "capsolver":    () => _solveCapSolver(pageUrl, siteKey, action),
        };
        let token;
        if (parallel) {
          token = await _raceSolvers(pageUrl, siteKey, action);
        } else {
          const primary = solverFns[solver] ? solver : "capsolver";
          try {
            token = await solverFns[primary]();
          } catch(primaryErr) {
            // Primary solver failed (e.g. CapSolver 1001 "Failed to solve") —
            // race the remaining solvers as fallback rather than giving up.
            console.warn(`[OctoProbe BG] Primary solver "${primary}" failed: ${primaryErr.message} — trying fallback`);
            const fallbackFns = Object.entries(solverFns)
              .filter(([k]) => k !== primary)
              .map(([, fn]) => fn);
            token = await new Promise((resolve, reject) => {
              let remaining = fallbackFns.length;
              let settled = false;
              for (const fn of fallbackFns) {
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
