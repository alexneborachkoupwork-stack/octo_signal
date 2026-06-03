// ── DOM refs ──────────────────────────────────────────────────────────────────

const userEl           = document.getElementById("inp-user");
const passEl           = document.getElementById("inp-pass");
const btnLoginIdle     = document.getElementById("btn-login-idle");
const btnLoginApply    = document.getElementById("btn-login-apply");

const rpFirstnameEl    = document.getElementById("inp-rp-firstname");
const rpLastnameEl     = document.getElementById("inp-rp-lastname");
const rpDobEl          = document.getElementById("inp-rp-dob");
const rpGenderEl       = document.getElementById("sel-rp-gender");
const rpTraveldocEl    = document.getElementById("inp-rp-traveldoc");
const btnRpGenerate    = document.getElementById("btn-rp-generate");
const btnRpValidate    = document.getElementById("btn-rp-validate");

const selNationality   = document.getElementById("sel-nationality");
const visaPostoEl      = document.getElementById("inp-visa-posto");
const btnTriggerAuto   = document.getElementById("btn-trigger-auto");
const btnTriggerSignal = document.getElementById("btn-trigger-signal");
const btnRegisterIdle  = document.getElementById("btn-register-idle");
const btnRegisterLogin = document.getElementById("btn-register-login");
const btnRegisterApply = document.getElementById("btn-register-apply");

const selSolver          = document.getElementById("sel-solver");
const proxyHostEl        = document.getElementById("inp-proxy-host");
const proxyPortEl        = document.getElementById("inp-proxy-port");
const btnProxySocks5     = document.getElementById("btn-proxy-socks5");
const btnProxyHttp       = document.getElementById("btn-proxy-http");
const proxyLoginEl       = document.getElementById("inp-proxy-login");
const proxyPassEl        = document.getElementById("inp-proxy-pass");
const btnEmailMailtm     = document.getElementById("btn-email-mailtm");
const btnEmailCf         = document.getElementById("btn-email-cf");
const chkSolverParallel  = document.getElementById("chk-solver-parallel");
const chkPreVisitWarmup  = document.getElementById("chk-pre-visit-warmup");
const chkLangChange      = document.getElementById("chk-lang-change");

const swEl             = document.getElementById("sw-status");
const statusEl         = document.getElementById("status");

// ── Utilities ─────────────────────────────────────────────────────────────────

function setStatus(msg, isError = false) {
  statusEl.textContent = msg;
  statusEl.className   = isError ? "error" : "";
}

function setTriggerMode(mode) {
  const isSignal = mode === "EXTERNAL_SIGNAL";
  btnTriggerAuto.classList.toggle("active",   !isSignal);
  btnTriggerSignal.classList.toggle("active",  isSignal);
  skSet({[SK.TRIGGER_MODE]: mode});
}

function setEmailProvider(provider) {
  const isCf = provider === "cloudflare";
  btnEmailMailtm.classList.toggle("active", !isCf);
  btnEmailCf.classList.toggle("active",      isCf);
  skSet({[SK.EMAIL_PROVIDER]: provider});
}

function toggleSolverParallelism(enabled) {
  chkSolverParallel.checked = enabled;
  skSet({[SK.CAPTCHA_SOLVER_PARALLEL]: enabled});
}

function togglePreVisitWarmup(enabled) {
  chkPreVisitWarmup.checked = enabled;
  skSet({[SK.PRE_VISIT_WARMUP]: enabled});
}

function toggleLangChange(enabled) {
  chkLangChange.checked = enabled;
  skSet({[SK.LANG_SWITCH_ENABLED]: enabled});
}

// ── Load saved state on popup open ───────────────────────────────────────────

