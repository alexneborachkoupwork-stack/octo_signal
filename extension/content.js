/**
 * Octo Probe — content script.
 *
 * Injects a status badge on every page.
 * On the target site, runs the active workflow (login or register) based on
 * chrome.storage.local state set by the popup / background service worker.
 *
 * Workflow state keys (set by background.js):
 *   workflow-type : "login" | "register" | "visa" | null
 *   workflow-step : "home" | "auth" | "token" | "visa-menu" | "questionnaire" | "form" | "schedule" | null
 *   register-person : {name, surname, username, password, birth_date, gender, nationality, traveldoc}
 *   register-email  : {email, password, jwt}
 *   email-token     : string | null  (set by background when poll finds the message)
 */

// ── Service-worker keepalive via persistent port ──────────────────────────────
// Chrome MV3 terminates idle service workers after a few seconds of inactivity.
// setInterval / storage calls from the SW itself are unreliable — Chrome only
// tracks active *event handler* execution.  A chrome.runtime.connect port opened
// from a content script IS tracked: as long as any tab has this script running,
// the SW is kept alive indefinitely.  Content.js matches <all_urls> so it runs
// on the worker-init tab and every workflow tab the extension opens.
(function _swKeepalivePort() {
  try {
    const port = chrome.runtime.connect({name: "sw-keepalive"});
    port.onDisconnect.addListener(() => {
      setTimeout(_swKeepalivePort, 500);
    });
  } catch (_) {}
})();

// Chrome 110+ changed port behaviour: an open port no longer prevents SW
// termination — it only gives a single 30 s window from the onConnect event.
// Periodic sendMessage calls ARE tracked as chrome.runtime.onMessage events,
// which reset the 30 s idle timer without killing the SW or losing execution
// state.  20 s interval ensures the timer never reaches 0.
setInterval(() => {
  chrome.runtime.sendMessage({type: "sw-keepalive-ping"}).catch(() => {});
}, 20_000);

const TARGET_HOST = "pedidodevistos.mne.gov.pt";
const MAILTM      = "https://api.mail.tm";

const WARMUP_SITES = [
  "https://www.google.com/search?q=apply+for+visa+portugal",
  "https://en.wikipedia.org/wiki/Portugal",
  "https://news.google.com/",
  "https://www.youtube.com/",
  "https://www.google.com/maps/place/Lisbon,+Portugal",
  "https://www.google.com/search?q=schengen+visa+europe+requirements",
];
const WARMUP_DWELL_MS = 100_000;

// ---------------------------------------------------------------------------
// Selectors
// ---------------------------------------------------------------------------

const LANG_SELECTORS = [
  "select#language-select-d",
  "select#language-select-m",
  "select[name='lang']",
];

const LOGIN_LINK_SELECTORS = [
  "a[href*='Authentication']",
  "a[href*='authentication']",
  "a[href*='login']",
  "a[href*='Login']",
];

const USERNAME_SELECTORS = [
  "input[name='username']","input[name='email']","input[name='login']",
  "input[type='email']","input[id*='user']","input[id*='email']",
  "input[placeholder*='user' i]","input[placeholder*='email' i]",
  "input[placeholder*='utilizador' i]",
];
const PASSWORD_SELECTORS = [
  "input[name='password']","input[type='password']",
  "input[id*='pass']","input[placeholder*='password' i]","input[placeholder*='palavra' i]",
];

const REGISTER_LINK_SELECTORS = [
  "span[name='registration']",
  "input[name='registration']",
  "button[name='registration']",
  "[onclick*='registration']",
];
const REGISTER_LINK_TEXTS = ["register","sign up","criar conta","registar","novo utilizador"];

const TOKEN_INPUT_SELECTORS = [
  "input[name='tokenInput']",    // confirmed from 2.activation.html
  "input[id='tokenInput']",
  "input[id*='oken']",
  "#mainContent input[type='text']",
];
const TOKEN_SUBMIT_SELECTORS = [
  "#tokenFormSubmit",
  "#mainContent button[type='submit']",
  "#mainContent input[type='submit']",
  "#mainContent .btn-primary",
];

// ---------------------------------------------------------------------------
// Error codes
// ---------------------------------------------------------------------------

const E = Object.freeze({
  NETWORK_SLOW:        1,
  REGISTER_IP_BLOCK:   2,
  REGISTER_LINK:       3,
  REGISTER_FORM:       4,
  REGISTER_INCOMPLETE: 5,
  REGISTER_WAF:        6,
  REGISTER_CAPTCHA:    7,
  LOGIN_FAILED:        8,
  LOGIN_CAPTCHA:       9,
  REGISTER_TOKEN:      10,
  REGISTER_TOKEN_FORM: 11,
  REGISTER_SAVE:       12,
  VISA_FAILED:         13,
});

// ---------------------------------------------------------------------------
// Utilities
// ---------------------------------------------------------------------------

let _abortFlag = false;
let _postMonitorId = null; // { mo: MutationObserver, poll: intervalId } | null
function sleep(ms) {
  return new Promise((resolve, reject) => {
    if (_abortFlag) { reject(new DOMException("Aborted", "AbortError")); return; }
    chrome.runtime.sendMessage({type: "sleep", ms}, () => {
      if (_abortFlag) reject(new DOMException("Aborted", "AbortError"));
      else if (chrome.runtime.lastError) setTimeout(resolve, ms);
      else resolve();
    });
  });
}

function isVisible(el) {
  if (!el) return false;
  const r = el.getBoundingClientRect();
  const s = window.getComputedStyle(el);
  return r.width > 0 && r.height > 0
    && s.display    !== "none"
    && s.visibility !== "hidden"
    && s.opacity    !== "0";
}

function findBySelectors(sels) {
  for (const sel of sels) {
    try { const el = document.querySelector(sel); if (isVisible(el)) return el; } catch (_) {}
  }
  return null;
}

function findByText(tags, texts) {
  for (const tag of tags) {
    for (const el of document.querySelectorAll(tag)) {
      const t = el.textContent.trim().toLowerCase();
      if (texts.some(txt => t.includes(txt))) return el;
    }
  }
  return null;
}

async function waitFor(findFn, maxMs = 8000, intervalMs = 300) {
  const deadline = Date.now() + maxMs;
  while (Date.now() < deadline) {
    const el = findFn();
    if (el) return el;
    await sleep(intervalMs);
  }
  return null;
}

function fillInput(el, value) {
  el.focus();
  const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")?.set;
  if (setter) setter.call(el, value); else el.value = value;
  el.dispatchEvent(new Event("input",  {bubbles: true}));
  el.dispatchEvent(new Event("change", {bubbles: true}));
}

// ---------------------------------------------------------------------------
// Human-like interaction helpers (mouse trajectory + character-by-character typing)
// ---------------------------------------------------------------------------

function _bezier(t, p0, p1, p2, p3) {
  return (1-t)**3*p0 + 3*(1-t)**2*t*p1 + 3*(1-t)*t**2*p2 + t**3*p3;
}

function _gaussRnd() {
  let u, v;
  do { u = Math.random(); } while (!u);
  do { v = Math.random(); } while (!v);
  return Math.sqrt(-2 * Math.log(u)) * Math.cos(2 * Math.PI * v);
}
function _lognormalMs(medianMs, sigma) {
  return Math.max(25, Math.round(Math.exp(Math.log(medianMs) + (sigma ?? 0.4) * _gaussRnd())));
}

async function humanMoveTo(el) {
  const rect   = el.getBoundingClientRect();
  const tx     = rect.left + rect.width  * (0.25 + Math.random() * 0.5);
  const ty     = rect.top  + rect.height * (0.25 + Math.random() * 0.5);
  const sx     = window._mX ?? innerWidth  * 0.4;
  const sy     = window._mY ?? innerHeight * 0.3;
  const steps  = 4 + Math.floor(Math.random() * 4);
  const spread = 70;
  const cp1x   = sx + (tx-sx)*0.33 + (Math.random()-.5)*spread;
  const cp1y   = sy + (ty-sy)*0.33 + (Math.random()-.5)*spread;
  const cp2x   = sx + (tx-sx)*0.66 + (Math.random()-.5)*spread;
  const cp2y   = sy + (ty-sy)*0.66 + (Math.random()-.5)*spread;
  let prevHover = document.elementFromPoint(sx, sy);
  for (let i = 0; i <= steps; i++) {
    const t   = i / steps;
    const cx  = Math.round(_bezier(t, sx, cp1x, cp2x, tx));
    const cy  = Math.round(_bezier(t, sy, cp1y, cp2y, ty));
    const scx = cx + (window.screenX ?? 0);
    const scy = cy + (window.screenY ?? 0);
    const mvX = cx - (window._mX ?? cx);
    const mvY = cy - (window._mY ?? cy);
    const under = document.elementFromPoint(cx, cy) ?? document.body;
    if (under !== prevHover) {
      if (prevHover) {
        prevHover.dispatchEvent(new MouseEvent("mouseout",   {bubbles: true,  cancelable: true,  clientX: cx, clientY: cy, screenX: scx, screenY: scy, relatedTarget: under}));
        prevHover.dispatchEvent(new MouseEvent("mouseleave", {bubbles: false, cancelable: false, clientX: cx, clientY: cy, screenX: scx, screenY: scy, relatedTarget: under}));
      }
      under.dispatchEvent(new MouseEvent("mouseover",  {bubbles: true,  cancelable: true,  clientX: cx, clientY: cy, screenX: scx, screenY: scy, relatedTarget: prevHover}));
      under.dispatchEvent(new MouseEvent("mouseenter", {bubbles: false, cancelable: false, clientX: cx, clientY: cy, screenX: scx, screenY: scy, relatedTarget: prevHover}));
      prevHover = under;
    }
    under.dispatchEvent(new PointerEvent("pointermove", {
      bubbles: true, cancelable: true, clientX: cx, clientY: cy, screenX: scx, screenY: scy,
      pointerId: 1, pointerType: "mouse", isPrimary: true, movementX: mvX, movementY: mvY,
    }));
    under.dispatchEvent(new MouseEvent("mousemove", {
      bubbles: true, cancelable: true, clientX: cx, clientY: cy, screenX: scx, screenY: scy,
      movementX: mvX, movementY: mvY,
    }));
    window._mX = cx; window._mY = cy;
    const _t  = i / Math.max(steps, 1);
    const _spd = 0.3 + 1.4 * (_t < 0.5 ? 2*_t*_t : 1 - Math.pow(-2*_t+2, 2)/2);
    await sleep(Math.max(3, Math.round((5 + Math.random() * 8) / _spd)));
  }
  // 10 % chance of micro-overshoot then correction — real users often overshoot small targets
  if (Math.random() < 0.10) {
    const ox = Math.round(tx + (Math.random() - 0.5) * 12);
    const oy = Math.round(ty + (Math.random() - 0.5) * 12);
    const ou = document.elementFromPoint(ox, oy) ?? document.body;
    ou.dispatchEvent(new PointerEvent("pointermove", {
      bubbles: true, cancelable: true, clientX: ox, clientY: oy,
      screenX: ox + (window.screenX ?? 0), screenY: oy + (window.screenY ?? 0),
      pointerId: 1, pointerType: "mouse", isPrimary: true,
      movementX: ox - (window._mX ?? ox), movementY: oy - (window._mY ?? oy),
    }));
    window._mX = ox; window._mY = oy;
    await sleep(25 + Math.random() * 40);
    const tu = document.elementFromPoint(Math.round(tx), Math.round(ty)) ?? document.body;
    tu.dispatchEvent(new PointerEvent("pointermove", {
      bubbles: true, cancelable: true, clientX: Math.round(tx), clientY: Math.round(ty),
      screenX: Math.round(tx) + (window.screenX ?? 0), screenY: Math.round(ty) + (window.screenY ?? 0),
      pointerId: 1, pointerType: "mouse", isPrimary: true,
      movementX: Math.round(tx) - ox, movementY: Math.round(ty) - oy,
    }));
    window._mX = Math.round(tx); window._mY = Math.round(ty);
    await sleep(10 + Math.random() * 20);
    const _t  = i / Math.max(steps, 1);
    const _spd = 0.3 + 1.4 * (_t < 0.5 ? 2*_t*_t : 1 - Math.pow(-2*_t+2, 2)/2);
    await sleep(Math.max(3, Math.round((5 + Math.random() * 8) / _spd)));
  }
  await sleep(30 + Math.random() * 50);
  return {x: Math.round(tx), y: Math.round(ty)};
}

async function humanClick(el) {
  const {x: cx, y: cy} = await humanMoveTo(el);
  const scx   = cx + (window.screenX ?? 0);
  const scy   = cy + (window.screenY ?? 0);
  const mOpts = {bubbles: true, cancelable: true, clientX: cx, clientY: cy, screenX: scx, screenY: scy};
  const pOpts = {...mOpts, pointerId: 1, pointerType: "mouse", isPrimary: true};
  el.dispatchEvent(new PointerEvent("pointerdown", {...pOpts, pressure: 0.5, buttons: 1}));
  el.dispatchEvent(new MouseEvent("mousedown",     {...mOpts, buttons: 1}));
  await sleep(20 + Math.random() * 40);
  el.dispatchEvent(new PointerEvent("pointerup",   {...pOpts, pressure: 0,   buttons: 0}));
  el.dispatchEvent(new MouseEvent("mouseup",       {...mOpts, buttons: 0}));
  el.dispatchEvent(new MouseEvent("click",         {...mOpts, buttons: 0}));
  const prev = document.activeElement;
  if (prev && prev !== el && prev !== document.body && prev !== document.documentElement) {
    prev.dispatchEvent(new FocusEvent("blur",     {bubbles: false, cancelable: false, relatedTarget: el}));
    prev.dispatchEvent(new FocusEvent("focusout", {bubbles: true,  cancelable: false, relatedTarget: el}));
  }
  el.focus();
}

