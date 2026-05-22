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
// Utilities
// ---------------------------------------------------------------------------

function sleep(ms) { return new Promise(r => setTimeout(r, ms)); }

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

async function humanMoveTo(el) {
  const rect   = el.getBoundingClientRect();
  const tx     = rect.left + rect.width  * (0.25 + Math.random() * 0.5);
  const ty     = rect.top  + rect.height * (0.25 + Math.random() * 0.5);
  const sx     = window._mX ?? innerWidth  * 0.4;
  const sy     = window._mY ?? innerHeight * 0.3;
  const steps  = 8 + Math.floor(Math.random() * 8);
  const spread = 70;
  const cp1x   = sx + (tx-sx)*0.33 + (Math.random()-.5)*spread;
  const cp1y   = sy + (ty-sy)*0.33 + (Math.random()-.5)*spread;
  const cp2x   = sx + (tx-sx)*0.66 + (Math.random()-.5)*spread;
  const cp2y   = sy + (ty-sy)*0.66 + (Math.random()-.5)*spread;
  for (let i = 0; i <= steps; i++) {
    const t  = i / steps;
    const cx = Math.round(_bezier(t, sx, cp1x, cp2x, tx));
    const cy = Math.round(_bezier(t, sy, cp1y, cp2y, ty));
    document.dispatchEvent(new MouseEvent("mousemove", {bubbles: true, clientX: cx, clientY: cy}));
    window._mX = cx; window._mY = cy;
    await sleep(7 + Math.random() * 12);
  }
  await sleep(30 + Math.random() * 50);
}

async function humanClick(el) {
  await humanMoveTo(el);
  const rect = el.getBoundingClientRect();
  const cx   = Math.round(rect.left + rect.width  * 0.5);
  const cy   = Math.round(rect.top  + rect.height * 0.5);
  const opts = {bubbles: true, clientX: cx, clientY: cy};
  el.dispatchEvent(new MouseEvent("mousedown", opts));
  await sleep(30 + Math.random() * 60);
  el.dispatchEvent(new MouseEvent("mouseup",   opts));
  el.dispatchEvent(new MouseEvent("click",     opts));
  el.focus();
}

async function humanType(el, text) {
  await humanClick(el);
  await sleep(100 + Math.random() * 150);
  const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")?.set;
  for (const ch of text) {
    // Base typing speed 60-180 ms; rare 7% chance of a longer pause (hesitation)
    const delay = 60 + Math.random() * 120 + (Math.random() < 0.07 ? 280 + Math.random() * 400 : 0);
    await sleep(delay);
    el.dispatchEvent(new KeyboardEvent("keydown",  {key: ch, bubbles: true}));
    const cur = el.value;
    if (setter) setter.call(el, cur + ch); else el.value = cur + ch;
    el.dispatchEvent(new InputEvent("input",  {data: ch, inputType: "insertText", bubbles: true}));
    el.dispatchEvent(new KeyboardEvent("keyup", {key: ch, bubbles: true}));
  }
  await sleep(80 + Math.random() * 100);
  el.dispatchEvent(new Event("change", {bubbles: true}));
}