skGet(
  SK.USERNAME, SK.PASSWORD, SK.CAPTCHA_SOLVER,
  SK.VISA_CONSULAR_POST, SK.EMAIL_PROVIDER, SK.TRIGGER_MODE,
  SK.REAL_PERSON_INPUT, SK.WORKFLOW_TYPE, SK.WORKFLOW_STEP,
  SK.CAPTCHA_SOLVER_PARALLEL, SK.PRE_VISIT_WARMUP, SK.LANG_SWITCH_ENABLED,
  SK.RUN_STATUS, SK.RUN_ERROR,
  SK.PROXY_HOST, SK.PROXY_PORT, SK.PROXY_TYPE, SK.PROXY_LOGIN, SK.PROXY_PASS,
).then(data => {
  if (data[SK.USERNAME]) userEl.value = data[SK.USERNAME];
  if (data[SK.PASSWORD]) passEl.value = data[SK.PASSWORD];

  if (data[SK.PROXY_HOST])  proxyHostEl.value  = data[SK.PROXY_HOST];
  if (data[SK.PROXY_PORT])  proxyPortEl.value  = String(data[SK.PROXY_PORT]);
  if (data[SK.PROXY_LOGIN]) proxyLoginEl.value = data[SK.PROXY_LOGIN];
  if (data[SK.PROXY_PASS])  proxyPassEl.value  = data[SK.PROXY_PASS];
  _setProxyType(data[SK.PROXY_TYPE] ?? "socks5");

  if (data[SK.VISA_CONSULAR_POST]) {
    visaPostoEl.value = data[SK.VISA_CONSULAR_POST];
  } else {
    skSet({[SK.VISA_CONSULAR_POST]: visaPostoEl.value});
  }

  selSolver.value = data[SK.CAPTCHA_SOLVER] ?? "capsolver";
  setEmailProvider(data[SK.EMAIL_PROVIDER] ?? "mailtm");
  setTriggerMode(data[SK.TRIGGER_MODE] ?? "AUTO_TRIGGER");

  toggleSolverParallelism(data[SK.CAPTCHA_SOLVER_PARALLEL] ?? false);
  togglePreVisitWarmup(data[SK.PRE_VISIT_WARMUP] ?? false);
  toggleLangChange(data[SK.LANG_SWITCH_ENABLED] ?? false);

  const rp = data[SK.REAL_PERSON_INPUT];
  if (rp) {
    if (rp.firstName)   rpFirstnameEl.value = rp.firstName;
    if (rp.lastName)    rpLastnameEl.value  = rp.lastName;
    if (rp.dob)         rpDobEl.value        = rp.dob;
    if (rp.gender)      rpGenderEl.value     = rp.gender;
    if (rp.traveldoc)   rpTraveldocEl.value  = rp.traveldoc;
    if (rp.nationality) selNationality.value = rp.nationality;
  }

  _updateRunStatus(data[SK.RUN_STATUS], data[SK.RUN_ERROR], data[SK.WORKFLOW_TYPE], data[SK.WORKFLOW_STEP]);
});

function _updateRunStatus(runStatus, runError, wfType, wfStep) {
  if (runStatus === "error" && runError) { setStatus(runError, true); return; }
  if (runStatus && runStatus !== "DONE") { setStatus(runStatus); return; }
  if (wfType) { setStatus(`Running: ${wfType} / ${wfStep ?? "…"}`); return; }
}

// Live status updates while popup is open — background writes SK.RUN_STATUS / SK.RUN_ERROR
// via _sendStatusUpdate() and _writeRunError() on every workflow state transition.
chrome.storage.onChanged.addListener((changes, area) => {
  if (area !== "local") return;
  const statusChange = changes[SK.RUN_STATUS];
  const errorChange  = changes[SK.RUN_ERROR];
  if (!statusChange && !errorChange) return;
  const runStatus = statusChange?.newValue ?? null;
  const runError  = errorChange?.newValue  ?? null;
  if (runStatus === "error" && runError) { setStatus(runError, true); return; }
  if (runStatus) setStatus(runStatus);
});

// ── Apply section ─────────────────────────────────────────────────────────────

function saveCredentials(callback) {
  const username = userEl.value.trim();
  const password = passEl.value;
  if (!username || !password) { setStatus("Both fields are required.", true); return; }
  skSet({[SK.USERNAME]: username, [SK.PASSWORD]: password}).then(() => callback(username, password));
}