async function humanType(el, text) {
  await humanClick(el);
  await sleep(150 + Math.random() * 200); // focus settle — real users pause before typing
  const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")?.set;
  if (el.value) {
    el.dispatchEvent(new KeyboardEvent("keydown", {key: "a", keyCode: 65, which: 65, ctrlKey: true, bubbles: true, cancelable: true}));
    el.dispatchEvent(new KeyboardEvent("keyup",   {key: "a", keyCode: 65, which: 65, ctrlKey: true, bubbles: true, cancelable: true}));
    await sleep(30 + Math.random() * 40);
    if (setter) setter.call(el, ""); else el.value = "";
    el.dispatchEvent(new InputEvent("input", {data: null, inputType: "deleteContentBackward", bubbles: true}));
    await sleep(40 + Math.random() * 50);
  }
  for (const ch of text) {
    await sleep(_lognormalMs(90, 0.5));  // IKI ~90ms ≈ 65 WPM (careful data-entry pace)
    const cc = ch.charCodeAt(0);
    el.dispatchEvent(new KeyboardEvent("keydown",  {key: ch, keyCode: cc, which: cc, charCode: 0,  bubbles: true, cancelable: true}));
    el.dispatchEvent(new KeyboardEvent("keypress", {key: ch, keyCode: cc, which: cc, charCode: cc, bubbles: true, cancelable: true}));
    await sleep(_lognormalMs(55, 0.45)); // hold time
    const cur = el.value;
    el.dispatchEvent(new InputEvent("beforeinput", {data: ch, inputType: "insertText", bubbles: true, cancelable: true}));
    if (setter) setter.call(el, cur + ch); else el.value = cur + ch;
    el.dispatchEvent(new InputEvent("input",       {data: ch, inputType: "insertText", bubbles: true}));
    el.dispatchEvent(new KeyboardEvent("keyup",    {key: ch, keyCode: cc, which: cc, charCode: 0,  bubbles: true, cancelable: true}));
  }
  await sleep(80 + Math.random() * 120); // post-type settle
  el.dispatchEvent(new Event("change", {bubbles: true}));
}

async function humanSelect(el, value) {
  await humanClick(el);

  const opts      = Array.from(el.options ?? []);
  const targetIdx = opts.findIndex(o => o.value === value);
  const setter    = Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype, "value")?.set;

  function _setTo(v) {
    if (setter) setter.call(el, v); else el.value = v;
    el.dispatchEvent(new Event("input", {bubbles: true}));
  }

  if (targetIdx < 0) {
    _setTo(value);
    el.dispatchEvent(new Event("change",   {bubbles: true}));
    el.dispatchEvent(new FocusEvent("blur",     {bubbles: false, cancelable: false}));
    el.dispatchEvent(new FocusEvent("focusout", {bubbles: true,  cancelable: false}));
    await sleep(50 + Math.random() * 50);
    return;
  }

  const currentIdx = Math.max(0, opts.findIndex(o => o.value === el.value));
  const delta      = targetIdx - currentIdx;
  const absDelta   = Math.abs(delta);

  // Eyes scanning visible options — longer pause when target is farther away.
  await sleep(600 + Math.random() * (absDelta <= 5 ? 600 : 1200));

  if (absDelta > 0 && absDelta <= 5) {
    // Close by: arrow-key navigation with realistic hold time.
    const key     = delta > 0 ? "ArrowDown" : "ArrowUp";
    const keyCode = delta > 0 ? 40 : 38;
    for (let i = 0; i < absDelta; i++) {
      el.dispatchEvent(new KeyboardEvent("keydown", {key, keyCode, which: keyCode, bubbles: true, cancelable: true}));
      await sleep(_lognormalMs(90, 0.35));
      _setTo(opts[currentIdx + (delta > 0 ? i + 1 : -(i + 1))].value);
      el.dispatchEvent(new KeyboardEvent("keyup",   {key, keyCode, which: keyCode, bubbles: true, cancelable: true}));
      await sleep(_lognormalMs(160, 0.35) + (i === 0 ? 80 : 0));
    }
  } else if (absDelta > 5) {
    // Far away: type first 1–2 chars of option text to jump, like a real user.
    const chars = opts[targetIdx].text.trim().slice(0, 2);
    for (const ch of chars) {
      const cc = ch.toUpperCase().charCodeAt(0);
      el.dispatchEvent(new KeyboardEvent("keydown",  {key: ch, keyCode: cc, which: cc, bubbles: true, cancelable: true}));
      el.dispatchEvent(new KeyboardEvent("keypress", {key: ch, keyCode: cc, which: cc, charCode: cc, bubbles: true, cancelable: true}));
      await sleep(_lognormalMs(90, 0.4));
      el.dispatchEvent(new KeyboardEvent("keyup",    {key: ch, keyCode: cc, which: cc, bubbles: true, cancelable: true}));
      await sleep(_lognormalMs(140, 0.4));
    }
    _setTo(value);
    await sleep(150 + Math.random() * 150);
  }

  // "I've found it" pause before committing.
  await sleep(180 + Math.random() * 200);

  // Commit: change + blur mirrors Tab or clicking away to close the dropdown.
  el.dispatchEvent(new Event("change",   {bubbles: true}));
  el.dispatchEvent(new FocusEvent("blur",     {bubbles: false, cancelable: false}));
  el.dispatchEvent(new FocusEvent("focusout", {bubbles: true,  cancelable: false}));
  await sleep(100 + Math.random() * 100);
}

function storageGet(keys) {
  return new Promise(resolve => chrome.storage.local.get(keys, resolve));
}

function storageSet(obj) {
  return new Promise(resolve => chrome.storage.local.set(obj, resolve));
}

function sendBgMessage(msg) {
  return new Promise(resolve => chrome.runtime.sendMessage(msg, resolve));
}

function _logEntry(msg) {
  sendBgMessage({type: "log-entry", msg}).catch(() => {});
}

// Forward all console output to the run logger so the saved .log file mirrors
// what appears in DevTools. Runs once at content script load.
(function _patchConsole() {
  const _fwd = (level, args) => {
    try {
      const text = args.map(a => (a instanceof Error ? a.stack : typeof a === "object" ? JSON.stringify(a) : String(a))).join(" ");
      _logEntry(`[${level}] ${text}`);
    } catch (_) {}
  };
  for (const level of ["log", "warn", "error", "info"]) {
    const orig = console[level].bind(console);
    console[level] = function (...args) { orig(...args); _fwd(level.toUpperCase(), args); };
  }
})();

// ---------------------------------------------------------------------------
// Badge
// ---------------------------------------------------------------------------

function injectBadge() {}
function setBadge() {}

// ---------------------------------------------------------------------------
// Shared: language switch
// ---------------------------------------------------------------------------

async function switchToEnglish() {
  const {"lang-switch-enabled": enabled = false} = await storageGet("lang-switch-enabled");
  if (!enabled) { console.log("[OctoProbe] Lang switch disabled — skipping."); return; }

  await sleep(300);
  const langSel = await waitFor(
    () => LANG_SELECTORS.map(s => document.querySelector(s)).find(Boolean),
    5000
  );
  if (!langSel) { console.warn("[OctoProbe] Language select not found."); return; }
  if (langSel.value === "ENG") { console.log("[OctoProbe] Already English."); return; }

  setBadge("Switching to English…", "#f0c040");
  await humanSelect(langSel, "ENG");
  await sleep(1200 + Math.random() * 600);
}

// ---------------------------------------------------------------------------
// Shared: click Login button
// ---------------------------------------------------------------------------

async function clickLoginLink() {
  setBadge("Finding Login…", "#f0c040");
  const el = await waitFor(
    () => findBySelectors(LOGIN_LINK_SELECTORS) || findByText(["a","button"], ["login","log in","entrar","iniciar sessão"]),
    6000
  );
  if (!el) { setBadge("Login button not found", "#ff6b6b"); return; }
  setBadge("Clicking Login…", "#f0c040");
  await humanClick(el);
}

// ---------------------------------------------------------------------------
// Login workflow
// ---------------------------------------------------------------------------

const LOGIN_SUBMIT_SELECTORS = [
  "#formLogin button[type='submit']",
  "#formLogin input[type='submit']",
  "form#login button[type='submit']",
  "form button[type='submit']",
  "form input[type='submit']",
  ".btn-login",
];

// ---------------------------------------------------------------------------
// Warmup (legacy — kept only for WARMUP_SITES / WARMUP_DWELL_MS constants)
// ---------------------------------------------------------------------------

async function warmupStep() {
  const {
    "warmup-end-time":   endTime,
    "warmup-site-index": siteIdx = 0,
  } = await storageGet(["warmup-end-time", "warmup-site-index"]);

  const now      = Date.now();
  const timeLeft = Math.max(0, Math.round((endTime - now) / 1000));

  if (now >= endTime) {
    console.log("[OctoProbe] Warmup complete — transitioning to registration");
    await storageSet({"workflow-type": "register", "workflow-step": "home", "warmup-site-index": 0});
    location.href = `https://${TARGET_HOST}/VistosOnline/`;
    return;
  }

  setBadge(`Warming up… ${timeLeft}s`, "#888888");
  console.log(`[OctoProbe] Warmup site ${siteIdx}: ${location.href} — ${timeLeft}s left`);

  let scrollPos = 0;
  let _scrollTimer = null;
  const _doScroll = () => {
    scrollPos += 70 + Math.round(_lognormalMs(90, 0.5));
    window.scrollTo({top: scrollPos, behavior: "smooth"});
    _scrollTimer = setTimeout(_doScroll, _lognormalMs(5200, 0.4));
  };
  _scrollTimer = setTimeout(_doScroll, _lognormalMs(3000, 0.5));

  setTimeout(async () => {
    clearTimeout(_scrollTimer);
    const nextIdx = siteIdx + 1;

    if (Date.now() >= endTime || nextIdx >= WARMUP_SITES.length * 2) {
      await storageSet({"workflow-type": "register", "workflow-step": "home", "warmup-site-index": 0});
      location.href = `https://${TARGET_HOST}/VistosOnline/`;
    } else {
      await storageSet({"warmup-site-index": nextIdx});
      location.href = WARMUP_SITES[nextIdx % WARMUP_SITES.length];
    }
  }, WARMUP_DWELL_MS);
}

async function _clearWorkflowFailed(reason, code = 0) {
  setBadge(reason, "#ff6b6b");
  throw Object.assign(new Error(reason), {errorCode: code, isWorkflowError: true});
}

async function _selectNationality(natEl, person) {
  // Nationality is always CPV (Cape Verde) — select directly by option value.
  person.nationality = "CPV";
  await storageSet({"register-person": Object.assign({}, person)});
  await humanSelect(natEl, "CPV");
  console.log("[OctoProbe] Nationality selected: CPV (Cape Verde)");
}

// Returns selectors of fields that are visibly present but have an empty value.
function _checkFormFields() {
  const sels = [
    "#name", "#surname", "#username", "#password", "#valPassword",
    "#bday", "#traveldoc", "#email", "#gender", "#nationality",
  ];
  return sels.filter(sel => {
    const el = document.querySelector(sel);
    return !el || !el.value.trim();
  });
}

// Re-fill only the fields listed in `missing`.
async function _refillMissing(missing, person, email) {
  const textMap = {
    "#name":        person.name,
    "#surname":     person.surname,
    "#username":    person.username,
    "#password":    person.password,
    "#valPassword": person.password,
    "#bday":        person.birth_date,
    "#traveldoc":   person.traveldoc,
    "#email":       email,
  };
  for (const sel of missing) {
    const el = document.querySelector(sel);
    if (!el) { console.warn(`[OctoProbe] Refill: ${sel} not in DOM`); continue; }
    console.log(`[OctoProbe] Refill: ${sel}`);
    if (sel === "#gender") {
      await humanSelect(el, person.gender);
    } else if (sel === "#nationality") {
      await _selectNationality(el, person);
    } else {
      const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")?.set;
      if (setter) setter.call(el, ""); else el.value = "";
      el.dispatchEvent(new Event("input", {bubbles: true}));
      await humanType(el, textMap[sel] ?? "");
    }
    await sleep(300 + Math.random() * 300);
  }
}

async function fillRegisterForm(person, email) {
  // Wait for AJAX-loaded fields before touching anything (slow proxies load skeleton
  // first, then inject fields after a network round-trip).
  const nameField = await waitFor(() => document.querySelector("#name"), 45000);
  if (!nameField) {
    console.warn("[OctoProbe] Registration form fields did not load (form_incomplete)");
    return false;
  }

  // Field order mirrors natural reading / tab order; inter-field pauses are randomised.
  const textFields = [
    ["#name",        person.name],
    ["#surname",     person.surname],
    ["#username",    person.username],
    ["#password",    person.password],
    ["#valPassword", person.password],
    ["#bday",        person.birth_date],
    ["#traveldoc",   person.traveldoc],
    ["#email",       email],
  ];

  for (const [sel, value] of textFields) {
    const el = document.querySelector(sel);
    if (!el) { console.warn(`[OctoProbe] Field not found: ${sel}`); continue; }
    await humanType(el, value);
    // Inter-field pause: real users glance at next field label, think, reposition hand.
    // Base 900–2100ms; 20% chance of a longer "reading" pause (2–4s).
    await sleep(900 + Math.random() * 1200 + (Math.random() < 0.20 ? 2000 + Math.random() * 2000 : 0));
  }

  // Select fields — click, brief look, choose
  const genderEl = document.querySelector("#gender");
  if (genderEl) await humanSelect(genderEl, person.gender);
  await sleep(300 + Math.random() * 400);

  const natEl = document.querySelector("#nationality");
  if (natEl) await _selectNationality(natEl, person);
  await sleep(300 + Math.random() * 400);

  // Post-fill validation: refill any fields still empty (e.g. AJAX-slow DOM, concurrent
  // or a field that wasn't in the DOM during the initial pass).
  const missing = _checkFormFields();
  if (missing.length) {
    console.warn("[OctoProbe] Post-fill check: refilling", missing);
    await _refillMissing(missing, person, email);
    // If still empty after refill, report to caller so cmd-register-fill returns the error
    const stillMissing = _checkFormFields();
    if (stillMissing.length) {
      console.warn("[OctoProbe] Fields still missing after refill:", stillMissing);
      return false;
    }
  }
  return true;
}

// CDP-based direct click was removed: it consistently triggers the challenge grid
// (image CAPTCHA) on all sessions and creates a detectable debugger-attach timing spike.
// All CAPTCHA solving now goes through the API solver path.
async function waitForRecaptcha() {
  return _waitForRecaptchaApi();
}