async function humanSelect(el, value) {
  await humanClick(el);
  await sleep(180 + Math.random() * 250);
  el.value = value;
  el.dispatchEvent(new Event("change", {bubbles: true}));
  await sleep(80 + Math.random() * 120);
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

// ---------------------------------------------------------------------------
// Badge
// ---------------------------------------------------------------------------

let badge = null;

function injectBadge() {
  badge = document.getElementById("octo-probe-badge");
  if (badge) return;
  badge = document.createElement("div");
  badge.id = "octo-probe-badge";
  Object.assign(badge.style, {
    position: "fixed", top: "8px", right: "8px", zIndex: "2147483647",
    background: "#1a1a2e", color: "#00d4aa",
    font: "bold 11px monospace", padding: "4px 10px",
    borderRadius: "4px", boxShadow: "0 2px 6px rgba(0,0,0,0.5)",
    pointerEvents: "none", userSelect: "none",
    letterSpacing: "0.5px", transition: "color 0.3s",
  });
  const _ver = chrome.runtime.getManifest().version;
  setBadge(`Octo Probe v${_ver}`, "#00d4aa");
  (document.body ?? document.documentElement).appendChild(badge);
}

function setBadge(text, color = "#00d4aa") {
  if (!badge) return;
  badge.textContent = text;
  badge.style.color  = color;
}

// ---------------------------------------------------------------------------
// Shared: language switch
// ---------------------------------------------------------------------------

async function switchToEnglish() {
  await sleep(300);
  const langSel = await waitFor(
    () => LANG_SELECTORS.map(s => document.querySelector(s)).find(Boolean),
    5000
  );
  if (!langSel) { console.warn("[OctoProbe] Language select not found."); return; }
  if (langSel.value === "ENG") { console.log("[OctoProbe] Already English."); return; }

  setBadge("Switching to English…", "#f0c040");
  langSel.value = "ENG";
  langSel.dispatchEvent(new Event("change", {bubbles: true}));
  await sleep(1500);
}

// ---------------------------------------------------------------------------
// Shared: click Login button
// ---------------------------------------------------------------------------

async function clickLoginLink() {
  setBadge("Finding Login…", "#f0c040");
  const el = await waitFor(
    () => findBySelectors(LOGIN_LINK_SELECTORS) || findByText(["a","button"], ["login","log in"]),
    6000
  );
  if (!el) { setBadge("Login button not found", "#ff6b6b"); return; }
  setBadge("Clicking Login…", "#f0c040");
  el.click();
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

// Step 1 — home page: language switch + navigate to auth page.
async function loginStepHome() {
  setBadge("Login: language…", "#f0c040");
  await switchToEnglish();
  setBadge("Login: finding login button…", "#f0c040");
  await clickLoginLink();
  await storageSet({"workflow-step": "auth"});
  // Page navigates; new content script instance picks up step "auth".
}

// Step 2 — auth page: fill credentials, solve reCAPTCHA, submit.
//
// Two-phase approach to beat the bd.js WAF challenge + reCAPTCHA 2-minute expiry race:
//
// Attempt 0 — real form POST with a dummy captchaResponse.
//   bd.js fires (WAF layer, before app code), validates the browser, sets the challenge
//   cookie, then replays the POST. The server rejects the dummy token and the browser
//   lands on /VistosOnline/login. Dispatch detects /login, navigates back to auth.
//   No real captcha credit is spent and the challenge cookie is now valid.
//
// Attempt 1+ — jQuery AJAX via humanClick (the site's own doLogin()).
//   Challenge cookie already set → no bd.js → server processes the request directly.
//   Page stays alive on failure so the next retry starts from captcha, not from scratch.
//   Captcha is solved immediately before submit — no extra sleep — so the 2-min window
//   is not wasted waiting for bd.js.
async function loginStepAuth(creds) {
  const net = await checkNetworkQuality();
  if (!net.good) {
    await _clearWorkflowFailed(`Network too slow (${net.avg}ms) — check proxy`);
    return;
  }

  const {"login-form-attempt": prevAttempt = 0} = await storageGet("login-form-attempt");
  if (prevAttempt >= 3) {
    await storageSet({"login-form-attempt": 0});
    await _clearWorkflowFailed("Login: failed after 3 attempts — check credentials");
    return;
  }

  setBadge("Login: waiting for form…", "#f0c040");
  const userEl = await waitFor(() => findBySelectors(USERNAME_SELECTORS), 10000);
  if (!userEl) { await _clearWorkflowFailed("Login: username field not found"); return; }

  setBadge("Login: filling credentials…", "#f0c040");
  await humanType(userEl, creds.username);
  await sleep(500 + Math.random() * 500);

  const passEl = await waitFor(() => findBySelectors(PASSWORD_SELECTORS), 4000);
  if (!passEl) { await _clearWorkflowFailed("Login: password field not found"); return; }
  await humanType(passEl, creds.password);
  await sleep(500 + Math.random() * 500);

  await storageSet({"login-pending": true, "login-form-attempt": prevAttempt + 1});

  if (prevAttempt >= 1) {
    // doLogin() success calls location.reload() — page reloads to Authentication.jsp with
    // the session cookie already set. Detect this before re-filling the form.
    try {
      const probe = await fetch("/VistosOnline/Questionario", {
        method: "GET", credentials: "include", redirect: "follow", cache: "no-store",
      });
      if (probe.ok && !/Authentication|authentication/i.test(probe.url)) {
        await storageSet({"workflow-type": null, "workflow-step": null, "login-pending": false, "login-form-attempt": 0});
        setBadge("Logged in!", "#00d4aa");
        location.href = "/VistosOnline/Questionario";
        return;
      }
    } catch(e) { console.warn("[OctoProbe] Early session probe error:", e); }
  }

  if (prevAttempt === 0) {
    // Phase 1: trigger bd.js challenge with a dummy token. No captcha credit spent.
    setBadge("Login: triggering WAF challenge…", "#f0c040");
    await _callPageFn(`
      (function() {
        var uEl = document.querySelector('input[name="username"], #username');
        var pEl = document.querySelector('input[name="password"], #password');
        var f = document.createElement('form');
        f.method = 'POST';
        f.action = '/VistosOnline/login';
        [['username',        uEl ? uEl.value : ''],
         ['password',        pEl ? pEl.value : ''],
         ['language',        'ENG'],
         ['rgpd',            'Y'],
         ['captchaResponse', 'challenge_pass']
        ].forEach(function(kv) {
          var i = document.createElement('input'); i.type = 'hidden';
          i.name = kv[0]; i.value = kv[1]; f.appendChild(i);
        });
        document.body.appendChild(f);
        console.log('[OctoProbe] Phase-1 login POST (challenge trigger)');
        f.submit();
      })();
    `);
    // Browser navigates away. Dispatch handles /login bounce → back to auth → attempt 1.
    return;
  }

  // Phase 2: challenge cookie set. Solve fresh captcha and submit via jQuery AJAX.
  // Page stays alive so the next retry (if needed) starts from captcha, not scratch.
  setBadge("Login: checking for reCAPTCHA…", "#f0c040");
  const rcEl = await waitFor(
    () => document.querySelector("[data-sitekey], iframe[src*='recaptcha'], .g-recaptcha"),
    8000
  );
  if (rcEl) {
    setBadge("Login: solving reCAPTCHA…", "#f0c040");
    const solved = await waitForRecaptcha();
    if (!solved) { await _clearWorkflowFailed("Login: reCAPTCHA failed"); return; }
    const r = rcEl.getBoundingClientRect();
    window._mX = Math.round(r.left + r.width  * 0.5);
    window._mY = Math.round(r.top  + r.height * 0.5);
    // No extra sleep — submit immediately while the token is fresh
  } else {
    await sleep(800 + Math.random() * 600);
  }

  await injectAlertCapture();

  const submitBtn = await waitFor(() => findBySelectors(LOGIN_SUBMIT_SELECTORS), 4000);
  if (!submitBtn) { await _clearWorkflowFailed("Login: submit button not found"); return; }

  setBadge("Login: submitting…", "#f0c040");
  await humanClick(submitBtn);

  // RGPD consent popup — only on the first login of a new session.
  const rgpdEl = await waitFor(() => {
    const el = document.querySelector("#loginMsg");
    return isVisible(el) ? el : null;
  }, 12000);
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

  // If the page navigated on its own (jQuery followed the redirect), script is destroyed.
  // Still alive here means the browser stayed on Authentication.jsp.
  setBadge("Login: waiting for response…", "#f0c040");
  await sleep(5000);
  if (!/Authentication|authentication/i.test(location.pathname + location.search)) return;

  // Silent session probe — doLogin() sets the cookie even when jQuery stays on auth URL.
  setBadge("Login: probing session…", "#f0c040");
  let sessionOk = false;
  try {
    const probe = await fetch("/VistosOnline/Questionario", {
      method: "GET", credentials: "include", redirect: "follow", cache: "no-store",
    });
    sessionOk = probe.ok && !/Authentication|authentication/i.test(probe.url);
    console.log(`[OctoProbe] Login session probe: ${probe.url} — ${sessionOk ? "OK" : "FAIL"}`);
  } catch(e) {
    console.warn("[OctoProbe] Session probe error:", e);
  }

  if (sessionOk) {
    await storageSet({"workflow-type": null, "workflow-step": null, "login-pending": false, "login-form-attempt": 0});
    setBadge("Logged in!", "#00d4aa");
    location.href = "/VistosOnline/Questionario";
    return;
  }

  // Captcha or credentials rejected — reload for a fresh reCAPTCHA widget on next attempt.
  await storageSet({"login-pending": false});
  setBadge("Login: retrying…", "#f0c040");
  await sleep(2000 + Math.random() * 1000);
  location.reload();
}

// ---------------------------------------------------------------------------
// Register workflow — home page step
// ---------------------------------------------------------------------------

async function registerStepHome() {
  setBadge("Register: language…", "#f0c040");
  await switchToEnglish();
  setBadge("Register: login page…", "#f0c040");
  await clickLoginLink();
  await storageSet({"workflow-step": "auth"});
  // page will navigate, new content script picks up step "auth"
}

// ---------------------------------------------------------------------------
// Register workflow — auth page step (click Register link -> fill form)
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
  const scrollTimer = setInterval(() => {
    scrollPos += 80 + Math.round(Math.random() * 80);
    window.scrollTo({top: scrollPos, behavior: "smooth"});
  }, 5000);

  setTimeout(async () => {
    clearInterval(scrollTimer);
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

async function _clearWorkflowFailed(reason) {
  setBadge(reason, "#ff6b6b");
  await storageSet({"workflow-type": null, "workflow-step": null, "email-token": null, "register-retried": null, "login-form-attempt": 0, "register-partial-attempt": 0, "register-submit-attempt": 0, "register-post-submitted": false, "warmup-end-time": null, "warmup-site-index": 0});
  chrome.runtime.sendMessage({type: "stop-email-poll"});
}

async function registerStepAuth(person, emailAcct) {
  // Fail fast if the proxy connection is too slow — poor network dramatically
  // increases reCAPTCHA rejection rate and wastes API solver credits.
  const net = await checkNetworkQuality();
  if (!net.good) {
    await _clearWorkflowFailed(`Network too slow (${net.avg}ms avg) — check proxy`);
    return;
  }

  // Guard against the partial-challenge loop: clicking Register triggers
  // $.load('/VistosOnline/partials/registration.jsp'); if that returns the bd.js
  // challenge page, the script runs in the main page context and navigates the whole
  // page to Authentication.jsp — killing the registration flow silently. Our
  // challenge-count detector doesn't see AJAX-level challenges, so we track Register
  // clicks across restarts with a separate counter.
  const {"register-partial-attempt": partialAttempt = 0} = await storageGet("register-partial-attempt");
  if (partialAttempt >= 3) {
    await storageSet({"register-partial-attempt": 0});
    await _clearWorkflowFailed("Register form blocked — IP challenged on partial load, switch proxy");
    return;
  }

  setBadge("Register: finding link…", "#f0c040");

  const regEl = await waitFor(
    () => findBySelectors(REGISTER_LINK_SELECTORS)
       || findByText(["span","a","button","div"], REGISTER_LINK_TEXTS),
    8000
  );
  if (!regEl) { await _clearWorkflowFailed("Register link not found"); return; }

  await storageSet({"register-partial-attempt": partialAttempt + 1});
  await sleep(900 + Math.random() * 1000);
  await humanClick(regEl);
  await sleep(300 + Math.random() * 300);

  setBadge("Register: waiting for form…", "#f0c040");
  const form = await waitFor(() => document.querySelector("#formReg"), 12000);
  if (!form) { await _clearWorkflowFailed("Register form: partial blocked by bot challenge — switch proxy"); return; }

  // Form loaded — reset the partial-challenge counter.
  await storageSet({"register-partial-attempt": 0});

  setBadge("Register: filling form…", "#f0c040");
  await fillRegisterForm(person, emailAcct.email);

  // Verify every required field is filled before we ask the user to solve reCAPTCHA.
  // If any field is blank, re-fill only those fields, then check one more time.
  setBadge("Register: validating form…", "#f0c040");
  const missingFirst = _checkFormFields();
  if (missingFirst.length) {
    console.warn("[OctoProbe] Blank fields after fill — re-filling:", missingFirst);
    setBadge("Register: re-filling fields…", "#f0c040");
    await _refillMissing(missingFirst, person, emailAcct.email);
    await sleep(400 + Math.random() * 300);
    const missingFinal = _checkFormFields();
    if (missingFinal.length) {
      await _clearWorkflowFailed(`Form incomplete: ${missingFinal.join(", ")}`);
      return;
    }
  }
  console.log("[OctoProbe] All form fields verified — two-phase WAF submit");

  // Two-phase approach (mirrors loginStepAuth) to beat the bd.js WAF challenge
  // that now fires on every POST /VistosOnline/register:
  //
  // Phase 0 — real form POST with dummy captcha. bd.js executes in browser context,
  //   validates fingerprint, sets challenge cookie, replays the POST. Server rejects
  //   the dummy token. Browser navigates somewhere. Challenge cookie is now set.
  //   No captcha credit spent. register-post-submitted stays false.
  //
  // Phase 1+ — fill fields, solve fresh captcha, submit immediately. Challenge
  //   cookie is set → no bd.js → server processes registration → email sent.
  //   register-post-submitted=true so dispatch advances to email polling wherever
  //   the browser lands after the navigation.
  const {"register-submit-attempt": submitAttempt = 0} = await storageGet("register-submit-attempt");
  if (submitAttempt >= 3) {
    await _clearWorkflowFailed("Register: WAF persists after 3 attempts — switch proxy");
    return;
  }
  await storageSet({"register-submit-attempt": submitAttempt + 1});

  if (submitAttempt === 0) {
    // Phase 0: trigger WAF challenge — no captcha token spent.
    setBadge("Register: triggering WAF challenge…", "#f0c040");
    await _callPageFn(`
      (function() {
        var f = document.querySelector('#formReg');
        if (!f) return;
        var rgpd = f.querySelector('[name="rgpd"]');
        if (!rgpd) { rgpd = document.createElement('input'); rgpd.type = 'hidden'; rgpd.name = 'rgpd'; f.appendChild(rgpd); }
        rgpd.value = 'Y';
        var cap = f.querySelector('[name="captchaResponse"]');
        if (cap) cap.value = 'challenge_pass';
        console.log('[OctoProbe] Phase-0 register POST (challenge trigger)');
        f.submit();
      })();
    `);
    // Browser navigates away — script destroyed.
    // Dispatch detects no register-post-submitted → calls registerStepAuth(submitAttempt=1).
    return;
  }

  // Phase 1+: challenge cookie set. Solve fresh captcha and submit immediately.
  // Fields are already filled from fillRegisterForm() above — don't re-fill.
  setBadge("Register: solving reCAPTCHA…", "#f0c040");
  const solved = await waitForRecaptcha();
  if (!solved) { await _clearWorkflowFailed("reCAPTCHA not solved — timed out"); return; }

  // Inject alert capture AFTER captcha solve (before: reCAPTCHA checks native toString).
  await injectAlertCapture();
  document.addEventListener("octo-alert", (e) => {
    console.log(`[OctoProbe] Register alert: "${e.detail.msg}"`);
  }, {once: true});

  // Anchor mouse at reCAPTCHA widget for natural Bézier trajectory.
  const rcEl = document.querySelector(
    "iframe[src*='recaptcha'], .g-recaptcha, #recaptcha, [class*='recaptcha']"
  );
  if (rcEl) {
    const r = rcEl.getBoundingClientRect();
    window._mX = Math.round(r.left + r.width  * 0.5);
    window._mY = Math.round(r.top  + r.height * 0.5);
  }
  // No extra sleep — submit while token is fresh (2-min expiry window).

  // Start email polling before navigating — success may navigate away immediately.
  await storageSet({"register-post-submitted": true, "workflow-step": "token"});
  chrome.runtime.sendMessage({type: "start-email-poll", jwt: emailAcct.jwt});

  setBadge("Register: submitting…", "#f0c040");
  await _callPageFn(`
    (function() {
      var f = document.querySelector('#formReg');
      if (!f) return;
      var rgpd = f.querySelector('[name="rgpd"]');
      if (!rgpd) { rgpd = document.createElement('input'); rgpd.type = 'hidden'; rgpd.name = 'rgpd'; f.appendChild(rgpd); }
      rgpd.value = 'Y';
      console.log('[OctoProbe] Phase-1 register POST (real captcha + rgpd=Y)');
      f.submit();
    })();
  `);
  // Browser navigates away. Dispatch reads register-post-submitted=true on landing
  // page and advances to email verification regardless of landing URL.
}

// Pick a nationality from the dropdown's live options and persist the resolved value.
// Known nationality adjective → Portuguese country-name keyword.
// Keys are lowercased substrings found in passport nationality field.
// Match values are substrings expected in the form's dropdown option text.
const _NAT_ALIASES = [
  { keys: ["cabo-verd", "caboverd", "cape verd"],                          match: "cabo verde"   },
  { keys: ["guineense", "guiné-bissau", "guinea-biss", "guinea-biss"],     match: "guiné-bissau" },
  { keys: ["guinéenne", "guineenne", "guinéen"],                           match: "guiné"        },
  { keys: ["senegal"],                                                      match: "senegal"      },
  { keys: ["brasil", "brazil", "brasileir"],                               match: "brasil"       },
  { keys: ["india", "índi", "indian"],                                     match: "índi"         },
  { keys: ["china", "chinês", "chines"],                                   match: "chin"         },
  { keys: ["moçambique", "mozambique", "moçambic"],                        match: "moçambique"   },
  { keys: ["angola"],                                                       match: "angola"       },
  { keys: ["filipin", "philippin"],                                        match: "filipin"      },
  { keys: ["pakist", "paquistã"],                                          match: "pakist"       },
  { keys: ["marrocos", "morocc", "marroquin"],                             match: "marrocos"     },
  { keys: ["colômbi", "colombi"],                                          match: "colômbi"      },
];

function _norm(s) {
  return s.toLowerCase().normalize("NFD").replace(/[̀-ͯ]/g, "").replace(/[-\s]+/g, " ").trim();
}

// Match a raw nationality string (from passport) against form dropdown options.
// Returns the matching option element, or null if no confident match.
function _matchNationality(nat, validOpts) {
  const n = _norm(nat);
  // Alias map — checked in priority order; Guinea-Bissau before generic Guinea.
  for (const {keys, match} of _NAT_ALIASES) {
    if (keys.some(k => n.includes(_norm(k)))) {
      const matchNorm = _norm(match);
      for (const opt of validOpts) {
        if (_norm(opt.text).includes(matchNorm)) return opt;
      }
    }
  }
  // Direct contains as last resort
  for (const opt of validOpts) {
    const t = _norm(opt.text);
    if (t.includes(n) || n.includes(t)) return opt;
  }
  return null;
}

async function _selectNationality(natEl, person) {
  const validOpts = [...natEl.options].filter(o =>
    o.value && o.value.trim() !== "" &&
    !/^(-+|select|choose|pick|all|\?\?)/i.test(o.text.trim())
  );

  if (!validOpts.length) {
    console.warn("[OctoProbe] No valid nationality options — using stored:", person.nationality);
    await humanSelect(natEl, person.nationality);
    return;
  }

  let chosen;
  // Real person nationality is a word/phrase (length > 4); try to match it.
  if (person.nationality && person.nationality.length > 4) {
    chosen = _matchNationality(person.nationality, validOpts);
    if (chosen) {
      console.log(`[OctoProbe] Nationality matched: "${chosen.text.trim()}" (${chosen.value})`);
    } else {
      console.warn(`[OctoProbe] Nationality "${person.nationality}" unmatched — picking random`);
    }
  }
  // Auto-generated or unmatched: random pick.
  if (!chosen) {
    chosen = validOpts[Math.floor(Math.random() * validOpts.length)];
    console.log(`[OctoProbe] Nationality (random): "${chosen.text.trim()}" (${chosen.value})`);
  }

  person.nationality = chosen.value;
  await storageSet({"register-person": Object.assign({}, person)});
  await humanSelect(natEl, person.nationality);
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
    // Human-like pause between fields: 400–900 ms, occasionally longer
    await sleep(400 + Math.random() * 500 + (Math.random() < 0.15 ? 600 + Math.random() * 800 : 0));
  }

  // Select fields — click, brief look, choose
  const genderEl = document.querySelector("#gender");
  if (genderEl) await humanSelect(genderEl, person.gender);
  await sleep(300 + Math.random() * 400);

  const natEl = document.querySelector("#nationality");
  if (natEl) await _selectNationality(natEl, person);
  await sleep(300 + Math.random() * 400);
}

// Dispatcher — routes to human or API path based on captcha-mode storage key.
async function waitForRecaptcha() {
  const { "captcha-mode": mode } = await storageGet(["captcha-mode"]);
  return mode === "human" ? _waitForRecaptchaHuman() : _waitForRecaptchaApi();
}

// Human mode — inject the watcher and wait for the user to solve manually.
async function _waitForRecaptchaHuman(maxMs = 300000) {
  const r = await sendBgMessage({type: "inject-recaptcha-watcher"}).catch(() => null);
  if (r?.ok) {
    console.log("[OctoProbe] reCAPTCHA watcher injected via scripting API");
  } else {
    console.warn("[OctoProbe] Watcher injection failed:", r?.error, "— DOM fallback");
    const reset = document.createElement("script");
    reset.textContent = `window._octoRcpWatcher = false; window._octoRcpToken = null;`;
    (document.head || document.documentElement).appendChild(reset);
    reset.remove();
    const s = document.createElement("script");
    s.textContent = `(function(){
      if (window._octoRcpWatcher) return;
      window._octoRcpWatcher = true;
      function check() {
        try {
          const r = typeof grecaptcha !== "undefined" &&
            (grecaptcha.enterprise?.getResponse?.() || grecaptcha.getResponse?.());
          if (r && r.length > 0) {
            window._octoRcpToken = r;
            document.dispatchEvent(new CustomEvent("octo-recaptcha-pass", {detail:{token:r}}));
            return;
          }
        } catch(_) {}
        setTimeout(check, 800);
      }
      check();
    })();`;
    (document.head || document.documentElement).appendChild(s);
    s.remove();
  }

  return new Promise((resolve) => {
    const timer = setTimeout(() => { console.warn("[OctoProbe] reCAPTCHA timeout."); resolve(false); }, maxMs);
    function onPass() {
      clearTimeout(timer); clearInterval(poll);
      console.log("[OctoProbe] reCAPTCHA solved (human).");
      resolve(true);
    }
    document.addEventListener("octo-recaptcha-pass", onPass, {once: true});
    const poll = setInterval(() => {
      const el = document.querySelector("#g-recaptcha-response-1, #g-recaptcha-response");
      if (el?.value?.length > 0) {
        document.removeEventListener("octo-recaptcha-pass", onPass);
        clearInterval(poll); clearTimeout(timer);
        console.log("[OctoProbe] reCAPTCHA solved (textarea fallback).");
        resolve(true);
      }
    }, 800);
  });
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
const _MIN_API_SOLVE_MS = 8000;

// API mode — send to solver service, inject token, resolve when widget confirms.
async function _waitForRecaptchaApi() {
  setBadge("API: solving reCAPTCHA…", "#9060cc");
  const solveStart = Date.now();

  const siteKey = await _extractSiteKey();
  if (!siteKey) {
    console.error("[OctoProbe] API mode: sitekey not found on page");
    return false;
  }
  // Extract the reCAPTCHA Enterprise action — embedded in the token and validated server-side.
  // Without the correct action the server rejects the token even if the sitekey matches.
  const action = document.querySelector("[data-sitekey][data-action]")?.dataset?.action
              ?? document.querySelector("[data-action]")?.dataset?.action
              ?? null;
  console.log("[OctoProbe] Sending to solver — siteKey:", siteKey, "action:", action);

  const result = await sendBgMessage({type: "solve-recaptcha-api", pageUrl: location.href, siteKey, action}).catch(() => null);
  if (!result?.ok || !result.token) {
    console.error("[OctoProbe] API solve failed:", result?.error);
    return false;
  }

  // Enforce minimum solve time — pad if the service was unusually fast.
  const elapsed = Date.now() - solveStart;
  if (elapsed < _MIN_API_SOLVE_MS) {
    const pad = _MIN_API_SOLVE_MS - elapsed;
    console.log(`[OctoProbe] Solver returned in ${elapsed}ms — padding ${pad}ms to reach ${_MIN_API_SOLVE_MS}ms minimum`);
    setBadge("API: solve too fast — padding…", "#9060cc");
    await sleep(pad);
  }
  console.log(`[OctoProbe] Token accepted after ${Date.now() - solveStart}ms total.`);

  // Inject token — the injection dispatches octo-recaptcha-pass so we listen first.
  const solved = await new Promise(resolve => {
    document.addEventListener("octo-recaptcha-pass", () => resolve(true), {once: true});
    sendBgMessage({type: "inject-recaptcha-token", token: result.token})
      .then(r => { if (!r?.ok) { console.error("[OctoProbe] Token injection failed:", r?.error); resolve(false); } })
      .catch(() => resolve(false));
    setTimeout(() => resolve(false), 10000);
  });

  if (solved) console.log("[OctoProbe] reCAPTCHA injected (API mode).");
  return solved;
}

// Override window.alert (and optionally confirm/prompt) in the page's MAIN world.
// confirmToo=true adds confirm/prompt — only pass this at form-submit time, NOT during
// login, because reCAPTCHA Enterprise checks native-function toString() for tampering.
async function injectAlertCapture(confirmToo = false) {
  const r = await sendBgMessage({type: "inject-alert-capture", confirmToo}).catch(() => null);
  if (r?.ok) {
    console.log(`[OctoProbe] alert${confirmToo ? "/confirm/prompt" : ""} suppressed (CSP-safe)`);
    return r;
  }
  console.warn("[OctoProbe] scripting API injection failed:", r?.error, "— trying DOM fallback");
  const s = document.createElement("script");
  s.textContent = `(function(confirmToo){
    if (!window._octoAlertHooked) {
      window._octoAlertHooked = true;
      window.alert = function(msg) {
        window._octoLastAlert = msg;
        document.dispatchEvent(new CustomEvent("octo-alert", {detail: {msg: String(msg)}}));
      };
    }
    if (confirmToo && !window._octoConfirmHooked) {
      window._octoConfirmHooked = true;
      window.confirm = function(msg) {
        window._octoLastConfirm = msg;
        document.dispatchEvent(new CustomEvent("octo-confirm", {detail: {msg: String(msg)}}));
        return true;
      };
      window.prompt = function(_msg, def) { return def ?? ""; };
    }
  })(${confirmToo});`;
  (document.head || document.documentElement).appendChild(s);
  s.remove();
}

// ---------------------------------------------------------------------------
// Register workflow — token page step
// ---------------------------------------------------------------------------

async function registerStepToken(emailAcct) {
  setBadge("Token: polling email…", "#f0c040");

  // Suppress alert() now — the format-error alert fires in MAIN world and
  // blocks script execution as a native dialog if not suppressed first.
  await injectAlertCapture();

  // Poll email first — the form stays on screen while we wait
  let token = await waitForEmailToken(emailAcct?.jwt, 120000);

  const urlToken = new URLSearchParams(location.search).get("token");
  console.log("[OctoProbe] URL ?token param:", urlToken);

  if (!token) {
    console.warn("[OctoProbe] Email token not found — no fallback available");
    setBadge("No token found", "#ff6b6b");
    return;
  }

  // The site's validation regex has no 'i' flag — all hex must be lowercase.
  token = token.toLowerCase();
  console.log("[OctoProbe] Token to submit (lowercased):", token);
  setBadge(`Token: ${token.slice(0, 12)}…`, "#f0c040");

  // Re-query the token input fresh AFTER email polling to avoid stale references
  // and to allow the AJAX form time to fully load
  const findTokenInput = () => {
    for (const sel of TOKEN_INPUT_SELECTORS) {
      const el = document.querySelector(sel);
      if (isVisible(el)) return el;
    }
    return null;
  };

  const tokenInput = await waitFor(findTokenInput, 15000);
  if (!tokenInput) { setBadge("Token form not found", "#ff6b6b"); return; }

  // Log all inputs for diagnostics
  const allInputs = [...document.querySelectorAll("#mainContent input, form input")];
  console.log("[OctoProbe] Inputs on page:", allInputs.map(i =>
    `type=${i.type} id=${i.id} name=${i.name} visible=${isVisible(i)}`
  ));
  console.log("[OctoProbe] Selected token input:", tokenInput.outerHTML.slice(0, 200));

  // Scroll into view and give the user a moment to "read" the form
  tokenInput.scrollIntoView({behavior: "smooth", block: "center"});
  await sleep(900 + Math.random() * 800);

  // Human-like click into the field (cursor curves to it)
  await humanClick(tokenInput);
  await sleep(150 + Math.random() * 200);

  // Type the token character by character in MAIN world — jQuery sees each keystroke
  // through its own event system, same as a real user typing.
  setBadge("Typing token…", "#f0c040");
  const fillResult = await sendBgMessage({type: "fill-token", token}).catch(() => null);
  console.log("[OctoProbe] Type-token result:", fillResult);

  await sleep(300 + Math.random() * 300);
  console.log("[OctoProbe] Field value after fill:", tokenInput.value);

  // Isolated-world fallback if scripting API failed or left no value
  if (!tokenInput.value) {
    console.warn("[OctoProbe] Scripting API fill failed — isolated-world humanType fallback");
    // Re-query fresh: the AJAX load may have re-rendered the form element
    const freshInput = findTokenInput();
    const target = freshInput ?? tokenInput;
    // Clear any partial content before re-typing
    target.focus();
    const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")?.set;
    if (setter) setter.call(target, ""); else target.value = "";
    target.dispatchEvent(new Event("input", {bubbles: true}));
    await humanType(target, token);
  }

  const submitBtn = TOKEN_SUBMIT_SELECTORS
    .map(sel => document.querySelector(sel))
    .find(el => isVisible(el));

  if (!submitBtn) { setBadge("Token submit not found", "#ff6b6b"); return; }

  // Store account as pending BEFORE clicking submit — the page navigates on success
  // which kills this script, so we must commit the data before the click.
  // Authentication.jsp picks it up on the next load.
  const {"register-person": savedPerson} = await storageGet("register-person");
  if (savedPerson) {
    const pendingAccount = {
      username:      savedPerson.username,
      password:      savedPerson.password,
      name:          savedPerson.name,
      surname:       savedPerson.surname,
      email:         emailAcct?.email ?? "",
      birth_date:    savedPerson.birth_date,
      gender:        savedPerson.gender,
      nationality:   savedPerson.nationality,
      traveldoc:     savedPerson.traveldoc,
      registered_at: new Date().toISOString(),
    };
    await storageSet({"pending-account": pendingAccount});
    console.log("[OctoProbe] Pending account stored pre-submit:", pendingAccount.username);
  }

  console.log("[OctoProbe] Submitting token form:", submitBtn.outerHTML.slice(0, 120));
  setBadge("Verifying token…", "#f0c040");
  await sleep(1100 + Math.random() * 1000);
  await humanClick(submitBtn);

  // Page navigates to Authentication.jsp on success — script is destroyed here.
  // The pending-account is picked up by main() on the next page load.
  setBadge("Awaiting verification…", "#f0c040");
}

function _normaliseToken(t) {
  // Convert bare 32-char hex to hyphenated UUID if needed
  if (t && /^[0-9a-f]{32}$/i.test(t))
    return `${t.slice(0,8)}-${t.slice(8,12)}-${t.slice(12,16)}-${t.slice(16,20)}-${t.slice(20,32)}`;
  return t;
}

async function waitForEmailToken(jwt, maxMs = 120000) {
  const deadline = Date.now() + maxMs;

  let bgToken = null;
  const bgListener = (msg) => { if (msg.type === "email-token") bgToken = msg.token; };
  chrome.runtime.onMessage.addListener(bgListener);

  while (Date.now() < deadline) {
    const {["email-token"]: stored} = await storageGet("email-token");
    if (stored) {
      const tok = _normaliseToken(stored);
      console.log("[OctoProbe] Email token from storage:", stored, "→", tok);
      chrome.runtime.onMessage.removeListener(bgListener);
      return tok;
    }

    if (bgToken) {
      const tok = _normaliseToken(bgToken);
      console.log("[OctoProbe] Email token from background:", bgToken, "→", tok);
      chrome.runtime.onMessage.removeListener(bgListener);
      return tok;
    }

    try {
      const r     = await fetch(`${MAILTM}/messages`, {headers: {Authorization: `Bearer ${jwt}`}});
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

        const tok = extractTokenFromMsg(msg);
        if (tok) {
          console.log("[OctoProbe] Token extracted:", tok);
          chrome.runtime.onMessage.removeListener(bgListener);
          return tok;
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

function _hexToUUID(h) {
  // Convert 32-char hex to xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx
  return `${h.slice(0,8)}-${h.slice(8,12)}-${h.slice(12,16)}-${h.slice(16,20)}-${h.slice(20,32)}`;
}

function extractTokenFromMsg(msg) {
  const text = msg?.text ?? "";
  // mail.tm returns html as an array of strings or a single string
  const rawHtml = Array.isArray(msg?.html) ? msg.html.join(" ") : (msg?.html ?? "");
  const html = rawHtml.replace(/<[^>]+>/g, " ");
  const body  = text || html;

  const patterns = [
    [/\b([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\b/i, "UUID with hyphens"],
    [/\b([0-9a-f]{32})\b/i,                        "32-char hex (UUID no hyphens)"],
    [/[?&]token=([A-Za-z0-9\-]{6,})/,              "URL ?token param"],
    [/token[=:\s]+([A-Za-z0-9\-]{6,})/i,            "token= assignment"],
    [/c[oó]digo[:\s]+([A-Za-z0-9\-]{4,})/i,         "Portuguese 'código'"],
    [/code[:\s]+([A-Za-z0-9\-]{4,})/i,               "English 'code'"],
    [/\b([0-9]{4,8})\b/,                              "standalone digits"],
  ];

  for (const [re, label] of patterns) {
    const m = body.match(re);
    if (!m) continue;

    let token = m[1];
    // If matched a raw 32-char hex, convert to hyphenated UUID format
    if (/^[0-9a-f]{32}$/i.test(token)) {
      token = _hexToUUID(token);
      console.log(`[OctoProbe] Token converted hex→UUID: ${token}`);
    }
    console.log(`[OctoProbe] Token match via "${label}": ${token}`);
    return token;
  }
  return null;
}

// ---------------------------------------------------------------------------
// Network quality probe
// ---------------------------------------------------------------------------

// Ping Google's dedicated connectivity endpoint twice and return the average RTT.
// Returns {avg, good} — "good" means avg RTT is below the threshold.
const _NET_BAD_MS  = 2000;  // avg RTT above this = poor proxy, stop workflow
const _NET_PROBE   = "https://www.google.com/generate_204";

async function checkNetworkQuality() {
  setBadge("Checking network…", "#f0c040");
  const times = [];
  for (let i = 0; i < 2; i++) {
    const t0 = performance.now();
    try {
      const ctrl = new AbortController();
      const tid  = setTimeout(() => ctrl.abort(), 5000);
      await fetch(_NET_PROBE, {method:"HEAD", mode:"no-cors", cache:"no-store", signal: ctrl.signal});
      clearTimeout(tid);
      times.push(performance.now() - t0);
    } catch(_) {
      times.push(5000); // treat timeout/failure as worst-case
    }
    if (i === 0) await sleep(300); // small gap between probes
  }
  const avg = Math.round(times.reduce((a,b) => a+b,0) / times.length);
  const good = avg < _NET_BAD_MS;
  console.log(`[OctoProbe] Network RTT: ${times.map(t=>Math.round(t)).join(", ")}ms — avg ${avg}ms — ${good ? "OK" : "POOR"}`);
  setBadge(`Network: ${avg}ms ${good ? "OK" : "POOR"}`, good ? "#00d4aa" : "#ff6b6b");
  await sleep(600); // brief pause so the badge is readable
  return {avg, good};
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
  const path = location.pathname;

  if (/Questionario/i.test(path)) {
    // Always return "questionnaire" — the form loads via AJAX after DOMContentLoaded
    // (observed 15 s delay in production). visaStepQuestionnaire waitFor handles it.
    // If session is truly lost the server redirects to auth before this code runs.
    return "questionnaire";
  }
  if (/Formulario/i.test(path)) {
    return document.querySelector("form[name='vistoForm'], #vistoForm") ? "form" : "session-lost";
  }
  if (/Schedule|Agendamento|ScheduleController/i.test(path)) {
    return "schedule";
  }
  if (/Authentication|authentication/i.test(path + location.search)) {
    return "auth";
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
  await switchToEnglish();
  await sleep(400 + Math.random() * 400);

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
// Visa workflow — login step (runs on auth page)
// ---------------------------------------------------------------------------

async function visaStepAuth(creds) {
  const net = await checkNetworkQuality();
  if (!net.good) { await _clearWorkflowFailed(`Network too slow (${net.avg}ms)`); return; }

  setBadge("Visa: waiting for login form…", "#9060cc");
  const userEl = await waitFor(() => findBySelectors(USERNAME_SELECTORS), 10000);
  if (!userEl) {
    const state = _detectPageState();
    console.warn(`[OctoProbe] Auth: login form not found, detected state=${state}`);
    if (state === "logged-in")     { await visaStepGoToQuestionnaire(); return; }
    if (state === "questionnaire") { await visaStepQuestionnaire();     return; }
    await _clearWorkflowFailed("Visa: login form not found");
    return;
  }

  await humanType(userEl, creds.username);
  await sleep(400 + Math.random() * 400);
  const passEl = await waitFor(() => findBySelectors(PASSWORD_SELECTORS), 4000);
  if (!passEl) { await _clearWorkflowFailed("Visa login: password field not found"); return; }
  await humanType(passEl, creds.password);
  await sleep(400 + Math.random() * 400);

  const submitBtn = findBySelectors(LOGIN_SUBMIT_SELECTORS);
  if (!submitBtn) { await _clearWorkflowFailed("Visa login: submit not found"); return; }

  // XHR intercept — set up once before the retry loop, logs every POST body.
  sendBgMessage({type: "exec-page-script", code: `
    if (!window._octoXhrLog) {
      window._octoXhrLog = true;
      const _open = XMLHttpRequest.prototype.open;
      XMLHttpRequest.prototype.open = function(m, url, ...a) { this._octoUrl = url; return _open.call(this, m, url, ...a); };
      const _send = XMLHttpRequest.prototype.send;
      XMLHttpRequest.prototype.send = function(body) {
        if (typeof body === 'string' && body.length > 0)
          document.dispatchEvent(new CustomEvent('octo-xhr-log', {detail:{url: this._octoUrl, body: body.slice(0,600)}}));
        return _send.call(this, body);
      };
    }
  `}).catch(() => null);
  document.addEventListener('octo-xhr-log', e => {
    console.log('[OctoProbe XHR send]', e.detail.url, '|', e.detail.body);
  }, {capture: true, once: false});

  // Capture any server-side alert fired by the site after the login AJAX response.
  // A server alert means the server explicitly rejected the request (wrong credentials,
  // captcha score, account issue). Retrying blindly risks blocking the account.
  let _visaServerAlert = null;
  document.addEventListener("octo-alert", (e) => {
    _visaServerAlert = e.detail.msg;
    console.log(`[OctoProbe] Visa login alert: "${_visaServerAlert}"`);
  }, {once: false});

  // Retry loop — up to 3 attempts on the same page without navigating away.
  // This mirrors human behaviour: solve captcha, submit, if rejected solve again.
  const MAX_ATTEMPTS = 3;
  for (let attempt = 1; attempt <= MAX_ATTEMPTS; attempt++) {
    const rcEl = await waitFor(
      () => document.querySelector("[data-sitekey], iframe[src*='recaptcha'], .g-recaptcha"),
      8000
    );
    if (rcEl) {
      setBadge(`Visa: solving reCAPTCHA (${attempt}/${MAX_ATTEMPTS})…`, "#9060cc");
      const solved = await waitForRecaptcha();
      if (!solved) { await _clearWorkflowFailed("Visa login: reCAPTCHA failed"); return; }
      await injectAlertCapture();
      const r = rcEl.getBoundingClientRect();
      window._mX = Math.round(r.left + r.width * 0.5);
      window._mY = Math.round(r.top + r.height * 0.5);
      await sleep(2000 + Math.random() * 2000);
    } else {
      await injectAlertCapture();
      await sleep(800 + Math.random() * 600);
    }

    setBadge(`Visa: logging in (${attempt}/${MAX_ATTEMPTS})…`, "#9060cc");
    await storageSet({"login-pending": true});
    await humanClick(submitBtn);

    // RGPD consent popup — only on the first login of a new session.
    // Use a shorter timeout on retries since it won't reappear once accepted.
    const rgpdEl = await waitFor(() => {
      const el = document.querySelector("#loginMsg");
      return isVisible(el) ? el : null;
    }, attempt === 1 ? 12000 : 3000);
    if (rgpdEl) {
      setBadge("Visa: accepting RGPD…", "#9060cc");
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

    // doLogin() is jQuery AJAX — success: server 302 → jQuery follows → portal HTML →
    // AjaxSucceeded (JSON.parse throws) — page URL stays on auth but session cookie IS set.
    // Probe the session silently via fetch so we don't navigate the browser.
    setBadge("Visa: waiting for login response…", "#9060cc");
    await sleep(5000);

    // If the page navigated away on its own, the content script was destroyed; still
    // alive here means the browser is still on Authentication.jsp.
    if (!/Authentication|authentication/i.test(location.pathname + location.search)) return;

    // Server-side rejection (wrong credentials, captcha score, account issue).
    // The alert was already shown to the user via the status badge; stop immediately —
    // further retries risk account blocking.
    if (_visaServerAlert) {
      await storageSet({"login-pending": false});
      setBadge("Visa: server rejected login", "#ff6b6b");
      await _clearWorkflowFailed(`Visa login: server error — "${_visaServerAlert}"`);
      return;
    }

    // Silent session probe — fetch /Questionario without navigating the browser.
    // Server redirects to auth if session is invalid; stays on Questionario if valid.
    setBadge("Visa: probing session…", "#9060cc");
    let sessionOk = false;
    try {
      const probe = await fetch("/VistosOnline/Questionario", {
        method: "GET", credentials: "include", redirect: "follow", cache: "no-store",
      });
      sessionOk = probe.ok && !/Authentication|authentication/i.test(probe.url);
      console.log(`[OctoProbe] Session probe (attempt ${attempt}): ${probe.url} — ${sessionOk ? "OK" : "FAIL"}`);
    } catch(e) {
      console.warn("[OctoProbe] Session probe error:", e);
    }

    if (sessionOk) {
      await storageSet({"login-pending": false});
      setBadge("Visa: logged in → questionnaire…", "#9060cc");
      location.href = "/VistosOnline/Questionario";
      return;
    }

    await storageSet({"login-pending": false});
    if (attempt >= MAX_ATTEMPTS) break;

    console.log(`[OctoProbe] Login attempt ${attempt} failed — re-solving reCAPTCHA`);
    setBadge(`Visa: retry ${attempt + 1}/${MAX_ATTEMPTS} — re-solving captcha…`, "#9060cc");
    // Give the reCAPTCHA widget a moment to reset before the next solve.
    await sleep(3000 + Math.random() * 2000);
  }

  await _clearWorkflowFailed("Visa login: failed after 3 attempts");
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
  const questForm = await waitFor(() => document.querySelector("#questForm"), 30000);
  if (!questForm) {
    // Form never appeared — session may be invalid even though URL is /Questionario.
    // Navigate to auth so the dispatch loop re-runs login from scratch.
    console.warn("[OctoProbe] Questionnaire form not found — redirecting to auth");
    setBadge("Visa: session lost → re-auth…", "#9060cc");
    location.href = "/VistosOnline/Authentication.jsp";
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
    if (!firstQ) { await _clearWorkflowFailed("Visa: Q21 not appeared after goNext(1,'CPV')"); return; }
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
  if (!continueBtn) { await _clearWorkflowFailed("Visa: Form button did not appear"); return; }

  await storageSet({"workflow-step": "form"});
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

  if (el.tagName === "SELECT") {
    await humanSelect(el, value);
    el.dispatchEvent(new Event("change", {bubbles: true}));
  } else {
    const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")?.set;
    if (setter) setter.call(el, value); else el.value = value;
    el.dispatchEvent(new Event("input",  {bubbles: true}));
    el.dispatchEvent(new Event("change", {bubbles: true}));
  }
  return true;
}

// Switch to a form tab by calling mudarTab in MAIN world.
async function _switchToTab(prevTab, nextTab) {
  await _callPageFn(`if(typeof mudarTab==="function") mudarTab(${prevTab},${nextTab});`);
  await sleep(800 + Math.random() * 500);
}

async function visaStepForm() {
  setBadge("Visa: waiting for form…", "#9060cc");

  // Wait for the form element.
  const form = await waitFor(() => document.querySelector("form[name='vistoForm'], #vistoForm, form"), 20000);
  if (!form) { await _clearWorkflowFailed("Visa: form not found"); return; }

  // ── Storage: pull person data, visa config ───────────────────────────────
  const {
    "real-person-input":   rp,
    "visa-arrival-date":   arrivalDate,
    "visa-consular-post":  storedPostoId,
  } = await storageGet(["real-person-input","visa-arrival-date","visa-consular-post"]);

  const person = rp ?? {};

  // Gender mapping.
  const genderVal = (person.gender ?? "F").toUpperCase() === "M" ? "MALE" : "FEMALE";
  const postoId   = storedPostoId ?? "5088";

  // ── Auto-generate passport dates if not supplied ──────────────────────────
  // Field 14: issue date — random day 3–7 years before today.
  const _today = new Date();
  const _fmt   = d => `${d.getFullYear()}/${String(d.getMonth()+1).padStart(2,"0")}/${String(d.getDate()).padStart(2,"0")}`;
  let passportIssueDate = person.passportIssue;
  if (!passportIssueDate) {
    const d = new Date(_today);
    d.setFullYear(d.getFullYear() - (3 + Math.floor(Math.random() * 5)));
    d.setMonth(Math.floor(Math.random() * 12));
    d.setDate(1 + Math.floor(Math.random() * 28));
    passportIssueDate = _fmt(d);
  }
  // Field 15: valid until — issue date + 10 years.
  let passportExpiryDate = person.passportExpiry;
  if (!passportExpiryDate) {
    const d = new Date(passportIssueDate.replace(/\//g, "-"));
    d.setFullYear(d.getFullYear() + 10);
    passportExpiryDate = _fmt(d);
  }

  // ── Arrival / departure dates ─────────────────────────────────────────────
  // Field 27: arrival — use stored value or pick 3–6 weeks from today.
  let computedArrivalDate = arrivalDate;
  if (!computedArrivalDate) {
    const d = new Date(_today);
    d.setDate(d.getDate() + (3 + Math.floor(Math.random() * 4)) * 7);
    computedArrivalDate = _fmt(d);
  }
  // Field 28: departure — arrival + 7 days.
  const _depDate = new Date(computedArrivalDate.replace(/\//g, "-"));
  _depDate.setDate(_depDate.getDate() + 7);
  const computedDepartureDate = _fmt(_depDate);

  // ── Consular post — top-level select above the tab strip ─────────────────
  setBadge("Visa: setting consular post…", "#9060cc");
  await sleep(400 + Math.random() * 300);
  await _setField("cmbPostoConsular", postoId);
  await sleep(400 + Math.random() * 300);

  // ── TAB 1: Personal data ──────────────────────────────────────────────────
  setBadge("Visa: Tab 1 — personal…", "#9060cc");
  await sleep(600 + Math.random() * 400);

  const tab1Fields = [
    ["f1",    person.lastName  ?? ""],   // surname
    ["f2",    "+"],                      // surname(s) at birth — same as current (field 2)
    ["f3",    person.firstName ?? ""],   // first name
    ["f4",    person.dob       ?? ""],   // date of birth
    ["f6sf1", "CPV"],                    // country of birth
    ["f6sf2", "+"],                      // place of birth — same value (field 5)
    ["f7sf1", "CPV"],                    // current nationality
    ["f8",    "CPV"],                    // original nationality
    ["f9",    genderVal],                // gender
    ["f10",   "1"],                      // marital status (single)
  ];
  for (const [name, val] of tab1Fields) {
    if (val) await _setField(name, val);
    await sleep(150 + Math.random() * 200);
  }

  // ── TAB 2: Travel document ────────────────────────────────────────────────
  await _switchToTab(1, 2);
  setBadge("Visa: Tab 2 — travel doc…", "#9060cc");

  const tab2Fields = [
    ["f13", "01"],                  // type of passport (ordinary)
    ["f14", person.traveldoc ?? ""],// passport number
    ["f15", "CPV"],                 // issued by
    ["f16", passportIssueDate],     // date of issue — auto or stored (field 14)
    ["f17", passportExpiryDate],    // valid until — issue + 10 yrs (field 15)
    ["f5",  ""],                    // ID number (optional)
  ];
  for (const [name, val] of tab2Fields) {
    if (val) await _setField(name, val);
    await sleep(150 + Math.random() * 200);
  }

  // ── TAB 3: Travel info ────────────────────────────────────────────────────
  await _switchToTab(2, 3);
  setBadge("Visa: Tab 3 — travel info…", "#9060cc");

  const tab3Fields = [
    ["f29",       "10"],           // purpose: tourism
    ["txtInfoMotEstada", "holiday"],
    ["em_destino_1", "PRT"],       // destination state
    ["f32",       "PRT"],          // transit route
    ["f24",       "1"],            // single entry
    ["f25",       "7"],            // duration of stay (days)
    ["f30",       computedArrivalDate],    // arrival — stored or 3–6 wks from today (field 27)
    ["f31",       computedDepartureDate], // departure — arrival + 7 days
  ];
  for (const [name, val] of tab3Fields) {
    if (val) await _setField(name, val);
    await sleep(150 + Math.random() * 200);
  }

  // ── TAB 4: Occupation / residence ────────────────────────────────────────
  await _switchToTab(3, 4);
  setBadge("Visa: Tab 4 — occupation…", "#9060cc");

  const tab4Fields = [
    ["f19",  "05"],   // student
    ["f20sf1", "SCHOOL"],
    ["f20sf2", "CPV"],
    ["f45",  "PRAIA"],
    ["f46",  "0"],
    ["cmbImpressoesDigitais", "N"],
    ["f27",  "N"],    // no entry permit
    ["f18sf1", ""],
    ["f18sf2", ""],
    ["f18sf3", ""],
  ];
  for (const [name, val] of tab4Fields) {
    if (val) await _setField(name, val);
    await sleep(150 + Math.random() * 200);
  }

  // ── TAB 5: Reference + costs ──────────────────────────────────────────────
  await _switchToTab(4, 5);
  setBadge("Visa: Tab 5 — reference…", "#9060cc");

  const tab5Fields = [
    ["cmbReferencia", "individual"],
    ["f34",    "HOTEL LISBOA"],
    ["f34sf2", "LISBOA, PORTUGAL"],
    ["f34sf3", ""],
    ["f34sf4", ""],
    ["f34sf5", "1"],
    ["cmbDespesasRequerente_1", "1"],
    ["cmbDespesasPatrocinador_1", "1"],
  ];
  for (const [name, val] of tab5Fields) {
    if (val) await _setField(name, val);
    await sleep(150 + Math.random() * 200);
  }

  // ── TAB 6: Attachments — SKIP ─────────────────────────────────────────────
  // Tab 6 is skipped entirely per specification.

  // ── Submit the form ───────────────────────────────────────────────────────
  setBadge("Visa: submitting form…", "#9060cc");
  await injectAlertCapture(true); // confirm/prompt safe here — captcha already solved

  // Click the last "Next" / submit button to advance from tab 5 and submit.
  // The form page uses a single submit button that becomes active after tab 5.
  await _callPageFn(`
    (function(){
      // Try mudarTab to tab 6 to trigger server validation, then submit.
      if(typeof mudarTab==="function") mudarTab(5,6);
    })();
  `);
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
  if (!formSubmitBtn) { await _clearWorkflowFailed("Visa: form submit button not found"); return; }

  await sleep(800 + Math.random() * 600);
  await humanClick(formSubmitBtn);

  // Capture any alert (server-side validation errors).
  const alertMsg = await new Promise(resolve => {
    const tid = setTimeout(() => resolve(null), 8000);
    document.addEventListener("octo-alert", (e) => {
      clearTimeout(tid);
      resolve(e.detail.msg);
    }, {once: true});
  });
  if (alertMsg) {
    console.warn("[OctoProbe] Form submit alert:", alertMsg);
  }

  await storageSet({"workflow-step": "schedule"});
  setBadge("Visa: form submitted…", "#9060cc");
}

// ---------------------------------------------------------------------------
// Visa workflow — step: schedule appointment
// ---------------------------------------------------------------------------

async function visaStepSchedule() {
  setBadge("Visa: schedule — waiting for page…", "#9060cc");

  const {"visa-consular-post": postoId, "visa-arrival-date": arrivalDate} =
    await storageGet(["visa-consular-post","visa-arrival-date"]);
  const POSTO = postoId ?? "5088";

  // Wait for captcha div to appear.
  const captchaDiv = await waitFor(
    () => {
      const el = document.querySelector("#captchaDiv, [id*='captcha' i], iframe[src*='recaptcha']");
      return el && isVisible(el) ? el : null;
    },
    20000
  );
  if (!captchaDiv) { await _clearWorkflowFailed("Visa: schedule captcha not found"); return; }

  // Solve reCAPTCHA for the schedule page.
  setBadge("Visa: solving schedule reCAPTCHA…", "#9060cc");
  const SCHEDULE_SITEKEY = "6LdOB9crAAAAADT4RFruc5sPmzLKIgvJVfL830d4";
  const solveStart = Date.now();

  const {"captcha-mode": captchaMode} = await storageGet("captcha-mode");
  let captchaToken = null;

  if (captchaMode === "human") {
    const solved = await waitForRecaptcha();
    if (!solved) { await _clearWorkflowFailed("Visa: schedule reCAPTCHA not solved"); return; }
    // Extract token from the hidden textarea.
    const ta = document.querySelector("#g-recaptcha-response-1, #g-recaptcha-response");
    captchaToken = ta?.value ?? null;
  } else {
    const scheduleAction = document.querySelector("[data-sitekey][data-action]")?.dataset?.action
                        ?? document.querySelector("[data-action]")?.dataset?.action
                        ?? null;
    const result = await sendBgMessage({
      type: "solve-recaptcha-api",
      pageUrl: location.href,
      siteKey: SCHEDULE_SITEKEY,
      action: scheduleAction,
    }).catch(() => null);
    if (!result?.ok || !result.token) {
      await _clearWorkflowFailed("Visa: schedule reCAPTCHA API failed");
      return;
    }
    const elapsed = Date.now() - solveStart;
    if (elapsed < 8000) await sleep(8000 - elapsed);
    captchaToken = result.token;
    // Inject token into page fields.
    await sendBgMessage({type: "inject-recaptcha-token", token: captchaToken}).catch(() => null);
  }

  if (!captchaToken) { await _clearWorkflowFailed("Visa: no captcha token obtained"); return; }

  // Fetch slots directly (bypasses window.data MAIN world issue).
  setBadge("Visa: fetching slots…", "#9060cc");
  let slotsData = null;
  try {
    const resp = await fetch(`/VistosOnline/slots?posto_id=${POSTO}`, {
      method: "POST",
      headers: {"Content-Type": "application/x-www-form-urlencoded"},
      body: `captcha=${encodeURIComponent(captchaToken)}`,
      credentials: "include",
    });
    slotsData = await resp.json();
  } catch (e) {
    console.error("[OctoProbe] Slots fetch error:", e);
    await _clearWorkflowFailed("Visa: slots API failed");
    return;
  }

  console.log("[OctoProbe] Slots response:", JSON.stringify(slotsData).slice(0, 500));

  // Inject window.data so MAIN world functions (ajaxFunctionPeriodos) work.
  await _callPageFn(`window.data = ${JSON.stringify(slotsData)};`);

  // Parse available dates and pick earliest valid slot.
  // Format: slotsData is array of "yyyy/mm/dd" strings OR an object with date keys.
  let availableDates = [];
  if (Array.isArray(slotsData)) {
    availableDates = slotsData;
  } else if (slotsData && typeof slotsData === "object") {
    availableDates = Object.keys(slotsData);
  }

  const today = new Date();
  today.setHours(0,0,0,0);
  const maxDate = arrivalDate ? new Date(arrivalDate.replace(/\//g,"-")) : null;
  if (maxDate) maxDate.setHours(0,0,0,0);

  // Filter: after today, before arrival date (or within 90 days), and within a few weeks.
  const cutoff = new Date(today);
  cutoff.setDate(cutoff.getDate() + 90);

  const validDates = availableDates
    .map(d => d.trim())
    .filter(d => /^\d{4}\/\d{2}\/\d{2}$/.test(d))
    .filter(d => {
      const dt = new Date(d.replace(/\//g, "-"));
      dt.setHours(0,0,0,0);
      if (dt <= today) return false;
      if (maxDate && dt >= maxDate) return false;
      if (dt > cutoff) return false;
      return true;
    })
    .sort();

  console.log("[OctoProbe] Valid slot dates:", validDates);

  if (!validDates.length) {
    await _clearWorkflowFailed("Visa: no valid slots available");
    return;
  }

  const chosenDate = validDates[0]; // earliest
  setBadge(`Visa: slot ${chosenDate}…`, "#9060cc");

  // Set the date in the readonly date input via MAIN world injection.
  await _callPageFn(`
    (function(){
      var inp = document.getElementById("f_date_c");
      if(!inp) return;
      var nativeSetter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,"value")?.set;
      if(nativeSetter) nativeSetter.call(inp,"${chosenDate}"); else inp.value="${chosenDate}";
      inp.removeAttribute("readonly");
      inp.dispatchEvent(new Event("input",{bubbles:true}));
      inp.dispatchEvent(new Event("change",{bubbles:true}));
      inp.setAttribute("readonly","1");
      if(typeof ajaxFunctionPeriodos==="function") ajaxFunctionPeriodos("${chosenDate}");
    })();
  `);

  // Wait for period dropdown to populate.
  setBadge("Visa: waiting for time slots…", "#9060cc");
  const periodSel = await waitFor(() => {
    const el = document.querySelector("#inputPeriodos");
    return el && el.options.length > 1 ? el : null;
  }, 15000);

  if (!periodSel) { await _clearWorkflowFailed("Visa: no period options loaded"); return; }

  // Pick first valid period option.
  const firstPeriod = [...periodSel.options].find(o => o.value && o.value.trim() !== "");
  if (!firstPeriod) { await _clearWorkflowFailed("Visa: no valid period option"); return; }

  await humanSelect(periodSel, firstPeriod.value);
  periodSel.dispatchEvent(new Event("change", {bubbles: true}));
  await sleep(500 + Math.random() * 400);

  // Override submitSlotsForm in MAIN world BEFORE clicking submit — the function uses
  // document.write() which would destroy the page context.
  await _callPageFn(`
    (function(){
      window._octoOrigSubmit = window.submitSlotsForm;
      window.submitSlotsForm = async function() {
        var form = document.vistoForm || document.querySelector("form");
        if(!form) { window.postMessage({type:"octo-pdf-error",error:"no form"},"*"); return; }
        var data = new FormData(form);
        var body = new URLSearchParams(data).toString();
        try {
          var resp = await fetch("/VistosOnline/SubmeterVistoCriaPDF?posto_id=${POSTO}", {
            method:"POST", body:body,
            headers:{"Content-Type":"application/x-www-form-urlencoded"},
            credentials:"include"
          });
          var html = await resp.text();
          window.postMessage({type:"octo-pdf-html", html:html}, "*");
        } catch(e) {
          window.postMessage({type:"octo-pdf-error", error:String(e)}, "*");
        }
      };
      console.log("[OctoProbe] submitSlotsForm overridden");
    })();
  `);
  await sleep(300);

  // Click the schedule submit button.
  setBadge("Visa: submitting schedule…", "#9060cc");
  const schedSubmit = document.querySelector("#btnSubmit");
  if (!schedSubmit) { await _clearWorkflowFailed("Visa: schedule submit button not found"); return; }
  await humanClick(schedSubmit);

  // Wait for pre-visto confirmation popup.
  const previstoPopup = await waitFor(() => {
    const el = document.querySelector("#previstoMsg");
    return el && isVisible(el) ? el : null;
  }, 15000);

  if (previstoPopup) {
    setBadge("Visa: confirming appointment…", "#9060cc");
    await sleep(800 + Math.random() * 600);
    const previstoSubmit = document.querySelector("#previstoSubmit");
    if (previstoSubmit) await humanClick(previstoSubmit);
  }

  // Intercept the PDF response posted by our overridden submitSlotsForm.
  setBadge("Visa: waiting for PDF…", "#9060cc");
  const pdfMsg = await _waitPageMessage("octo-pdf-html", 45000);

  if (!pdfMsg?.html) {
    const errMsg = pdfMsg?.error ?? "timeout";
    await _clearWorkflowFailed(`Visa: PDF failed — ${errMsg}`);
    return;
  }

  // Download the PDF: extract it from the HTML blob response.
  // The server returns raw PDF bytes embedded in the page via document.write() — the
  // response Content-Type is application/pdf but we got the HTML wrapper.
  // Instead, re-fetch the URL with same body to get the binary PDF.
  setBadge("Visa: downloading PDF…", "#9060cc");
  try {
    const form = document.querySelector("form[name='vistoForm'], form");
    const formData = form ? new URLSearchParams(new FormData(form)).toString() : "";
    const pdfResp = await fetch(`/VistosOnline/SubmeterVistoCriaPDF?posto_id=${POSTO}`, {
      method: "POST",
      headers: {"Content-Type": "application/x-www-form-urlencoded"},
      body: formData,
      credentials: "include",
    });
    const contentType = pdfResp.headers.get("content-type") ?? "";
    if (contentType.includes("pdf")) {
      const blob = await pdfResp.blob();
      const url  = URL.createObjectURL(blob);
      const a    = document.createElement("a");
      a.href     = url;
      a.download = `visa_appointment_${chosenDate.replace(/\//g,"-")}.pdf`;
      document.body.appendChild(a);
      a.click();
      setTimeout(() => { URL.revokeObjectURL(url); a.remove(); }, 5000);
      setBadge("Visa: PDF downloaded!", "#00d4aa");
    } else {
      console.warn("[OctoProbe] PDF response not PDF content-type:", contentType);
      setBadge("Visa: PDF response received (check downloads)", "#00d4aa");
    }
  } catch (e) {
    console.error("[OctoProbe] PDF download error:", e);
    setBadge("Visa: PDF fetch error", "#ff6b6b");
  }

  // Workflow complete.
  await storageSet({"workflow-type": null, "workflow-step": null, "login-pending": false});
  console.log("[OctoProbe] Visa workflow complete");
}

// ---------------------------------------------------------------------------
// Main entry point
// ---------------------------------------------------------------------------

(async function main() {
  if (!document.body) {
    await new Promise(r => document.addEventListener("DOMContentLoaded", r));
  }

  injectBadge();

  // Warmup workflow runs on any domain to build reCAPTCHA Enterprise trust signals.
  const {"workflow-type": earlyWfType} = await storageGet("workflow-type");
  if (earlyWfType === "warmup") {
    await warmupStep();
    return;
  }

  if (location.hostname !== TARGET_HOST) return;

  // Detect the WAF bot-challenge page (/ch/bd.js injected into an otherwise empty body).
  // bd.js checks window.alert.toString() for native code — any patch we inject breaks it.
  // Let the challenge run completely untouched; the browser will navigate once it passes.
  if (document.querySelector('script[src*="/ch/bd.js"]')) {
    // Count consecutive challenge pages without a real page in between.
    // Normal: 1 challenge → bd.js passes → real page (counter resets).
    // IP-blocked: challenge loops ≥ 3 times without ever reaching a real page.
    const {"challenge-count": prevCount = 0} = await storageGet("challenge-count");
    const challengeCount = prevCount + 1;
    await storageSet({"challenge-count": challengeCount});
    console.log(`[OctoProbe] Bot challenge page #${challengeCount} in sequence`);

    if (challengeCount >= 3) {
      // Hard block — stop the workflow and tell the user to switch proxy/IP.
      console.warn("[OctoProbe] IP appears blocked — challenge looped 3+ times without passing");
      await storageSet({"workflow-type": null, "workflow-step": null,
                        "login-pending": false, "challenge-count": 0});
      setBadge("IP BLOCKED — switch proxy", "#ff0000");
      return;
    }

    setBadge(`Bot challenge — waiting… (${challengeCount}/3)`, "#888888");
    return;
  }

  // Reached a real page — reset the consecutive-challenge counter.
  await storageSet({"challenge-count": 0});

  // Dismiss cookie consent banner before anything else — it blocks clicks when visible.
  await dismissCookieConsent();

  const {
    "workflow-type": wfType,
    "workflow-step": wfStep,
    "register-person": person,
    "register-email": emailAcct,
    "username": storedUsername,
    "password": storedPassword,
    "login-pending": loginPending,
    "register-post-submitted": regPostSubmitted,
  } = await storageGet(["workflow-type","workflow-step","register-person","register-email","username","password","login-pending","register-post-submitted"]);

  console.log(`[OctoProbe] workflow=${wfType} step=${wfStep} url=${location.href}`);

  // Pick up any pending account stored before token verification submit.
  // Fires on Authentication.jsp (or any page) after successful email verification navigates here.
  const {"pending-account": pendingAccount} = await storageGet("pending-account");
  if (pendingAccount && location.hostname === TARGET_HOST) {
    console.log("[OctoProbe] Found pending account — saving:", pendingAccount.username);
    const saveResult = await sendBgMessage({type: "save-account", account: pendingAccount}).catch(() => null);
    if (saveResult?.ok) {
      await new Promise(res => chrome.storage.local.remove("pending-account", res));
      chrome.runtime.sendMessage({type: "stop-email-poll"});
      console.log("[OctoProbe] Account download triggered:", saveResult.filename);
      const {"register-chain": regChain = "none"} = await storageGet("register-chain");
      if (regChain === "visa") {
        await storageSet({
          "username":      pendingAccount.username,
          "password":      pendingAccount.password,
          "workflow-type": "visa",
          "workflow-step": null,
          "email-token":   null,
        });
        setBadge("Account saved — logging in…", "#00d4aa");
        location.href = "/VistosOnline/";
      } else {
        await storageSet({"workflow-type": null, "workflow-step": null, "email-token": null});
        setBadge("Account saved!", "#00d4aa");
      }
    } else {
      console.warn("[OctoProbe] save-account failed:", saveResult);
      setBadge("Account save failed", "#ff6b6b");
    }
    return;
  }

  // Login success/retry detection — fires on pages reached after form submit navigation.
  if (loginPending && wfType === "login") {
    const isAuthPage  = /Authentication|authentication/i.test(location.pathname + location.search);
    const isLoginPage = /\/login\b/i.test(location.pathname); // bd.js replayed the POST here

    if (isLoginPage) {
      // Check for secblock — IP/account hard-blocked by the server.
      const bodyText = document.body?.innerText || "";
      if (/secblock|exceeded.*limit|user blocked/i.test(bodyText)) {
        console.warn("[OctoProbe] Login: secblock detected — IP or account is blocked");
        await storageSet({"login-pending": false, "login-form-attempt": 0});
        await _clearWorkflowFailed("IP/account BLOCKED — switch proxy");
        return;
      }
      // bd.js passed the challenge and replayed the POST with the dummy token, which the
      // server rejected. Challenge cookie is now set — navigate back to auth for phase 2.
      console.log("[OctoProbe] Login: bd.js challenge passed (dummy token) — proceeding to phase 2");
      setBadge("Login: re-solving captcha…", "#f0c040");
      location.href = "/VistosOnline/Authentication.jsp";
      return;
    }

    if (!isAuthPage) {
      // We navigated somewhere other than auth or the login endpoint — success.
      await storageSet({"workflow-type": null, "workflow-step": null, "login-pending": false, "login-form-attempt": 0});
      setBadge("Logged in!", "#00d4aa");
      console.log("[OctoProbe] Login successful — workflow complete");
      return;
    }
  }

  if (wfType === "register") {
    const isTokenPage = location.search.includes("token=");
    const isAuthPage  = /Authentication|authentication/i.test(location.pathname + location.search);
    const isLoginPage = /\/login\b/i.test(location.pathname);

    if (isLoginPage) {
      // bd.js replayed the Phase-0 register POST to /login — challenge cookie now set.
      location.href = "/VistosOnline/Authentication.jsp";
      return;
    }

    if (regPostSubmitted) {
      // Phase-1 real form POST completed — server navigated us somewhere unknown.
      // Clear flag and poll for the verification email regardless of landing URL.
      await storageSet({"register-post-submitted": false});
      setBadge("Register: waiting for verification email…", "#f0c040");
      const token = await waitForEmailToken(emailAcct?.jwt, 180000);
      if (!token) {
        await _clearWorkflowFailed("Register: no verification email received");
        return;
      }
      location.href = `/VistosOnline/Authentication.jsp?token=${token}`;
      return;
    }

    if (isTokenPage) {
      await registerStepToken(emailAcct);
    } else if (isAuthPage && wfStep === "auth") {
      await registerStepAuth(person, emailAcct);
    } else if (!isAuthPage && wfStep === "home") {
      await registerStepHome();
    } else {
      // Unexpected state — show info and do nothing
      setBadge(`Register: ${wfStep ?? "?"}`, "#888");
    }

  } else if (wfType === "login") {
    const isAuthPage = /Authentication|authentication/i.test(location.pathname + location.search);
    if (wfStep === "auth" && isAuthPage) {
      await loginStepAuth({username: storedUsername, password: storedPassword});
    } else if (wfStep === "home") {
      await loginStepHome();
    } else {
      setBadge(`Login: ${wfStep ?? "?"}`, "#888");
    }

  } else if (wfType === "visa") {
    // Route purely by current page state — no storage step dependency.
    // _detectPageState() inspects URL + DOM, so it's always accurate after navigation.
    const pageState = _detectPageState();
    console.log(`[OctoProbe] Visa dispatch: state=${pageState} url=${location.href}`);

    switch (pageState) {
      case "not-logged-in":
        // Reached the site but not authenticated — switch language then go to login.
        await switchToEnglish();
        await clickLoginLink();
        break;
      case "auth":
        await visaStepAuth({username: storedUsername, password: storedPassword});
        break;
      case "logged-in":
        // Authenticated home/dashboard — navigate to questionnaire.
        await visaStepGoToQuestionnaire();
        break;
      case "questionnaire":
        await visaStepQuestionnaire();
        break;
      case "form":
        await visaStepForm();
        break;
      case "schedule":
        await visaStepSchedule();
        break;
      case "session-lost":
        // URL implies a workflow page but required DOM is missing — session expired.
        // Return to home so _detectPageState can re-evaluate and log in if needed.
        console.warn("[OctoProbe] Visa: session-lost — navigating home to recover");
        location.href = "/VistosOnline/";
        break;
      default:
        setBadge("Visa: unknown page state", "#888");
        console.warn("[OctoProbe] Visa: unrecognised page — no action");
    }

  } else {
    // Legacy / no workflow set — do nothing extra
    setBadge(`Octo Probe v${chrome.runtime.getManifest().version}`, "#00d4aa");
  }
})();