btnLoginIdle.addEventListener("click", () => {
  saveCredentials((username, password) => {
    chrome.runtime.sendMessage({type: MSG.LOGIN_IDLE, payload: {username, password}, proxy: _readProxy()}, (resp) => {
      if (resp?.ok) { setStatus("Login started."); window.close(); }
      else setStatus(resp?.error ?? "Login failed.", true);
    });
  });
});

btnLoginApply.addEventListener("click", () => {
  saveCredentials((username, password) => {
    chrome.runtime.sendMessage({type: MSG.LOGIN_APPLY, payload: {username, password}, proxy: _readProxy()}, (resp) => {
      if (resp?.ok) { setStatus("Login + Apply started."); window.close(); }
      else setStatus(resp?.error ?? "Login failed.", true);
    });
  });
});

// ── Real person section ───────────────────────────────────────────────────────

async function _saveRpField(key, value) {
  const {[SK.REAL_PERSON_INPUT]: existing = {}} = await skGet(SK.REAL_PERSON_INPUT);
  skSet({[SK.REAL_PERSON_INPUT]: {...existing, [key]: value}});
}

rpFirstnameEl.addEventListener("input",  () => _saveRpField("firstName", rpFirstnameEl.value.trim()));
rpLastnameEl.addEventListener("input",   () => _saveRpField("lastName",  rpLastnameEl.value.trim()));
rpDobEl.addEventListener("input",        () => _saveRpField("dob",       rpDobEl.value.trim()));
rpGenderEl.addEventListener("change",    () => _saveRpField("gender",    rpGenderEl.value));
rpTraveldocEl.addEventListener("input",  () => _saveRpField("traveldoc", rpTraveldocEl.value.trim()));

const REQUIRED_RP_KEYS = ["firstName", "lastName", "dob", "gender", "traveldoc"];

// Returns {ok, rp}: rp=null means no data entered (backend will generate fake person).
async function _resolvePersonPayload() {
  const {[SK.REAL_PERSON_INPUT]: rp} = await skGet(SK.REAL_PERSON_INPUT);
  if (!rp?.firstName) return {ok: true, rp: null}; // no data — backend generates
  const missing = REQUIRED_RP_KEYS.filter(k => !rp[k]);
  if (missing.length) { setStatus(`Incomplete: ${missing.join(", ")}`, true); return {ok: false}; }
  return {ok: true, rp};
}

btnRpGenerate.addEventListener("click", async () => {
  const resp = await chrome.runtime.sendMessage({type: MSG.GENERATE_PERSON});
  if (!resp?.ok) { setStatus("Generate failed.", true); return; }
  const p = resp.person;
  rpFirstnameEl.value = p.name;
  rpLastnameEl.value  = p.surname;
  rpDobEl.value        = p.birth_date;
  rpGenderEl.value     = p.gender;
  rpTraveldocEl.value  = p.traveldoc;
  if (p.nationality) selNationality.value = p.nationality;
  await skSet({[SK.REAL_PERSON_INPUT]: {
    firstName:   p.name,
    lastName:    p.surname,
    dob:         p.birth_date,
    gender:      p.gender,
    traveldoc:   p.traveldoc,
    nationality: p.nationality ?? "CPV",
  }});
  setStatus("Person generated.");
});

btnRpValidate.addEventListener("click", async () => {
  const {ok, rp} = await _resolvePersonPayload();
  if (!ok) return;
  if (rp) setStatus("Person data valid.");
  else setStatus("No person data — backend will generate.", false);
});

// ── Register section ──────────────────────────────────────────────────────────

selNationality.addEventListener("change", async () => {
  const {[SK.REAL_PERSON_INPUT]: existing = {}} = await skGet(SK.REAL_PERSON_INPUT);
  skSet({[SK.REAL_PERSON_INPUT]: {...existing, nationality: selNationality.value}});
});

visaPostoEl.addEventListener("input", () => {
  const v = visaPostoEl.value.trim();
  if (v) skSet({[SK.VISA_CONSULAR_POST]: v});
});

btnTriggerAuto.addEventListener("click",   () => setTriggerMode("AUTO_TRIGGER"));
btnTriggerSignal.addEventListener("click", () => setTriggerMode("EXTERNAL_SIGNAL"));