// Extract the reCAPTCHA site key from the current page.
async function _extractSiteKey() {
  const el = document.querySelector("[data-sitekey]");
  if (el?.dataset?.sitekey) return el.dataset.sitekey;
  const fr = document.querySelector("iframe[src*='recaptcha']");
  if (fr) { const m = fr.src.match(/[?&]k=([A-Za-z0-9_-]+)/); if (m) return m[1]; }
  // Fallback: walk MAIN world internals via background scripting API
  const r = await sendBgMessage({type: "get-recaptcha-sitekey"}).catch(() => null);
  return r?.siteKey ?? null;
}

// Minimum elapsed time before a solver token is accepted.
// Tokens arriving faster than this are padded with a sleep — an instant solve
// is a strong bot signal even if the token itself is valid.
const _MIN_API_SOLVE_MS = 12000;

// Session-random event names — generated locally AND synced with background.
// Never fall back to static "octo-*" strings — generate random names immediately.
const _localSk   = Math.random().toString(36).slice(2, 10);
let _evtAlert    = 'a' + _localSk;  // random at load — no static fingerprint
let _evtRcp      = 'r' + _localSk;
let _sessionSk   = _localSk;
let _evtKeysReady = false;
async function _ensureEvtKeys() {
  if (_evtKeysReady) return;
  const keys = await sendBgMessage({type: "get-session-keys"}).catch(() => null);
  // Prefer background-generated keys (shared with inject-recaptcha-token handler);
  // fall back to locally generated keys — never use static "octo-*" names.
  if (keys?.evtAlert) { _evtAlert = keys.evtAlert; _evtRcp = keys.evtRcp; }
  if (keys?.sk) _sessionSk = keys.sk;
  _evtKeysReady = true;
}

// API mode — background solves, pads, and injects in one round-trip.
// Content.js drifts the mouse concurrently while the background works.
async function _waitForRecaptchaApi() {
  setBadge("API: solving reCAPTCHA…", "#9060cc");
  await _ensureEvtKeys();

  const siteKey = await _extractSiteKey();
  if (!siteKey) {
    console.error("[OctoProbe] API mode: sitekey not found on page");
    return false;
  }
  const action = document.querySelector("[data-sitekey][data-action]")?.dataset?.action
              ?? document.querySelector("[data-action]")?.dataset?.action
              ?? null;
  console.log("[OctoProbe] Sending to solver — siteKey:", siteKey, "action:", action);
  _logEntry(`captcha: sending to solver siteKey=${siteKey} action=${action}`);

  // Fire solve+inject in background; drift mouse here while it works.
  let _drifting = true;
  const bgDone = sendBgMessage({
    type: "solve-and-inject-recaptcha",
    pageUrl: location.href, siteKey, action,
    minSolveMs: _MIN_API_SOLVE_MS,
  }).catch(() => null);

  const solved = await new Promise(resolve => {
    // Background fires _evtRcp event via executeScript when injection is done.
    document.addEventListener(_evtRcp, () => { _drifting = false; resolve(true); }, {once: true});
    bgDone.then(r => {
      if (!r?.ok) {
        _drifting = false;
        console.error("[OctoProbe] solve-and-inject failed:", r?.error);
        _logEntry(`captcha: solver failed — ${r?.error ?? "no result"}`);
        resolve(false);
      } else {
        console.log(`[OctoProbe] Token accepted after ${r.totalMs}ms total.`);
        _logEntry(`captcha: token accepted after ${r.totalMs}ms`);
      }
    }).catch(() => { _drifting = false; resolve(false); });
    setTimeout(() => { _drifting = false; resolve(false); }, 45000);

    // Drift mouse while solve+inject is in progress — zero movement is a bot signal.
    (async () => {
      while (_drifting) {
        await sleep(200 + Math.random() * 400);
        if (!_drifting) break;
        const nx = Math.max(0, Math.min(innerWidth,  (window._mX ?? innerWidth  * 0.5) + (Math.random() - 0.5) * 8));
        const ny = Math.max(0, Math.min(innerHeight, (window._mY ?? innerHeight * 0.5) + (Math.random() - 0.5) * 8));
        document.dispatchEvent(new MouseEvent("mousemove", {
          bubbles: true, clientX: nx, clientY: ny,
          screenX: nx + (window.screenX ?? 0), screenY: ny + (window.screenY ?? 0),
        }));
        window._mX = nx; window._mY = ny;
      }
    })();
  });

  if (solved) console.log("[OctoProbe] reCAPTCHA injected (API mode).");
  return solved;
}

// Override window.alert (and optionally confirm/prompt) in the page's MAIN world.
// confirmToo=true adds confirm/prompt — only pass this at form-submit time, NOT during
// login, because reCAPTCHA Enterprise checks native-function toString() for tampering.
async function injectAlertCapture(confirmToo = false) {
  await _ensureEvtKeys();
  const r = await sendBgMessage({type: "inject-alert-capture", confirmToo}).catch(() => null);
  if (r?.ok) {
    console.log(`[OctoProbe] alert${confirmToo ? "/confirm/prompt" : ""} suppressed (CSP-safe)`);
    return r;
  }
  // DOM <script> fallback removed: injecting a <script> tag into the page DOM is detectable
  // via document.scripts enumeration and MutationObserver. If the scripting API fails,
  // alerts will fire natively (browser default) — better than exposing an automation artifact.
  console.warn("[OctoProbe] scripting API injection failed — alert capture skipped");
}

// Returns {linkToken, codeToken} where linkToken is the short-hex from the email URL
// and codeToken is the UUID code to enter in the form — or null if not found in time.
async function waitForEmailToken(jwt, maxMs = 120000) {
  const deadline = Date.now() + maxMs;

  const {"email-provider": emailProvider = "mailtm"} = await storageGet("email-provider");
  const isMailTm = emailProvider === "mailtm";

  let bgRawToken = null;
  const bgListener = (msg) => { if (msg.type === "email-token") bgRawToken = msg.token; };
  chrome.runtime.onMessage.addListener(bgListener);

  while (Date.now() < deadline) {
    // Check email-code-token first (CF mode stores UUID there); fall back to email-token (mail.tm).
    const {["email-code-token"]: storedCode, ["email-token"]: storedLink} = await storageGet(["email-code-token", "email-token"]);
    const stored = storedCode ?? storedLink;
    if (stored) {
      console.log("[OctoProbe] Email token from bg-storage:", stored);
      chrome.runtime.onMessage.removeListener(bgListener);
      const isHex32 = /^[0-9a-f]{32}$/i.test(stored);
      return isHex32
        ? {linkToken: stored.toLowerCase(), codeToken: null}
        : {linkToken: null, codeToken: stored.toLowerCase()};
    }

    if (bgRawToken) {
      console.log("[OctoProbe] Email token from bg-message:", bgRawToken);
      chrome.runtime.onMessage.removeListener(bgListener);
      // bg sends codeToken ?? linkToken — prefer UUID, fall back to hex linkToken
      const isHex32 = /^[0-9a-f]{32}$/i.test(bgRawToken);
      return isHex32
        ? {linkToken: bgRawToken.toLowerCase(), codeToken: null}
        : {linkToken: null, codeToken: bgRawToken.toLowerCase()};
    }

    if (!isMailTm) {
      // CF email: background poller is the only delivery path — just wait.
      await sleep(5000);
      continue;
    }

    try {
      const r = await fetch(`${MAILTM}/messages`, {headers: {Authorization: `Bearer ${jwt}`}});
      if (r.status === 401) {
        console.warn("[OctoProbe] mail.tm JWT expired (401) — aborting email poll");
        chrome.runtime.onMessage.removeListener(bgListener);
        return null;
      }
      const data  = await r.json();
      const items = data["hydra:member"] ?? [];

      console.log(`[OctoProbe] mail.tm inbox: ${items.length} message(s)`);

      for (const item of items) {
        const mr  = await fetch(`${MAILTM}/messages/${item.id}`, {headers: {Authorization: `Bearer ${jwt}`}});
        const msg = await mr.json();

        console.log("[OctoProbe] Email →", {
          from:    msg.from?.address,
          subject: msg.subject,
          textPreview: (msg.text ?? "").slice(0, 400),
          htmlPreview: (Array.isArray(msg.html) ? msg.html.join(" ") : (msg.html ?? "")).replace(/<[^>]+>/g, " ").slice(0, 400),
        });

        const tokens = extractTokenFromMsg(msg);
        if (tokens) {
          console.log("[OctoProbe] Tokens extracted:", tokens);
          chrome.runtime.onMessage.removeListener(bgListener);
          return tokens;
        }
        console.warn("[OctoProbe] No token pattern matched in this email.");
      }
    } catch (e) {
      console.warn("[OctoProbe] mail.tm poll error:", e);
    }

    await sleep(5000);
  }

  chrome.runtime.onMessage.removeListener(bgListener);
  return null;
}