btnRegisterIdle.addEventListener("click", async () => {
  const {ok, rp} = await _resolvePersonPayload();
  if (!ok) return;
  chrome.runtime.sendMessage({type: MSG.REGISTER_ONLY, realPerson: rp, proxy: _readProxy()}, (resp) => {
    if (resp?.ok) { setStatus("Register started."); window.close(); }
    else setStatus(resp?.error ?? "Register failed.", true);
  });
});

btnRegisterLogin.addEventListener("click", async () => {
  const {ok, rp} = await _resolvePersonPayload();
  if (!ok) return;
  chrome.runtime.sendMessage({type: MSG.REGISTER_LOGIN, realPerson: rp, proxy: _readProxy()}, (resp) => {
    if (resp?.ok) { setStatus("Register + Login started."); window.close(); }
    else setStatus(resp?.error ?? "Register failed.", true);
  });
});

btnRegisterApply.addEventListener("click", async () => {
  const postoId = visaPostoEl.value.trim();
  if (!postoId) { setStatus("Consular Post ID is required.", true); return; }
  const {ok, rp} = await _resolvePersonPayload();
  if (!ok) return;
  await skSet({[SK.VISA_CONSULAR_POST]: postoId});
  chrome.runtime.sendMessage({type: MSG.REGISTER_APPLY, realPerson: rp, proxy: _readProxy()}, (resp) => {
    if (resp?.ok) { setStatus("Register + Apply started."); window.close(); }
    else setStatus(resp?.error ?? "Register failed.", true);
  });
});

// ── Options section ───────────────────────────────────────────────────────────

selSolver.addEventListener("change", () => {
  skSet({[SK.CAPTCHA_SOLVER]: selSolver.value});
});

function _setProxyType(type) {
  const isSocks5 = type !== "http";
  btnProxySocks5.classList.toggle("active",  isSocks5);
  btnProxyHttp.classList.toggle("active",   !isSocks5);
}

function _readProxy() {
  const host = proxyHostEl.value.trim();
  const port = proxyPortEl.value.trim();
  if (!host || !port) return null;
  return {
    type:     btnProxySocks5.classList.contains("active") ? "socks5" : "http",
    host,
    port:     Number(port),
    login:    proxyLoginEl.value.trim(),
    password: proxyPassEl.value,
  };
}

proxyHostEl.addEventListener("input",    () => skSet({[SK.PROXY_HOST]:  proxyHostEl.value.trim()}));
proxyPortEl.addEventListener("input",    () => skSet({[SK.PROXY_PORT]:  Number(proxyPortEl.value.trim()) || 0}));
proxyLoginEl.addEventListener("input",   () => skSet({[SK.PROXY_LOGIN]: proxyLoginEl.value.trim()}));
proxyPassEl.addEventListener("input",    () => skSet({[SK.PROXY_PASS]:  proxyPassEl.value}));
btnProxySocks5.addEventListener("click", () => { _setProxyType("socks5"); skSet({[SK.PROXY_TYPE]: "socks5"}); });
btnProxyHttp.addEventListener("click",   () => { _setProxyType("http");   skSet({[SK.PROXY_TYPE]: "http"});   });

btnEmailMailtm.addEventListener("click", () => setEmailProvider("mailtm"));
btnEmailCf.addEventListener("click",     () => setEmailProvider("cloudflare"));

chkSolverParallel.addEventListener("change", () => toggleSolverParallelism(chkSolverParallel.checked));
chkPreVisitWarmup.addEventListener("change", () => togglePreVisitWarmup(chkPreVisitWarmup.checked));
chkLangChange.addEventListener("change",     () => toggleLangChange(chkLangChange.checked));

// ── Status section ────────────────────────────────────────────────────────────

chrome.runtime.sendMessage({type: MSG.PING}, (resp) => {
  if (chrome.runtime.lastError || !resp) {
    swEl.textContent = "Extension unreachable";
    swEl.style.color = "#ff6b6b";
    return;
  }
  swEl.textContent = `v${resp.version} active`;
  swEl.style.color = "#444";
});