// Returns {linkToken, codeToken} where:
//   linkToken  = raw 32-char hex from the email's verification URL (?token=…)
//   codeToken  = UUID with dashes displayed as the entry code in the email body
// Either field may be null if not found in the email.
function extractTokenFromMsg(msg) {
  const text = msg?.text ?? "";
  const rawHtml = Array.isArray(msg?.html) ? msg.html.join(" ") : (msg?.html ?? "");
  const html = rawHtml.replace(/<[^>]+>/g, " ");
  const body = text || html;

  // Link token: 32-char hex embedded in a ?token= URL query parameter.
  const linkMatch = body.match(/[?&]token=([0-9a-f]{32})\b/i);
  const linkToken = linkMatch ? linkMatch[1].toLowerCase() : null;

  // Code token: UUID-with-dashes in body text, NOT inside a URL.
  // Strip all URLs first so ?token=<32-hex> expansions aren't caught here.
  const bodyNoUrls = body.replace(/https?:\/\/[^\s<>"]+/gi, " ");
  const codeMatch  = bodyNoUrls.match(/\b([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\b/i);
  const codeToken  = codeMatch ? codeMatch[1].toLowerCase() : null;

  if (!linkToken && !codeToken) {
    // Last-resort fallback: any UUID or hex in body.
    const uuidM = body.match(/\b([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\b/i);
    if (uuidM) return {linkToken: null, codeToken: uuidM[1].toLowerCase()};
    const hexM  = body.match(/\b([0-9a-f]{32})\b/i);
    if (hexM)  return {linkToken: hexM[1].toLowerCase(), codeToken: null};
    return null;
  }

  console.log(`[OctoProbe] Email tokens — link: ${linkToken ?? "?"}, code: ${codeToken ?? "?"}`);
  return {linkToken, codeToken};
}

// ---------------------------------------------------------------------------
// Network quality probe
// ---------------------------------------------------------------------------

// Ping Google's dedicated connectivity endpoint twice and return the average RTT.
// Returns {avg, good} — "good" means both probes completed within threshold.
const _NET_BAD_MS  = 2000;  // avg RTT above this = poor proxy, stop workflow
const _NET_TIMEOUT = 2500;  // per-probe abort timeout — exceeding this already proves bad proxy
const _NET_PROBE   = "https://www.google.com/generate_204";

async function checkNetworkQuality() {
  setBadge("Checking network…", "#f0c040");
  const times = [];
  let timedOut = false;
  for (let i = 0; i < 2; i++) {
    const t0 = performance.now();
    try {
      const ctrl = new AbortController();
      const tid  = setTimeout(() => ctrl.abort(), _NET_TIMEOUT);
      await fetch(_NET_PROBE, {method:"HEAD", mode:"no-cors", cache:"no-store", signal: ctrl.signal});
      clearTimeout(tid);
      times.push(performance.now() - t0);
    } catch(_) {
      timedOut = true;
      times.push(_NET_TIMEOUT);
      break; // first probe timed out — proxy is dead, no need for second probe
    }
    if (i === 0) await sleep(300); // small gap between probes
  }
  const avg = Math.round(times.reduce((a,b) => a+b,0) / times.length);
  const good = !timedOut && avg < _NET_BAD_MS;
  console.log(`[OctoProbe] Network RTT: ${times.map(t=>Math.round(t)).join(", ")}ms — avg ${avg}ms — ${good ? "OK" : "POOR"}`);
  setBadge(`Network: ${avg}ms ${good ? "OK" : "POOR"}`, good ? "#00d4aa" : "#ff6b6b");
  await sleep(600); // brief pause so the badge is readable
  return {avg, good};
}

async function _checkProxyQuality() {
  const net = await checkNetworkQuality();
  let proxy = {queryOk: false};
  try {
    const r = await fetch(
      "http://ip-api.com/json?fields=status,query,countryCode,isp,org,proxy,hosting,mobile",
      {cache: "no-store"}
    );
    if (r.ok) {
      const d = await r.json();
      if (d.status === "success") {
        proxy = {queryOk: true, ip: d.query, country: d.countryCode,
                 isp: d.isp, org: d.org, isProxy: d.proxy,
                 isHosting: d.hosting, isMobile: d.mobile};
      }
    }
  } catch (_) {}
  return {network: {avgRtt: net.avg, good: net.good}, proxy};
}

// ---------------------------------------------------------------------------
// Cookie consent dismissal
// ---------------------------------------------------------------------------

async function dismissCookieConsent() {
  // Cookie banners are injected asynchronously — poll for up to 4 s so we don't
  // miss a banner that appears after document_end fires.
  const COOKIE_SELECTORS = [
    "#onetrust-accept-btn-handler",
    "#accept-all-cookies",
    "#acceptAllCookies",
    "#CybotCookiebotDialogBodyLevelButtonLevelOptinAllowAll",
    "[id*='cookie'][id*='accept' i]",
    "[id*='cookie'][id*='agree' i]",
    "[id*='consent'][id*='accept' i]",
    "[class*='cookie'][class*='accept' i]",
    "[class*='consent'][class*='accept' i]",
  ];
  const COOKIE_TEXTS = ["accept all", "accept cookies", "aceitar todos", "aceitar tudo", "allow all", "aceitar"];

  const btn = await waitFor(() =>
    findBySelectors(COOKIE_SELECTORS) ||
    findByText(["button", "a", "input"], COOKIE_TEXTS),
  4000, 200);

  if (!btn) return; // no banner on this page — proceed normally

  console.log("[OctoProbe] Cookie consent banner found — dismissing");
  await sleep(600 + Math.random() * 600);
  await humanClick(btn);
  await sleep(400 + Math.random() * 400);
  console.log("[OctoProbe] Cookie consent dismissed");
}

// ---------------------------------------------------------------------------
// Visa workflow — helpers
// ---------------------------------------------------------------------------

// Execute arbitrary JS in the MAIN world via the background service worker.
async function _callPageFn(code) {
  return sendBgMessage({type: "exec-page-script", code}).catch(() => null);
}

// Post a message to MAIN world and wait for a reply posted back via window.postMessage.
function _waitPageMessage(expectType, timeoutMs = 30000) {
  return new Promise(resolve => {
    const tid = setTimeout(() => { window.removeEventListener("message", handler); resolve(null); }, timeoutMs);
    function handler(ev) {
      if (ev.source !== window || ev.data?.type !== expectType) return;
      clearTimeout(tid);
      window.removeEventListener("message", handler);
      resolve(ev.data);
    }
    window.addEventListener("message", handler);
  });
}

// ---------------------------------------------------------------------------
// Visa workflow — page state detection (URL + DOM, no storage dependency)
// ---------------------------------------------------------------------------
//
// State machine:
//   "not-logged-in"  — on target site but no authenticated nav
//   "auth"           — on Authentication page (login form present)
//   "logged-in"      — authenticated home/dashboard, no workflow page active
//   "questionnaire"  — on /Questionario with questForm present
//   "form"           — on /Formulario with visa form present
//   "schedule"       — on Schedule/ScheduleController page
//   "session-lost"   — URL implies a workflow page but required DOM missing
//   "unknown"        — unrecognised page on target host

function _detectPageState() {
  if (location.hostname !== TARGET_HOST) return "off-site";
  const path = location.pathname;

  if (/Questionario/i.test(path)) {
    // Return "session-lost" if not authenticated — server may serve the page shell without
    // redirecting, but AJAX content never loads for unauthenticated users.
    const hasAuthNav = !!(
      document.querySelector("a[href*='Questionario']") ||
      document.querySelector(".user-label") ||
      document.querySelector("a[href*='logout']")
    );
    return hasAuthNav ? "questionnaire" : "session-lost";
  }
  if (/Formulario/i.test(path)) {
    return document.querySelector("form[name='vistoForm'], #vistoForm") ? "form" : "session-lost";
  }
  if (/Schedule|Agendamento|ScheduleController/i.test(path)) {
    return "schedule";
  }
  if (/Authentication|authentication/i.test(path + location.search)) {
    return location.search.includes("token=") ? "token" : "auth";
  }

  // Home/dashboard or any other page: detect login by authenticated-only nav elements.
  const loggedIn = !!(
    document.querySelector("a[href*='Questionario']") ||
    document.querySelector("a[href*='Pedidos']") ||
    document.querySelector(".user-label") ||
    document.querySelector("a[href*='logout']")
  );
  return loggedIn ? "logged-in" : "not-logged-in";
}

// ---------------------------------------------------------------------------
// Visa workflow — navigate to questionnaire (when already logged in)
// ---------------------------------------------------------------------------

async function visaStepGoToQuestionnaire() {
  setBadge("Visa: opening questionnaire…", "#9060cc");

  // If login redirected directly to the questionnaire page, use _detectPageState to decide.
  // Reloading when authenticated would destroy AJAX session state and #questForm would never
  // appear. If NOT authenticated, signal "session-lost" so F4 fails fast.
  if (/Questionario/i.test(location.pathname)) {
    const st = _detectPageState();
    console.log("[OctoProbe] Already on questionnaire page — signalling page-ready state=" + st);
    chrome.runtime.sendMessage({type: "page-ready", state: st, url: location.href}).catch(() => {});
    return;
  }

  await switchToEnglish();

  // Wait for the home page to be fully initialised (window.onload fired).
  // The home page fires session-init scripts on window.onload (~4 s after load).
  // Navigating before those scripts complete means the questionnaire AJAX has no
  // server-side session to attach to, so #questForm is never injected.
  if (document.readyState !== "complete") {
    await waitFor(() => document.readyState === "complete" ? true : null, 15000, 200);
  }
  await sleep(3000 + Math.random() * 1000); // dwell after window.onload scripts finish

  // Prefer clicking the nav link (most natural interaction for logged-in users).
  const navLink = document.querySelector("a[href*='Questionario']");
  if (navLink && isVisible(navLink)) {
    await humanClick(navLink);
    return;
  }
  // Direct navigation fallback — works even if nav isn't rendered yet.
  location.href = "/VistosOnline/Questionario";
}

// ---------------------------------------------------------------------------
// Visa workflow — step: questionnaire
// ---------------------------------------------------------------------------

// Known answer cascade. Each entry is [questionSelectId, value].
// Full questionnaire cascade for CPV nationality → short-stay Schengen visa.
// Q1 (nationality) is disabled and auto-fires goNext("1","CPV") on page load.
// Each subsequent question arrives via AJAX — we must wait for it before filling.
// Chain: Q1(auto) → Q21(CPV) → Q2(01) → Q3(SCH) → Q5(O) → Q6(10) → Q16(FAM_N) → result
const _QUESTIONNAIRE_STEPS = [
  // [selectId, qNum, value, label]
  // qNum matches the id_pergunta argument to goNext() — same as the trailing number in selectId.
  ["cb_question_21",  21,  "CPV",    "Country of residence"],
  ["cb_question_2",    2,  "01",     "Passport type (ordinary)"],
  ["cb_question_3",    3,  "SCH",    "Stay duration (up to 90 days)"],
  ["cb_question_5",    5,  "O",      "Seasonal work? (no)"],
  ["cb_question_6",    6,  "10",     "Purpose of stay (tourism)"],
  ["cb_question_16",  16,  "FAM_N",  "Traveling with EU family? (no)"],
];

// Wait for a questionnaire select to be present in DOM AND have its option populated.
async function _waitForQSelect(selId, maxMs = 15000) {
  return waitFor(() => {
    const el = document.getElementById(selId);
    if (!el) return null;
    // Options length > 1 means the server populated the list (index 0 is placeholder).
    return el.options.length > 1 ? el : null;
  }, maxMs, 300);
}

async function visaStepQuestionnaire() {
  setBadge("Visa: questionnaire loading…", "#9060cc");

  // Wait for #questForm (the questionnaire form) to be present.
  // The form is injected via AJAX after DOMContentLoaded — can take 15+ s.
  const questForm = await waitFor(() => document.querySelector("#questForm"), 90000);
  if (!questForm) {
    // Form never appeared — session may be invalid even though URL is /Questionario.
    // Navigate to auth so the dispatch loop re-runs login from scratch.
    console.warn("[OctoProbe] Questionnaire form not found — reloading page to retry");
    setBadge("Visa: questionnaire reload…", "#9060cc");
    location.reload();
    return;
  }

  // Q1 (nationality). On some sessions it auto-fires; on others the select is
  // interactive and must be set manually. Check the current state and act accordingly.
  setBadge("Visa: Q1 — checking nationality…", "#9060cc");

  // Wait briefly for Q21 in case Q1 already auto-fired on page load.
  let firstQ = await _waitForQSelect("cb_question_21", 5000);

  if (!firstQ) {
    // Q21 not yet present — Q1 did not auto-fire. Check/set the nationality select.
    const q1El = document.getElementById("cb_question_1");
    if (q1El) {
      // Wait for its options to be populated (same readiness check as other questions).
      const q1Ready = await waitFor(() => q1El.options.length > 1 ? q1El : null, 8000, 300);
      if (q1Ready) {
        const cur = q1El.value;
        const isCpv = cur === "CPV";
        if (!isCpv) {
          console.log(`[OctoProbe] Q1 current value="${cur}" — selecting CPV`);
          await sleep(400 + Math.random() * 400);
          const setter = Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype, "value")?.set;
          if (setter) setter.call(q1El, "CPV"); else q1El.value = "CPV";
        } else {
          console.log("[OctoProbe] Q1 already set to CPV");
        }
      } else {
        console.warn("[OctoProbe] Q1 select options did not load");
      }
    } else {
      console.warn("[OctoProbe] cb_question_1 not found — attempting goNext(1,'CPV') anyway");
    }

    await sleep(300 + Math.random() * 200);
    console.log("[OctoProbe] Calling goNext(1,'CPV')");
    await _callPageFn(`if(typeof goNext==='function') goNext(1,'CPV');`);

    firstQ = await _waitForQSelect("cb_question_21", 15000);
    if (!firstQ) { await _clearWorkflowFailed("Visa: Q21 not appeared after goNext(1,'CPV')", E.VISA_FAILED); return; }
  } else {
    console.log("[OctoProbe] Q1 auto-fired — Q21 already present");
  }

  // Fill each cascading question in order.
  for (const [selId, qNum, value, label] of _QUESTIONNAIRE_STEPS) {
    setBadge(`Visa: Q — ${label}…`, "#9060cc");

    // Wait for this question's select to appear and be populated with options.
    const el = await _waitForQSelect(selId, 12000);
    if (!el) {
      console.warn(`[OctoProbe] Q select #${selId} did not appear — skipping`);
      continue;
    }

    // Brief human-like pause before interacting.
    await sleep(400 + Math.random() * 500);

    // Update the displayed value via native setter (visual only — no event needed).
    const setter = Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype, "value")?.set;
    if (setter) setter.call(el, value); else el.value = value;

    // Call goNext() directly in MAIN world. Dispatching a "change" event from the
    // isolated world does not reliably trigger inline onchange handlers set in MAIN world,
    // so we bypass the event entirely and invoke goNext() ourselves. goNext() updates
    // the form hidden fields (nacionalidade / pais_residencia / tipo_passaporte) and
    // fires the XHR that appends the next question row to the table.
    await _callPageFn(`if(typeof goNext==="function") goNext(${qNum},"${value}");`);

    console.log(`[OctoProbe] Q #${selId} (Q${qNum}) → ${value}`);

    // Allow the XHR to complete and the next question to be injected into DOM.
    await sleep(1500 + Math.random() * 800);
  }

  // After all answers, poll for btnContinue to become visible.
  // The server sets display:block when tipo_visto gets a valid (non-XX) value.
  setBadge("Visa: waiting for Form button…", "#9060cc");
  const continueBtn = await waitFor(
    () => {
      const btn = document.querySelector("#btnContinue");
      return btn && btn.style.display !== "none" && btn.offsetParent !== null ? btn : null;
    },
    20000, 500
  );
  if (!continueBtn) { await _clearWorkflowFailed("Visa: Form button did not appear", E.VISA_FAILED); return; }

  await sleep(600 + Math.random() * 600);
  setBadge("Visa: submitting questionnaire…", "#9060cc");
  await humanClick(continueBtn);
}

// ---------------------------------------------------------------------------
// Visa workflow — step: 6-tab form
// ---------------------------------------------------------------------------

// Sets a form field by name using DOM property setter + events.
// Returns true if the field was found.
async function _setField(name, value) {
  if (!value) return false;
  const el = document.querySelector(`[name="${name}"], #${name}`);
  if (!el) { console.warn(`[OctoProbe] Form field not found: ${name}`); return false; }

  // Scroll element into view before mouse trajectory — humanMoveTo uses
  // getBoundingClientRect() which returns 0×0 for off-screen/hidden elements,
  // causing the cursor to fly to (0, 0) which is a strong bot signal.
  el.scrollIntoView({behavior: "smooth", block: "nearest"});
  await sleep(120 + Math.random() * 150);

  if (el.tagName === "SELECT") {
    await humanSelect(el, value);
  } else {
    await humanType(el, value);
  }
  return true;
}

// Switch to a form tab by calling mudarTab in MAIN world.
async function _switchToTab(prevTab, nextTab) {
  await _callPageFn(`if(typeof mudarTab==="function") mudarTab(${prevTab},${nextTab});`);
  await sleep(800 + Math.random() * 500);
}

async function _submitVisaForm() {
  setBadge("Visa: submitting form…", "#9060cc");
  await injectAlertCapture(true);
  await _callPageFn(`(function(){ if(typeof mudarTab==="function") mudarTab(5,6); })();`);
  await sleep(1500 + Math.random() * 500);
  const formSubmitBtn = await waitFor(
    () => {
      const cands = [
        document.querySelector("#btnSubmit"),
        document.querySelector("input[type='submit'][value*='Submit' i]"),
        document.querySelector("input[type='submit'][value*='Enviar' i]"),
        document.querySelector("button[type='submit']"),
      ];
      return cands.find(el => el && isVisible(el)) ?? null;
    },
    10000
  );
  if (!formSubmitBtn) throw new Error("Visa: form submit button not found");
  await sleep(800 + Math.random() * 600);
  await humanClick(formSubmitBtn);
  const alertMsg = await new Promise(resolve => {
    const tid = setTimeout(() => resolve(null), 8000);
    document.addEventListener(_evtAlert, (e) => { clearTimeout(tid); resolve(e.detail.msg); }, {once: true});
  });
  if (alertMsg) console.warn("[OctoProbe] Form submit alert:", alertMsg);
  setBadge("Visa: form submitted…", "#9060cc");
  return {ok: true};
}

async function visaStepForm({submitAfter = true} = {}) {
  setBadge("Visa: waiting for form…", "#9060cc");

  // Wait for the form element.
  const form = await waitFor(() => document.querySelector("form[name='vistoForm'], #vistoForm, form"), 20000);
  if (!form) { await _clearWorkflowFailed("Visa: form not found", E.VISA_FAILED); return; }

  // ── Storage: pull person data, visa config ───────────────────────────────
  const {
    "real-person-input":   rp,
    "visa-arrival-date":   arrivalDate,
    "visa-departure-date": departureDate,
    "visa-consular-post":  storedPostoId,
  } = await storageGet(["real-person-input","visa-arrival-date","visa-departure-date","visa-consular-post"]);

  const person = rp ?? {};

  // consulPost is required — no fallback. Abort early with a clear error.
  if (!storedPostoId) {
    await _clearWorkflowFailed("Visa: consulPost is required but not set in storage", E.VISA_FAILED);
    return;
  }
  const postoId = storedPostoId;

  const _today = new Date();
  const _fmt   = d => `${d.getFullYear()}/${String(d.getMonth()+1).padStart(2,"0")}/${String(d.getDate()).padStart(2,"0")}`;
  const _cfg   = await fetch(chrome.runtime.getURL("config.json")).then(r => r.json()).catch(() => ({}));

  // ── Passport issue date ───────────────────────────────────────────────────
  // Priority: person.passportIssue (inject) → config.json → fallback 1–3 years ago
  let passportIssueDate = person.passportIssue ?? _cfg.passportIssue;
  if (!passportIssueDate) {
    const d = new Date(_today);
    d.setFullYear(d.getFullYear() - (1 + Math.floor(Math.random() * 3))); // 1–3 years ago
    d.setMonth(Math.floor(Math.random() * 12));
    d.setDate(1 + Math.floor(Math.random() * 28));
    passportIssueDate = _fmt(d);
    console.log(`[OctoProbe] passportIssue not provided — generated: ${passportIssueDate}`);
  }

  // ── Passport expiry date ──────────────────────────────────────────────────
  // Priority: person.passportExpiry (inject) → config.json → fallback 5 years after issue
  let passportExpiryDate = person.passportExpiry ?? _cfg.passportExpiry;
  if (!passportExpiryDate) {
    const d = new Date(passportIssueDate.replace(/\//g, "-"));
    d.setFullYear(d.getFullYear() + 5); // 5 years after issue
    passportExpiryDate = _fmt(d);
    console.log(`[OctoProbe] passportExpiry not provided — generated: ${passportExpiryDate}`);
  }

  // ── Arrival date ──────────────────────────────────────────────────────────
  // Priority: visa-arrival-date (inject) → fallback 1–3 months (30–90 days) from today
  let computedArrivalDate = arrivalDate;
  if (!computedArrivalDate) {
    const d = new Date(_today);
    d.setDate(d.getDate() + 30 + Math.floor(Math.random() * 61)); // 30–90 days
    computedArrivalDate = _fmt(d);
    console.log(`[OctoProbe] arrivalDate not provided — generated: ${computedArrivalDate}`);
  }

  // ── Departure date ────────────────────────────────────────────────────────
  // Priority: visa-departure-date (inject) → fallback 7–89 days after arrival
  let computedDepartureDate = departureDate;
  if (!computedDepartureDate) {
    const d = new Date(computedArrivalDate.replace(/\//g, "-"));
    d.setDate(d.getDate() + 7 + Math.floor(Math.random() * 83)); // 7–89 days after arrival
    computedDepartureDate = _fmt(d);
    console.log(`[OctoProbe] departureDate not provided — generated: ${computedDepartureDate}`);
  }

  // Persist both resolved dates to storage so visaStepSchedule can use the correct
  // arrival bound when filtering available appointment slots (even when auto-generated).
  await storageSet({
    "visa-arrival-date":   computedArrivalDate,
    "visa-departure-date": computedDepartureDate,
  });

  // ── Consular post (f0sf1) — inject; triggers AJAX for appointment slots ──
  setBadge("Visa: setting consular post…", "#9060cc");
  await sleep(400 + Math.random() * 300);
  await _setField("f0sf1", postoId);
  // Synthetic change events from isolated world have isTrusted=false — call
  // ajaxFunction directly in MAIN world to guarantee the AJAX request fires.
  await _callPageFn(`if(typeof ajaxFunction==="function") ajaxFunction("${postoId}");`);
  await sleep(1200 + Math.random() * 600);   // wait for AJAX to settle

  // ── TAB 1: Personal data ──────────────────────────────────────────────────
  setBadge("Visa: Tab 1 — personal…", "#9060cc");
  await sleep(600 + Math.random() * 400);

  const tab1Fields = [
    // prefill (readonly, skip): f1 surname, f3 first name, f4 dob, f9 gender
    ["f2",    "+"],      // surname at birth — default:+
    ["f6",    "+"],      // place of birth — default:+  (was f6sf2, corrected)
    ["f6sf1", "CPV"],   // country of birth — default:CPV
    ["f7sf1", "CPV"],   // current nationality — default:CPV
    ["f8",    "CPV"],   // original nationality — default:CPV
    ["f10",   "1"],     // marital status — default:single (1)
  ];
  for (const [name, val] of tab1Fields) {
    if (val) await _setField(name, val);
    await sleep(400 + Math.random() * 400);
  }

  // ── TAB 2: Travel document ────────────────────────────────────────────────
  await _switchToTab(1, 2);
  setBadge("Visa: Tab 2 — travel doc…", "#9060cc");

  const tab2Fields = [
    // prefill (readonly, skip): f13 passport type, f14 passport number
    // Visual reading order: f16 → f17 → f15 (matches form layout)
    ["f16", passportIssueDate],   // date of issue — inject (person > config > auto)
    ["f17", passportExpiryDate],  // valid until — inject (person > config > auto)
    ["f15", "CPV"],               // issued by — default:CPV
    // f5 (ID number) — optional, no mark, skip
  ];
  for (const [name, val] of tab2Fields) {
    if (val) await _setField(name, val);
    await sleep(400 + Math.random() * 400);
  }

  // ── TAB 3: Travel info ────────────────────────────────────────────────────
  await _switchToTab(2, 3);
  setBadge("Visa: Tab 3 — travel info…", "#9060cc");
  // prefill (readonly, skip): f29 purpose of travel
  // txtInfoMotEstada — optional, no mark, skip
  // f25 duration — auto-calc hidden input, set by calcularDuracaoEValidar below

  // Destination — default:PRT; call gerirDestino to keep destination slots consistent.
  await _setField("em_destino_1", "PRT");
  await _callPageFn(`if(typeof gerirDestino==="function") gerirDestino('PT','1');`);
  await sleep(400 + Math.random() * 400);

  // Transit route and number of entries — defaults.
  await _setField("f32", "PRT");
  await sleep(400 + Math.random() * 400);
  await _setField("f24", "1");
  await sleep(500 + Math.random() * 400);

  // Arrival date — inject; MAIN world handlers unlock departure and may validate.
  setBadge("Visa: Tab 3 — arrival date…", "#9060cc");
  await _setField("f30", computedArrivalDate);
  await _callPageFn(`(function(){
    var el = document.querySelector('[name="f30"]');
    if(typeof validarChegadaPartida==="function") validarChegadaPartida(el, 'PT');
    if(typeof cleanPartida==="function") cleanPartida();
    if(typeof gerirEstadoPartida==="function") gerirEstadoPartida();
  })();`);
  await sleep(500 + Math.random() * 400);

  // Departure date — inject; calcularDuracaoEValidar updates f25 (duration hidden input).
  setBadge("Visa: Tab 3 — departure date…", "#9060cc");
  await _setField("f31", computedDepartureDate);
  await _callPageFn(`(function(){
    var el = document.querySelector('[name="f31"]');
    if(typeof calcularDuracaoEValidar==="function") calcularDuracaoEValidar(el);
  })();`);
  await sleep(400 + Math.random() * 300);

  // ── TAB 4: Occupation / residence ────────────────────────────────────────
  await _switchToTab(3, 4);
  setBadge("Visa: Tab 4 — occupation…", "#9060cc");

  const tab4Fields = [
    ["f19",    "05"], // occupation — default:05
    ["f20sf1",  "+"], // employer name — default:+
    ["f20sf2",  "+"], // employer address/phone — default:+
    ["f45",     "+"], // address — default:+
    ["f46",     "0"], // phone — default:0
    // f27 (entry permit) — no mark; form first option defaults to N, skip
    // f18sf1/f18sf2/f18sf3 — conditional, no mark, skip
  ];
  for (const [name, val] of tab4Fields) {
    if (val) await _setField(name, val);
    await sleep(400 + Math.random() * 400);
  }
  // Fingerprints — default:N; call gerirImpressoes to keep dependent fields hidden.
  await _setField("cmbImpressoesDigitais", "N");
  await _callPageFn(`if(typeof gerirImpressoes==="function") gerirImpressoes(document.querySelector('[name="cmbImpressoesDigitais"]'));`);
  await sleep(400 + Math.random() * 300);

  // ── TAB 5: Reference + costs ──────────────────────────────────────────────
  await _switchToTab(4, 5);
  setBadge("Visa: Tab 5 — reference…", "#9060cc");

  // Reference type — must call gerirReferencia in MAIN world to reveal the host
  // fields section. Without this, f34/f34sf2 are inside a display:none container
  // and humanMoveTo would target coords (0,0) — a strong bot signal.
  await _setField("cmbReferencia", "individual");
  await _callPageFn(`if(typeof gerirReferencia==="function") gerirReferencia(document.querySelector('[name="cmbReferencia"]'));`);
  // Wait until host name field becomes visible before proceeding.
  await waitFor(() => { const el = document.querySelector('[name="f34"]'); return el && isVisible(el) ? el : null; }, 4000, 200);
  await sleep(400 + Math.random() * 300);

  // Host details — defaults
  await _setField("f34",    "+");   // host name
  await sleep(400 + Math.random() * 400);
  await _setField("f34sf2", "+");   // host address
  await sleep(400 + Math.random() * 400);
  await _setField("f34sf5", "1");   // host county
  await sleep(400 + Math.random() * 400);

  // Expenses — default:1
  await _setField("cmbDespesasRequerente_1", "1");
  await _callPageFn(`if(typeof gerirDespesasRequerente==="function") gerirDespesasRequerente(1);`);
  await sleep(400 + Math.random() * 300);

  // ── TAB 6: Attachments — SKIP ─────────────────────────────────────────────
  // Tab 6 is skipped entirely per specification.

  // ── Submit the form ───────────────────────────────────────────────────────
  if (submitAfter) await _submitVisaForm();
}

// ---------------------------------------------------------------------------
// Visa workflow — step: schedule appointment
// ---------------------------------------------------------------------------

// Parse HTML response from SubmeterVistoCriaPDF to find the PDF resource URL.
// The server returns an HTML page embedding the PDF via iframe/object/anchor.
function _extractPdfUrl(html) {
  const patterns = [
    /iframe[^>]+src=["']([^"']*MostrarPdf[^"']*)["']/i,
    /object[^>]+data=["']([^"']*MostrarPdf[^"']*)["']/i,
    /<embed[^>]+src=["']([^"']*MostrarPdf[^"']*)["']/i,
    /href=["']([^"']*MostrarPdf[^"']*)["']/i,
    /iframe[^>]+src=["']([^"']*\.pdf[^"']*)["']/i,
    /href=["']([^"']*\.pdf[^"']*)["']/i,
  ];
  for (const pat of patterns) {
    const m = html.match(pat);
    if (m?.[1]) return m[1];
  }
  return null;
}

// Download a blob URL using chrome.downloads when available, anchor click otherwise.
async function _downloadBlobUrl(blobUrl, filename) {
  try {
    await chrome.downloads.download({ url: blobUrl, filename, saveAs: false });
    console.log("[OctoProbe] PDF download triggered via chrome.downloads:", filename);
  } catch (_) {
    const a = document.createElement("a");
    a.href = blobUrl; a.download = filename;
    document.body.appendChild(a); a.click();
    setTimeout(() => { try { URL.revokeObjectURL(blobUrl); } catch(__) {} a.remove(); }, 6000);
    console.log("[OctoProbe] PDF download triggered via anchor:", filename);
  }
}

async function visaStepSchedule() {
  setBadge("Visa: schedule — waiting for page…", "#9060cc");

  const {"visa-consular-post": postoId, "direct-slots-fetch": directFetch = false} =
    await storageGet(["visa-consular-post", "direct-slots-fetch"]);
  const POSTO = postoId ?? "5088";

  // ── 1. Wait for captchaDiv ────────────────────────────────────────────────
  const captchaDiv = await waitFor(
    () => { const el = document.getElementById("captchaDiv"); return el && isVisible(el) ? el : null; },
    20000
  );
  if (!captchaDiv) { await _clearWorkflowFailed("Visa: schedule captcha not found", E.VISA_FAILED); return; }

  // Human dwell: read the page before touching captcha.
  await sleep(1200 + Math.random() * 1300);

  // ── 2. Solve reCAPTCHA ────────────────────────────────────────────────────
  setBadge("Visa: solving schedule reCAPTCHA…", "#9060cc");
  await injectAlertCapture();
  await _ensureEvtKeys();
  console.log("[OctoProbe] Schedule captcha: starting solve");
  const schedSolveStart = Date.now();
  const schedSolved = await waitForRecaptcha();
  if (!schedSolved) { await _clearWorkflowFailed("Visa: schedule reCAPTCHA failed", E.VISA_FAILED); return; }
  console.log("[OctoProbe] Schedule captcha: solved in " + (Date.now() - schedSolveStart) + "ms");

  // ── 3. Optional direct slots fetch (off by default — potentially detectable) ──
  // When enabled, the extension fetches available slots via the same XHR the page
  // uses, without waiting for the native handleServerResponse. Useful for probing
  // slot availability early or with test accounts.
  let prefetchedSlotsRaw = null;
  if (directFetch) {
    const tok = await _callPageFn(
      `var ta=document.querySelector("#g-recaptcha-response-1,#g-recaptcha-response"); return ta?ta.value:null;`
    ).then(r => r?.result ?? null).catch(() => null);
    if (tok && tok.length > 20) {
      try {
        const r = await fetch(`/VistosOnline/slots?posto_id=${POSTO}`, {
          method: "POST",
          headers: {"Content-Type": "application/x-www-form-urlencoded"},
          body: `posto_id=${encodeURIComponent(POSTO)}&captcha=${encodeURIComponent(tok)}`,
          credentials: "include",
        });
        if (r.ok) {
          prefetchedSlotsRaw = await r.text();
          console.log("[OctoProbe] Direct slots fetch: received", prefetchedSlotsRaw.length, "chars");
        }
      } catch(e) { console.warn("[OctoProbe] Direct slots fetch failed:", e); }
    }
  }

  // ── 5. Wait for calendarDiv ───────────────────────────────────────────────
  setBadge("Visa: waiting for calendar…", "#9060cc");
  const calendarVisible = await waitFor(() => {
    const el = document.getElementById("calendarDiv");
    return el && el.style.display !== "none" ? el : null;
  }, 30000);
  if (!calendarVisible) {
    await _clearWorkflowFailed("Visa: calendar did not appear — token rejected or no slots", E.VISA_FAILED);
    return;
  }
  console.log("[OctoProbe] Calendar revealed");

  // ── 6. Read available slots from window.data ──────────────────────────────
  // handleServerResponse() parses JSON into page-global `data` variable.
  const slotsRaw = prefetchedSlotsRaw
    ?? await _callPageFn(`return Array.isArray(data) ? JSON.stringify(data) : null;`).then(r => r?.result ?? null).catch(() => null);

  let chosenDateISO = null; // "YYYY-MM-DD" — calendar key format
  let chosenDate    = null; // "YYYY/MM/DD" — field value format
  let _chosenSlotKey = null; // for slot reporting to manager
  if (slotsRaw) {
    try {
      const slots = JSON.parse(slotsRaw);
      const today = new Date(); today.setHours(0,0,0,0);
      const valid = slots
        .filter(s => s.date && s.periods?.length)
        .map(s => s.date) // "YYYY-MM-DD"
        .filter(d => { const dt = new Date(d); dt.setHours(0,0,0,0); return dt > today; })
        .sort();
      console.log("[OctoProbe] Available dates:", valid);

      if (valid.length) {
        // ── Slot intelligence: observe + request assignment ───────────────────
        // Build flat {date, time} list from all visible slots for the pool.
        const visibleSlots = [];
        for (const s of slots) {
          if (!s.date || !s.periods?.length) continue;
          for (const p of s.periods) {
            const time = typeof p === "string" ? p : (p.time ?? p.hour ?? "");
            if (time) visibleSlots.push({ date: s.date, time });
          }
        }

        // Fire-and-forget observation — non-blocking
        chrome.runtime.sendMessage({ type: "slot-observation", postId: Number(POSTO), slots: visibleSlots }).catch(() => {});

        // Soft-blocking assignment request (3s timeout — falls back to local logic)
        const assignment = await chrome.runtime.sendMessage({
          type: "slot-assignment-request",
          postId: Number(POSTO),
          visibleSlots,
        }).catch(() => null);

        if (assignment?.ok && assignment.primary?.date) {
          chosenDateISO  = assignment.primary.date;
          chosenDate     = assignment.primary.date.replace(/-/g, "/");
          _chosenSlotKey = assignment.primary.slotKey;
          console.log("[OctoProbe] Slot assignment: primary=", _chosenSlotKey,
            "fallbacks=", assignment.fallbacks?.length ?? 0);
          // Store fallbacks on window for use in submit retry (if needed)
          window._octoSlotFallbacks = assignment.fallbacks ?? [];
        } else {
          // No manager assignment — pick earliest available locally
          chosenDateISO = valid[0];
          chosenDate    = valid[0].replace(/-/g, "/");
          if (visibleSlots.length) {
            const fb = visibleSlots.find(s => s.date === chosenDateISO);
            _chosenSlotKey = fb ? `${POSTO}-${fb.date}-${fb.time}` : null;
          }
        }
      }
    } catch(_) {}
  }
  if (!chosenDate) { await _clearWorkflowFailed("Visa: no available slots found", E.VISA_FAILED); return; }

  setBadge(`Visa: selecting date ${chosenDate}…`, "#9060cc");
  await sleep(800 + Math.random() * 600);

  // ── 7. Human-like date selection via the calendar widget ──────────────────
  // Click the f_trigger_c icon to open the Calendar.js popup, navigate to the
  // correct month, then click the target day cell. The page's dateChanged()
  // callback fires natively — setting f_date_c and calling ajaxFunctionPeriodos().

  const trigger = document.getElementById("f_trigger_c");
  if (!trigger) { await _clearWorkflowFailed("Visa: calendar trigger not found", E.VISA_FAILED); return; }
  trigger.scrollIntoView({behavior: "smooth", block: "center"});
  await sleep(400 + Math.random() * 300);
  await humanClick(trigger);

  // Wait for Calendar.js popup div to appear (div.calendar with non-hidden visibility)
  const calPopup = await waitFor(() => {
    for (const d of document.querySelectorAll("div.calendar")) {
      const s = window.getComputedStyle(d);
      if (s.visibility !== "hidden" && s.display !== "none" && s.opacity !== "0") return d;
    }
    return null;
  }, 5000);

  if (calPopup) {
    // Navigate to the correct month/year
    const [targetYear, targetMonth, targetDay] = chosenDateISO.split("-").map(Number);
    // Map month labels to 1-based index — supports both English (lang-switch enabled)
    // and Portuguese (lang-switch disabled, the default).
    const MONTH_MAP = {
      january:1,  february:2,  march:3,      april:4,    may:5,      june:6,
      july:7,     august:8,    september:9,  october:10, november:11,december:12,
      janeiro:1,  fevereiro:2, março:3,       abril:4,    maio:5,     junho:6,
      julho:7,    agosto:8,    setembro:9,   outubro:10, novembro:11,dezembro:12,
      // normalised (strip diacritics) fallbacks
      marco:3,
    };

    for (let nav = 0; nav < 24; nav++) {
      const monthLabel = calPopup.querySelector(".label.month, td.label.month, .calHead .month");
      const yearLabel  = calPopup.querySelector(".label.year,  td.label.year,  .calHead .year");
      if (!monthLabel || !yearLabel) break;

      const rawMonth = monthLabel.textContent.trim().toLowerCase();
      const monthKey = rawMonth.normalize("NFD").replace(/[̀-ͯ]/g, ""); // strip diacritics (e.g. março → marco)
      const curMonth = MONTH_MAP[rawMonth] ?? MONTH_MAP[monthKey] ?? 0;
      const curYear  = parseInt(yearLabel.textContent.trim(), 10);
      if (isNaN(curYear) || curMonth === 0) break;
      if (curYear === targetYear && curMonth === targetMonth) break;

      // Click prev (<) or next (>) navigation button
      const navDivs = [...calPopup.querySelectorAll("td.button > div.nav, td.button > .nav, .button.nav")];
      const isAhead = (targetYear * 12 + targetMonth) > (curYear * 12 + curMonth);
      const navBtn  = isAhead ? navDivs[navDivs.length - 1] : navDivs[0];
      if (!navBtn) break;
      await humanClick(navBtn);
      await sleep(250 + Math.random() * 150);
    }

    // Click the day cell matching targetDay (skip disabled/out-of-range cells)
    await sleep(300 + Math.random() * 200);
    const dayCells = [...calPopup.querySelectorAll("td.day")];
    const targetCell = dayCells.find(td => {
      if (td.classList.contains("disabled") || td.classList.contains("out-of-range")) return false;
      return parseInt(td.textContent.trim(), 10) === targetDay;
    });

    if (targetCell) {
      await humanClick(targetCell);
      console.log("[OctoProbe] Calendar: clicked day", targetDay);
    } else {
      console.warn("[OctoProbe] Calendar: day cell not found — using programmatic fallback");
      await _callPageFn(`
        (function(){
          var inp = document.getElementById("f_date_c");
          if (!inp) return;
          inp.removeAttribute("readonly");
          var ns = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,"value")?.set;
          if (ns) ns.call(inp,"${chosenDate}"); else inp.value = "${chosenDate}";
          inp.setAttribute("readonly","1");
          inp.dispatchEvent(new Event("input",{bubbles:true}));
          inp.dispatchEvent(new Event("change",{bubbles:true}));
          if (typeof ajaxFunctionPeriodos === "function") ajaxFunctionPeriodos("${chosenDate}");
        })();
      `);
    }
  } else {
    // Calendar popup never appeared — set date programmatically
    console.warn("[OctoProbe] Calendar popup not found — using programmatic date set");
    await _callPageFn(`
      (function(){
        var inp = document.getElementById("f_date_c");
        if (!inp) return;
        inp.removeAttribute("readonly");
        var ns = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,"value")?.set;
        if (ns) ns.call(inp,"${chosenDate}"); else inp.value = "${chosenDate}";
        inp.setAttribute("readonly","1");
        inp.dispatchEvent(new Event("input",{bubbles:true}));
        inp.dispatchEvent(new Event("change",{bubbles:true}));
        if (typeof ajaxFunctionPeriodos === "function") ajaxFunctionPeriodos("${chosenDate}");
      })();
    `);
  }

  // Confirm f_date_c is set (dateChanged sets it; programmatic fallback sets it directly)
  const dateSet = await waitFor(() => {
    const el = document.getElementById("f_date_c");
    return el && el.value === chosenDate ? el : null;
  }, 8000);
  if (!dateSet) {
    console.warn("[OctoProbe] f_date_c not confirmed — forcing value");
    await _callPageFn(`
      (function(){
        var inp = document.getElementById("f_date_c");
        if (!inp) return;
        inp.removeAttribute("readonly");
        var ns = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,"value")?.set;
        if (ns) ns.call(inp,"${chosenDate}"); else inp.value = "${chosenDate}";
        inp.setAttribute("readonly","1");
        if (typeof ajaxFunctionPeriodos === "function") ajaxFunctionPeriodos("${chosenDate}");
      })();
    `);
  }

  // ── 8. Wait for period dropdown and select first option ───────────────────
  setBadge("Visa: waiting for time slots…", "#9060cc");
  const periodSel = await waitFor(() => {
    const el = document.getElementById("inputPeriodos");
    return el && !el.disabled && el.options.length > 1 ? el : null;
  }, 15000);
  if (!periodSel) { await _clearWorkflowFailed("Visa: no period options loaded", E.VISA_FAILED); return; }

  const firstPeriod = [...periodSel.options].find(o => o.value && o.value.trim() !== "");
  if (!firstPeriod) { await _clearWorkflowFailed("Visa: no valid period option", E.VISA_FAILED); return; }

  await humanSelect(periodSel, firstPeriod.value);
  await sleep(600 + Math.random() * 500);
  console.log("[OctoProbe] Period selected:", firstPeriod.text, "value=" + firstPeriod.value);

  // ── 9. Override submitSlotsForm before clicking submit ────────────────────
  // The page's submitSlotsForm() calls document.write(resultData) which destroys
  // the page context and kills the content script. Our override uses fetch() to
  // POST the same data and signals the result via postMessage.
  // FormData captures txthuman (filled by reCAPTCHA Enterprise) and back="" naturally.
  // Use session-key-derived postMessage type so "octo-*" never appears in MAIN world
  const _schedMsgType = 's' + _sessionSk;
  await _callPageFn(`
    (function(msgType, postoId){
      window.submitSlotsForm = async function() {
        var form = document.getElementById("vistoForm");
        if (!form) { window.postMessage({type:msgType,error:"no form"},"*"); return; }
        var fd   = new FormData(form);
        var body = new URLSearchParams({
          lang:       fd.get("lang")       ?? "ENG",
          txtHuman:   fd.get("txtHuman")   ?? "",
          back:       fd.get("back")       ?? "",
          f_date_c:   fd.get("f_date_c")   ?? "",
          cmbPeriodo: fd.get("cmbPeriodo") ?? "",
        }).toString();
        try {
          var resp = await fetch("/VistosOnline/SubmeterVistoCriaPDF?posto_id=" + postoId, {
            method:"POST", body:body,
            headers:{"Content-Type":"application/x-www-form-urlencoded","X-Requested-With":"XMLHttpRequest"},
            credentials:"include"
          });
          var ct = (resp.headers.get("content-type") ?? "").toLowerCase();
          if (ct.includes("pdf") || ct.includes("octet-stream")) {
            var blob = await resp.blob();
            window.postMessage({type:msgType, blobUrl:URL.createObjectURL(blob), isPdf:true}, "*");
          } else {
            var html = await resp.text();
            window.postMessage({type:msgType, html:html, isPdf:false}, "*");
          }
        } catch(e) {
          window.postMessage({type:msgType, error:String(e)}, "*");
        }
      };
    })(${JSON.stringify(_schedMsgType)}, ${JSON.stringify(POSTO)});
  `);
  await sleep(800 + Math.random() * 600);

  // ── 10. Click submit → submeter() validates → openPopUpPreVisto() ─────────
  setBadge("Visa: submitting schedule…", "#9060cc");
  const btnSubmit = document.getElementById("btnSubmit");
  if (!btnSubmit) { await _clearWorkflowFailed("Visa: submit button not found", E.VISA_FAILED); return; }
  await humanClick(btnSubmit);

  // ── 11. Confirm pre-visto popup ───────────────────────────────────────────
  const previstoPopup = await waitFor(() => {
    const el = document.getElementById("previstoMsg");
    return el && isVisible(el) ? el : null;
  }, 15000);
  if (!previstoPopup) { await _clearWorkflowFailed("Visa: confirmation popup did not appear", E.VISA_FAILED); return; }

  setBadge("Visa: confirming appointment…", "#9060cc");
  await sleep(800 + Math.random() * 800);
  const previstoSubmit = document.getElementById("previstoSubmit");
  if (!previstoSubmit) { await _clearWorkflowFailed("Visa: previstoSubmit not found", E.VISA_FAILED); return; }
  await humanClick(previstoSubmit); // → closePopUPrevisto() + submitSlotsForm() (our override)

  // ── 12. Wait for booking confirmation from overridden submitSlotsForm ──────
  setBadge("Visa: waiting for booking confirmation…", "#9060cc");
  const result = await _waitPageMessage(_schedMsgType, 45000);
  if (!result || result.error) {
    // Report failure to slot pool before exiting
    if (_chosenSlotKey) {
      chrome.runtime.sendMessage({ type: "slot-failure", slotKey: _chosenSlotKey, reason: result?.error ?? "timeout" }).catch(() => {});
    }
    await _clearWorkflowFailed(`Visa: booking failed — ${result?.error ?? "timeout"}`, E.VISA_FAILED);
    return;
  }

  // ── 13. Download PDF ──────────────────────────────────────────────────────
  setBadge("Visa: downloading PDF…", "#9060cc");
  const pdfFilename = `visa_appt_${chosenDate.replace(/\//g,"-")}_${Date.now()}.pdf`;

  if (result.isPdf && result.blobUrl) {
    // Server returned the PDF binary directly
    await _downloadBlobUrl(result.blobUrl, pdfFilename);
    setBadge("Visa: appointment confirmed!", "#00d4aa");
  } else if (result.html) {
    // Server returned HTML — extract MostrarPdf URL and fetch the actual PDF
    const pdfPath = _extractPdfUrl(result.html);
    if (pdfPath) {
      const fullUrl = pdfPath.startsWith("http") ? pdfPath
        : pdfPath.startsWith("/") ? pdfPath
        : "/VistosOnline/" + pdfPath;
      try {
        const pdfResp = await fetch(fullUrl, {credentials: "include"});
        if (pdfResp.ok) {
          const blob = await pdfResp.blob();
          await _downloadBlobUrl(URL.createObjectURL(blob), pdfFilename);
          setBadge("Visa: appointment confirmed!", "#00d4aa");
        } else {
          console.warn("[OctoProbe] PDF fetch returned", pdfResp.status);
          setBadge("Visa: confirmed (PDF fetch failed)", "#f0c040");
        }
      } catch(e) {
        console.error("[OctoProbe] PDF download error:", e);
        setBadge("Visa: confirmed (PDF error)", "#f0c040");
      }
    } else {
      // No PDF link — likely a text/HTML confirmation page; still a success
      console.log("[OctoProbe] No PDF URL in response HTML — confirmation page received");
      setBadge("Visa: appointment confirmed!", "#00d4aa");
    }
  }

  // Report slot success to pool
  if (_chosenSlotKey) {
    chrome.runtime.sendMessage({ type: "slot-success", slotKey: _chosenSlotKey }).catch(() => {});
  }

  console.log("[OctoProbe] Visa: schedule complete  date=" + chosenDate + "  period=" + firstPeriod.text);
}

// ---------------------------------------------------------------------------
// cmd-* handler helpers
// ---------------------------------------------------------------------------

async function _loginFillCredentials(creds) {
  setBadge("Login: waiting for form…", "#f0c040");
  const userEl = await waitFor(() => findBySelectors(USERNAME_SELECTORS), 10000);
  if (!userEl) return {ok: false, status: "form_not_found"};
  setBadge("Login: filling credentials…", "#f0c040");
  await humanType(userEl, creds.username);
  await sleep(500 + Math.random() * 500);
  const passEl = await waitFor(() => findBySelectors(PASSWORD_SELECTORS), 4000);
  if (!passEl) return {ok: false, status: "pass_not_found"};
  await humanType(passEl, creds.password);
  await sleep(500 + Math.random() * 500);
  return {ok: true};
}

async function _loginSubmit() {
  const rcEl = await waitFor(
    () => document.querySelector("[data-sitekey], iframe[src*='recaptcha'], .g-recaptcha"),
    8000
  );
  if (rcEl) {
    rcEl.scrollIntoView({behavior: "smooth", block: "center"});
    await sleep(800 + Math.random() * 600);
    setBadge("Login: solving reCAPTCHA…", "#f0c040");
    const solved = await waitForRecaptcha();
    if (!solved) return {ok: false, status: "captcha_fail"};
    const r = rcEl.getBoundingClientRect();
    window._mX = Math.round(r.left + r.width * 0.5);
    window._mY = Math.round(r.top + r.height * 0.5);
  } else {
    await sleep(800 + Math.random() * 600);
  }
  await injectAlertCapture();
  const submitBtn = await waitFor(() => findBySelectors(LOGIN_SUBMIT_SELECTORS), 4000);
  if (!submitBtn) return {ok: false, status: "submit_not_found"};
  setBadge("Login: submitting…", "#f0c040");
  await humanClick(submitBtn);
  // RGPD consent popup
  const rgpdEl = await waitFor(() => {
    const el = document.querySelector("#loginMsg");
    return isVisible(el) ? el : null;
  }, 25000);
  if (rgpdEl) {
    setBadge("Login: accepting RGPD…", "#f0c040");
    const cb1 = document.querySelector("#loginCheckbox1");
    const cb2 = document.querySelector("#loginCheckbox2");
    const cb3 = document.querySelector("#loginCheckbox3");
    if (cb3?.checked) await humanClick(cb3);
    await sleep(200 + Math.random() * 200);
    if (cb1 && !cb1.checked) await humanClick(cb1);
    await sleep(300 + Math.random() * 300);
    if (cb2 && !cb2.checked) await humanClick(cb2);
    await sleep(400 + Math.random() * 300);
    const rgpdSubmit = await waitFor(() => {
      const btn = document.querySelector("#loginSubmit");
      return btn && !btn.disabled ? btn : null;
    }, 5000);
    if (rgpdSubmit) await humanClick(rgpdSubmit);
    await sleep(1000 + Math.random() * 1000);
  }
  setBadge("Login: waiting for redirect…", "#f0c040");
  await sleep(8000);
  if (!/Authentication|authentication/i.test(location.pathname + location.search)) {
    return {ok: true, status: "navigated"};
  }
  // Still on Authentication after 5 s — the login POST was rejected or never redirected.
  // Do not navigate manually; the session is not established.
  await sendBgMessage({type: "reset-recaptcha"});
  setBadge("Login: rejected — will retry…", "#f0a030");
  return {ok: false, status: "rejected"};
}

async function _registerOpenForm() {
  const net = await checkNetworkQuality();
  if (!net.good) return {ok: false, status: "network_poor", avg: net.avg};
  setBadge("Register: finding link…", "#f0c040");
  const regEl = await waitFor(
    () => findBySelectors(REGISTER_LINK_SELECTORS) || findByText(["span","a","button","div"], REGISTER_LINK_TEXTS),
    8000
  );
  if (!regEl) return {ok: false, status: "link_not_found"};
  await sleep(900 + Math.random() * 1000);
  await humanClick(regEl);
  await sleep(300 + Math.random() * 300);
  setBadge("Register: waiting for form…", "#f0c040");
  const form = await waitFor(() => document.querySelector("#formReg"), 12000);
  if (!form) return {ok: false, status: "form_blocked"};
  await sleep(2500 + Math.random() * 2000);
  return {ok: true, status: "form_ready"};
}

async function _registerSubmit() {
  await _ensureEvtKeys();

  // Quick sanity check — if fill failed silently, don't waste a captcha solve credit.
  const missing = _checkFormFields();
  if (missing.length) return {ok: false, status: "form_incomplete", fields: missing};

  setBadge("Register: reviewing form…", "#f0c040");

  const rcEl = document.querySelector("iframe[src*='recaptcha'], .g-recaptcha, #recaptcha, [class*='recaptcha']");
  if (rcEl) {
    rcEl.scrollIntoView({behavior: "smooth", block: "center"});
    await sleep(800 + Math.random() * 600);
    const r = rcEl.getBoundingClientRect();
    window._mX = Math.round(r.left + r.width * 0.5);
    window._mY = Math.round(r.top + r.height * 0.5);
  }

  // Pre-captcha behavioral dwell — reCAPTCHA Enterprise scores the entire page session.
  // A real user spends minutes reviewing the filled form before submitting.
  setBadge("Register: reviewing form…", "#f0c040");
  _logEntry("register: pre-captcha dwell starting");
  const _dwellEnd = Date.now() + 25000 + Math.random() * 15000;
  const _reviewFields = Array.from(document.querySelectorAll(
    "#formReg input:not([type=hidden]), #formReg select"
  )).filter(el => isVisible(el));
  while (Date.now() < _dwellEnd) {
    if (_reviewFields.length) {
      const f = _reviewFields[Math.floor(Math.random() * _reviewFields.length)];
      await humanMoveTo(f);
    }
    if (Math.random() < 0.3) {
      const scrollTarget = Math.random() * (document.body.scrollHeight * 0.6);
      window.scrollTo({top: scrollTarget, behavior: "smooth"});
    }
    await sleep(2500 + Math.random() * 3000);
  }
  _logEntry("register: pre-captcha dwell done");

  const formSubmitBtn = document.querySelector(
    "#formReg button[type='submit'], #formReg input[type='submit'], #formReg button[type='button']"
  );
  if (!formSubmitBtn) return {ok: false, status: "submit_not_found"};

  await injectAlertCapture();

  // Persistent alert listener — outside the loop to prevent a late-firing attempt-1
  // alert from consuming attempt-2's {once} listener and silencing the real attempt-2 alert.
  let _registerAlert = null;
  const _alertHandler = (e) => {
    _registerAlert = e.detail.msg;
    _logEntry(`register: server alert → "${e.detail.msg}"`);
    console.log(`[OctoProbe] Register alert: "${e.detail.msg}"`);
  };
  document.addEventListener(_evtAlert, _alertHandler);

  try {
  // 2-attempt loop — server issues a softer scoring threshold on the second attempt.
  // Both attempts: close any open RGPD popup → fresh captcha solve → click formSubmitBtn.
  for (let rgpdAttempt = 1; rgpdAttempt <= 2; rgpdAttempt++) {

    if (rgpdAttempt > 1) {
      // Dismiss the still-open RGPD popup BEFORE re-solving — the modal covers the form
      // and the captcha widget.  A real user closes the error dialog, reviews the form,
      // then tries again.
      const popupClose = document.querySelector(
        "#registroMsg .close, #registroMsg [data-dismiss='modal'], #registroMsg button[aria-label='Close']"
      );
      if (popupClose && isVisible(popupClose)) {
        await humanClick(popupClose);
      } else {
        document.dispatchEvent(new KeyboardEvent("keydown", {key: "Escape", keyCode: 27, bubbles: true, cancelable: true}));
      }
      await waitFor(() => {
        const el = document.querySelector("#registroMsg");
        return (!el || !isVisible(el)) ? true : null;
      }, 5000);
      // Scroll the recaptcha area back into view (form is visible again after dismiss).
      if (rcEl) rcEl.scrollIntoView({behavior: "smooth", block: "center"});
      await sleep(1000 + Math.random() * 800);
    }

    setBadge("Register: solving reCAPTCHA…", "#f0c040");
    _logEntry(`register: starting captcha solve (attempt ${rgpdAttempt})`);
    const solved = await waitForRecaptcha();
    if (!solved) {
      _logEntry(`register: captcha solve failed (attempt ${rgpdAttempt})`);
      return {ok: false, status: "captcha_fail"};
    }
    _logEntry(`register: captcha solved — submitting (attempt ${rgpdAttempt})`);

    setBadge("Register: opening privacy popup…", "#f0c040");
    await humanClick(formSubmitBtn);

    const rgpdPopup = await waitFor(() => {
      const el = document.querySelector("#registroMsg");
      return el && isVisible(el) ? el : null;
    }, 10000);

    if (!rgpdPopup) {
      setBadge("Register: waiting for response…", "#f0c040");
      _logEntry("register: RGPD popup absent — form accepted");
      return {ok: true, status: "submitted"};
    }

    setBadge("Register: accepting privacy…", "#f0c040");
    await sleep(600 + Math.random() * 500);
    const rgpdSubmit = document.querySelector("#registroSubmit");
    if (!rgpdSubmit) return {ok: false, status: "rgpd_not_found"};

    setBadge("Register: submitting…", "#f0c040");
    _registerAlert = null; // clear stale alert from previous attempt before submit
    await humanClick(rgpdSubmit);

    const outcome = await new Promise(resolve => {
      const checkBtn = setInterval(() => {
        const btn = document.querySelector("#registroSubmit");
        if (btn && !btn.disabled) { clearInterval(checkBtn); clearTimeout(navTimer); resolve("failed"); }
      }, 300);
      const navTimer = setTimeout(() => { clearInterval(checkBtn); resolve("timeout"); }, 35000);
      window.addEventListener("beforeunload", () => {
        clearInterval(checkBtn); clearTimeout(navTimer); resolve("navigated");
      }, {once: true});
    });

    _logEntry(`register: RGPD outcome=${outcome} attempt=${rgpdAttempt} alert="${_registerAlert ?? "none"}"`);

    if (outcome === "navigated") return {ok: true, status: "navigated"};

    const _al = (_registerAlert ?? "").toLowerCase();
    // "1 more attempts" = first rejection, quota still available → let attempt 2 run.
    // True block = "blocked"/"bloqueado" + "incremental"/"incremento" WITHOUT a remaining-attempt hint.
    const _isBlocked  = _al.includes("blocked") || _al.includes("bloqueado");
    const _isIncrem   = _al.includes("incremental") || _al.includes("incremento");
    const _hasRemain  = _al.includes("1 more attempt") || /mais\s+[1-9]\d*\s+tentativ/i.test(_al);
    if (_isBlocked && _isIncrem && !_hasRemain) {
      console.warn("[OctoProbe] Register: IP block detected");
      return {ok: false, status: "ip_blocked"};
    }
    // "Email indisponível" = email address already registered.
    if (_al.includes("indispon") && _al.includes("email")) {
      _logEntry(`register: email already taken — aborting (alert="${_registerAlert}")`);
      return {ok: false, status: "email_taken"};
    }
    // "Invalid username" / "utilizador inválido" = account already exists.
    const _isInvalid  = _al.includes("invalid") || _al.includes("inválid"); // inválid(o/a)
    if (_isInvalid && (_al.includes("username") || _al.includes("utilizador"))) {
      _logEntry(`register: username already taken — aborting (alert="${_registerAlert}")`);
      return {ok: false, status: "username_taken"};
    }

    if (rgpdAttempt < 2) {
      _logEntry("register: first attempt rejected — waiting before retry");
      await sleep(3000 + Math.random() * 2000);
    }
  }
  return {ok: false, status: "captcha_fail"};
  } finally {
    document.removeEventListener(_evtAlert, _alertHandler);
  }
}

async function _tokenFill(codeToken) {
  if (!codeToken) return {ok: false, status: "no_token"};
  await injectAlertCapture();
  const findTokenInput = () => {
    for (const sel of TOKEN_INPUT_SELECTORS) {
      const el = document.querySelector(sel);
      if (isVisible(el)) return el;
    }
    return null;
  };
  const tokenInput = await waitFor(findTokenInput, 15000);
  if (!tokenInput) return {ok: false, status: "input_not_found"};
  await sleep(1200 + Math.random() * 600);
  const isUUID = v => /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(v ?? "");
  const preFilled = tokenInput.value?.trim();
  if (preFilled && isUUID(preFilled)) {
    console.log("[OctoProbe] Token input pre-filled by server:", preFilled);
    return {ok: true, prefilled: true};
  }
  tokenInput.scrollIntoView({behavior: "smooth", block: "center"});
  await sleep(900 + Math.random() * 800);
  const fillResult = await sendBgMessage({type: "fill-token", token: codeToken}).catch(() => null);
  console.log("[OctoProbe] Type-token result:", fillResult);
  await sleep(300 + Math.random() * 300);
  if (!tokenInput.value) {
    console.warn("[OctoProbe] Scripting API fill failed — isolated-world humanType fallback");
    const target = findTokenInput() ?? tokenInput;
    target.focus();
    const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")?.set;
    if (setter) setter.call(target, ""); else target.value = "";
    target.dispatchEvent(new Event("input", {bubbles: true}));
    await humanType(target, codeToken);
  }
  return {ok: true};
}

async function _tokenSubmit() {
  await _ensureEvtKeys();
  const submitBtn = TOKEN_SUBMIT_SELECTORS
    .map(sel => document.querySelector(sel))
    .find(el => isVisible(el));
  if (!submitBtn) return {ok: false, status: "submit_not_found"};
  setBadge("Verifying token…", "#f0c040");
  await sleep(1100 + Math.random() * 1000);
  await humanClick(submitBtn);
  setBadge("Awaiting verification…", "#f0c040");
  const outcome = await Promise.race([
    new Promise(resolve => window.addEventListener("beforeunload", () => resolve("navigated"), {once: true})),
    new Promise(resolve => document.addEventListener(_evtAlert, (e) => resolve({alert: e.detail.msg}), {once: true})),
    sleep(12000).then(() => "timeout"),
  ]);
  if (outcome === "navigated") return {ok: true, status: "navigated"};
  if (outcome && typeof outcome === "object" && outcome.alert !== undefined) {
    const alertLower = outcome.alert.toLowerCase();
    const isSuccess = alertLower.includes("sucesso") || alertLower.includes("success") || alertLower.includes("created");
    _logEntry(`[OctoProbe] VerificarEmail alert (${isSuccess ? "SUCCESS" : "ERROR"}): ${outcome.alert}`);
    if (!isSuccess) return {ok: false, status: "verification_failed", alert: outcome.alert};
    await sleep(2000);
    location.href = "/VistosOnline/Authentication.jsp";
    return {ok: true, status: "navigated"};
  }
  // Timeout: try direct fetch
  console.warn("[OctoProbe] No response from token form — trying direct VerificarEmail fetch");
  try {
    const urlToken = new URLSearchParams(location.search).get("token")?.toLowerCase() ?? null;
    const {"email-code-token": storedCode} = await storageGet("email-code-token");
    const params = new URLSearchParams({
      language: "ENG",
      token: storedCode ?? urlToken,
      tokenSearchParams: urlToken ?? storedCode,
    });
    const resp = await fetch("/VistosOnline/VerificarEmail", {
      method: "POST",
      headers: {"Content-Type": "application/x-www-form-urlencoded"},
      credentials: "include",
      body: params.toString(),
    });
    const text = await resp.text();
    console.log("[OctoProbe] Direct VerificarEmail:", resp.status, JSON.stringify(text));
    if (text) {
      let data;
      try { data = JSON.parse(text); } catch (_) { data = null; }
      if (data?.type === "error") return {ok: false, status: "token_rejected", reason: data.description};
    }
  } catch (e) {
    console.error("[OctoProbe] Direct VerificarEmail error:", e);
  }
  location.href = "/VistosOnline/Authentication.jsp";
  return {ok: true, status: "navigated"};
}

// ---------------------------------------------------------------------------
// Command dispatch table
// ---------------------------------------------------------------------------

const _CMD_HANDLERS = {
  "cmd-get-state":          async ()    => ({ok: true, state: _detectPageState()}),
  "cmd-accept-cookie":      async ()    => { await dismissCookieConsent(); return {ok: true}; },
  "cmd-log-ip": async () => {
    try {
      const res = await fetch("https://api.ipify.org?format=json");
      const {ip} = await res.json();
      console.log("[OctoProbe] Proxy IP:", ip);
      return {ok: true, ip};
    } catch (e) {
      console.warn("[OctoProbe] IP fetch failed:", e.message);
      return {ok: true, ip: "unknown"};
    }
  },
  "cmd-switch-lang":        async ()    => { await switchToEnglish(); return {ok: true}; },
  "cmd-click-login-link":   async ()    => { await clickLoginLink(); return {ok: true}; },

  "cmd-register-open-form": async ()    => _registerOpenForm(),
  "cmd-register-fill":      async (msg) => {
    setBadge("Register: filling form…", "#f0c040");
    const filled = await fillRegisterForm(msg.person, msg.email);
    if (filled === false) return {ok: false, status: "form_incomplete"};
    return {ok: true};
  },
  "cmd-register-submit":    async ()    => _registerSubmit(),

  "cmd-token-fill":         async (msg) => _tokenFill(msg.token),
  "cmd-token-submit":       async ()    => _tokenSubmit(),

  "cmd-login-fill":         async (msg) => _loginFillCredentials(msg),
  "cmd-login-submit":       async ()    => _loginSubmit(),

  "cmd-go-questionnaire":   async ()    => { await visaStepGoToQuestionnaire(); return {ok: true}; },
  "cmd-fill-questionnaire": async ()    => { await visaStepQuestionnaire(); return {ok: true}; },
  "cmd-fill-form":          async ()    => { await visaStepForm();    return {ok: true}; },
  "cmd-fill-form-tabs":     async ()    => { await visaStepForm({submitAfter: false}); return {ok: true}; },
  "cmd-submit-form":        async ()    => _submitVisaForm(),
  "cmd-schedule":           async ()    => { await visaStepSchedule(); return {ok: true}; },

  "cmd-keep-tick": async () => {
    const maxScroll = Math.max(0, document.body.scrollHeight - window.innerHeight);
    if (maxScroll > 0) {
      const pos = Math.floor(Math.random() * maxScroll);
      window.scrollTo({top: pos, behavior: "smooth"});
    }
    return {ok: true};
  },

  "cmd-start-post-monitor": async () => {
    const { "target-post-id": targetPostId } = await storageGet(["target-post-id"]);
    const target = String(targetPostId ?? "");
    if (!target) return {ok: false, error: "target-post-id not set"};

    // Always attempt to refresh consular post options (triggers AJAX for fresh data)
    await _callPageFn(
      `if(typeof loadPostos==="function") loadPostos();` +
      ` else if(typeof carregarPostos==="function") carregarPostos();`
    ).catch(() => {});

    // If already monitoring, the refresh above is all we needed — done
    if (_postMonitorId) return {ok: true};

    const sel = document.getElementById("f0sf1");
    if (!sel) return {ok: true}; // page not ready yet — F6 tick will retry

    const found = () => {
      if (_postMonitorId) {
        _postMonitorId.mo.disconnect();
        clearInterval(_postMonitorId.poll);
        _postMonitorId = null;
      }
      chrome.runtime.sendMessage({ type: "target-post-available", postId: Number(target) }).catch(() => {});
    };

    // Immediate check
    if ([...sel.options].some(o => String(o.value) === target && o.value !== "")) { found(); return {ok: true}; }

    // MutationObserver (priority 1) — catches AJAX-loaded option additions
    const mo = new MutationObserver(() => {
      if ([...sel.options].some(o => String(o.value) === target && o.value !== "")) found();
    });
    mo.observe(sel, { childList: true });

    // Polling fallback 500–800 ms jitter (priority 3)
    const poll = setInterval(() => {
      if ([...sel.options].some(o => String(o.value) === target && o.value !== "")) found();
    }, 500 + Math.random() * 300);

    _postMonitorId = { mo, poll };
    return {ok: true};
  },

  "cmd-run-proxy-check": async () => {
    const q = await _checkProxyQuality();
    return {ok: true, ...q};
  },
};

// ---------------------------------------------------------------------------
// Message listener
// ---------------------------------------------------------------------------

chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
  if (msg.type === "abort") {
    _abortFlag = true;
    sendResponse({ok: true});
    return false;
  }
  if (msg.type === "run-proxy-check") {
    sendResponse({ok: true});
    _checkProxyQuality().then(q => {
      chrome.runtime.sendMessage({type: "ws-send", data: {
        type: "proxy-check", workflow: null, timestamp: new Date().toISOString(), ...q,
      }});
    });
    return false;
  }
  const handler = _CMD_HANDLERS[msg.type];
  if (handler) {
    _abortFlag = false;
    handler(msg).then(r => sendResponse(r)).catch(e => sendResponse({ok: false, error: e.message}));
    return true;
  }
  return false;
});

// ---------------------------------------------------------------------------
// Main entry point
// ---------------------------------------------------------------------------

(async function main() {
  if (!document.body) {
    await new Promise(r => document.addEventListener("DOMContentLoaded", r));
  }

  injectBadge();

  if (location.hostname !== TARGET_HOST) return;

  // WAF bot-challenge — give bd.js time to fingerprint and auto-redirect.
  // If bd.js passes: page navigates away, this context is destroyed, and content.js
  // fires a normal page-ready on the destination. If bd.js fails: no redirect happens
  // and we report waf-challenge after the dwell so background.js can abort.
  if (document.querySelector('script[src*="/ch/bd.js"]')) {
    const {"challenge-count": prev = 0} = await storageGet("challenge-count");
    const count = prev + 1;
    await storageSet({"challenge-count": count});
    console.log(`[OctoProbe] Bot challenge page #${count} — waiting 20s for bd.js auto-redirect`);
    setBadge(`WAF challenge (${count}/2) — waiting…`, "#888888");

    // Immediately notify background that a WAF challenge is in progress so it extends
    // its waitForPageReady rather than timing out silently. Background only acts on this
    // if the final "waf-challenge" fires after 20s (no redirect).
    chrome.runtime.sendMessage({type: "page-ready", state: "waf-challenge-active", url: location.href}).catch(() => {});

    // Dwell: bd.js fingerprints the browser and auto-redirects if it passes.
    // If the page navigates (success), this setTimeout never fires — context is dead.
    await new Promise(r => setTimeout(r, 20000));

    // Still on the challenge page — bd.js did not redirect.
    console.warn("[OctoProbe] WAF challenge not solved after 20s");
    if (count >= 2) {
      await storageSet({"challenge-count": 0});
      setBadge("IP BLOCKED — switch proxy", "#ff0000");
    } else {
      setBadge("WAF failed — reporting to bg", "#ff6b6b");
    }
    chrome.runtime.sendMessage({type: "page-ready", state: "waf-challenge", url: location.href}).catch(() => {});
    return;
  }

  await storageSet({"challenge-count": 0});

  // Persist any pending account set just before token submit (survives navigation).
  const {"pending-account": pendingAccount} = await storageGet("pending-account");
  if (pendingAccount) {
    console.log("[OctoProbe] Found pending account — saving:", pendingAccount.username);
    const saveResult = await sendBgMessage({type: "save-account", account: pendingAccount}).catch(() => null);
    if (saveResult?.ok) {
      await new Promise(res => chrome.storage.local.remove("pending-account", res));
      chrome.runtime.sendMessage({type: "stop-email-poll"});
      console.log("[OctoProbe] Account saved:", saveResult.filename);
    } else {
      console.warn("[OctoProbe] save-account failed:", saveResult);
    }
  }

  // Dismiss cookie consent before reporting state — it blocks clicks when visible.
  await dismissCookieConsent();

  const state = _detectPageState();
  console.log(`[OctoProbe] page-ready: state=${state} url=${location.href}`);
  chrome.runtime.sendMessage({type: "page-ready", state, url: location.href}).catch(() => {});

  // On schedule pages, install a passive XHR intercept to report slot observations.
  // This runs in the background during warmup idle — workers on the schedule page
  // continuously see slot data as the page polls /VistosOnline/slots.
  if (state === "schedule") _installSlotsXhrHook();

  setBadge(`Octo Probe v${chrome.runtime.getManifest().version}`, "#00d4aa");
})();

// ---------------------------------------------------------------------------
// Passive XHR intercept — reports /VistosOnline/slots responses to manager
// ---------------------------------------------------------------------------

function _installSlotsXhrHook() {
  // Use session-key-derived names so no static "octo-*" or "__octo*" string appears in MAIN world.
  const _hookFlag   = '_h' + _sessionSk;
  const _slotsType  = '_s' + _sessionSk;

  // Run in MAIN world via executeScript so it can intercept page's own XHR instances.
  chrome.runtime.sendMessage({
    type: "exec-page-script",
    code: `(function(hookFlag, msgType){
      if (window[hookFlag]) return;
      window[hookFlag] = true;
      var _open = XMLHttpRequest.prototype.open;
      XMLHttpRequest.prototype.open = function(method, url) {
        if (typeof url === "string" && url.includes("/VistosOnline/slots")) {
          this.addEventListener("load", function() {
            try {
              var data = JSON.parse(this.responseText);
              if (Array.isArray(data)) {
                var m = url.match(/posto_id=(\\d+)/);
                var postId = m ? Number(m[1]) : 0;
                var slots = [];
                for (var i = 0; i < data.length; i++) {
                  var s = data[i];
                  if (!s.date || !s.periods) continue;
                  for (var j = 0; j < s.periods.length; j++) {
                    var p = s.periods[j];
                    var t = typeof p === "string" ? p : (p.time || p.hour || "");
                    if (t) slots.push({date: s.date, time: t});
                  }
                }
                if (slots.length) window.postMessage({type:msgType, postId, slots}, "*");
              }
            } catch(_) {}
          });
        }
        return _open.apply(this, arguments);
      };
    })(${JSON.stringify(_hookFlag)}, ${JSON.stringify(_slotsType)});`,
  }).catch(() => {});

  // Forward postMessage observations to background
  window.addEventListener("message", (ev) => {
    if (ev.source !== window || ev.data?.type !== _slotsType) return;
    chrome.runtime.sendMessage({
      type: "slot-observation",
      postId: ev.data.postId,
      slots:  ev.data.slots,
    }).catch(() => {});
  });
}