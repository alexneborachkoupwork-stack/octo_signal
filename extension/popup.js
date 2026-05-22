const userEl       = document.getElementById("inp-user");
const passEl       = document.getElementById("inp-pass");
const saveBtn      = document.getElementById("btn-save");
const goBtn        = document.getElementById("btn-go");
const regBtn       = document.getElementById("btn-register");
const statusEl     = document.getElementById("status");
const swEl         = document.getElementById("sw-status");
const btnWarmupOn  = document.getElementById("btn-warmup-on");
const btnWarmupOff = document.getElementById("btn-warmup-off");
const btnChainNone = document.getElementById("btn-chain-none");
const btnChainVisa = document.getElementById("btn-chain-visa");
const btnHuman     = document.getElementById("btn-human");
const btnApi       = document.getElementById("btn-api");
const apiOpts      = document.getElementById("api-opts");
const selSolver    = document.getElementById("sel-solver");
const chkParallel  = document.getElementById("chk-parallel");

// Email provider elements
const btnEmailMailtm = document.getElementById("btn-email-mailtm");
const btnEmailCf     = document.getElementById("btn-email-cf");
const cfOpts         = document.getElementById("cf-opts");
const cfDomainEl     = document.getElementById("cf-domain");
const cfWorkerEl     = document.getElementById("cf-worker");
const cfSecretEl     = document.getElementById("cf-secret");

// Visa section elements
const visaArrivalEl = document.getElementById("visa-arrival");
const visaPostoEl   = document.getElementById("visa-posto");
const btnVisa       = document.getElementById("btn-visa");

// Real person form elements
const rpFirstname  = document.getElementById("rp-firstname");
const rpLastname   = document.getElementById("rp-lastname");
const rpDob        = document.getElementById("rp-dob");
const rpGender     = document.getElementById("rp-gender");
const rpNationality= document.getElementById("rp-nationality");
const rpTraveldoc  = document.getElementById("rp-traveldoc");
const rpSurnameBirth  = document.getElementById("rp-surname-birth");
const rpPlaceBirth    = document.getElementById("rp-place-birth");
const rpPassportIssue = document.getElementById("rp-passport-issue");
const rpPassportExpiry= document.getElementById("rp-passport-expiry");
const btnRegReal   = document.getElementById("btn-reg-real");
const btnRpClear   = document.getElementById("btn-rp-clear");

function setStatus(msg, isError = false) {
  statusEl.textContent = msg;
  statusEl.className   = isError ? "error" : "";
}

// Load saved credentials, captcha settings, real person data, and workflow state on open.
chrome.storage.local.get(
  ["username","password","captcha-mode","captcha-solver","captcha-parallel",
   "workflow-type","workflow-step","real-person-input",
   "visa-arrival-date","visa-consular-post",
   "email-provider","cf-mail-domain","cf-worker-url","cf-worker-secret",
   "warmup-enabled","register-chain"],
  (data) => {
    if (data.username) userEl.value = data.username;
    if (data.password) passEl.value = data.password;

    if (data["visa-arrival-date"]) visaArrivalEl.value = data["visa-arrival-date"];
    if (data["visa-consular-post"]) visaPostoEl.value  = data["visa-consular-post"];

    const wfType = data["workflow-type"];
    const wfStep = data["workflow-step"];
    if (wfType) {
      setStatus(`Running: ${wfType} / ${wfStep ?? "…"} — clicking again resets`);
    } else if (data.username && data.password) {
      setStatus("Credentials loaded.");
    }

    setWarmup(data["warmup-enabled"] ?? false);
    setRegChain(data["register-chain"] ?? "none");
    setCaptchaMode(data["captcha-mode"] ?? "api");
    selSolver.value     = data["captcha-solver"]   ?? "capsolver";
    chkParallel.checked = data["captcha-parallel"] ?? false;

    setEmailProvider(data["email-provider"] ?? "mailtm");
    if (data["cf-mail-domain"])    cfDomainEl.value = data["cf-mail-domain"];
    if (data["cf-worker-url"])     cfWorkerEl.value = data["cf-worker-url"];
    if (data["cf-worker-secret"])  cfSecretEl.value = data["cf-worker-secret"];

    // Restore real person form
    const rp = data["real-person-input"] ?? {};
    if (rp.firstName)      rpFirstname.value      = rp.firstName;
    if (rp.lastName)       rpLastname.value       = rp.lastName;
    if (rp.dob)            rpDob.value            = rp.dob;
    if (rp.gender)         rpGender.value         = rp.gender;
    if (rp.nationality)    rpNationality.value    = rp.nationality;
    if (rp.traveldoc)      rpTraveldoc.value      = rp.traveldoc;
    if (rp.surnameAtBirth) rpSurnameBirth.value   = rp.surnameAtBirth;
    if (rp.placeOfBirth)   rpPlaceBirth.value     = rp.placeOfBirth;
    if (rp.passportIssue)  rpPassportIssue.value  = rp.passportIssue;
    if (rp.passportExpiry) rpPassportExpiry.value = rp.passportExpiry;
  }
);

// Auto-save real person fields to storage whenever they change.
function _saveRpInput() {
  chrome.storage.local.set({"real-person-input": {
    firstName:      rpFirstname.value.trim(),
    lastName:       rpLastname.value.trim(),
    dob:            rpDob.value.trim(),
    gender:         rpGender.value,
    nationality:    rpNationality.value.trim(),
    traveldoc:      rpTraveldoc.value.trim(),
    surnameAtBirth: rpSurnameBirth.value.trim(),
    placeOfBirth:   rpPlaceBirth.value.trim(),
    passportIssue:  rpPassportIssue.value.trim(),
    passportExpiry: rpPassportExpiry.value.trim(),
  }});
}
[rpFirstname, rpLastname, rpDob, rpNationality, rpTraveldoc,
 rpSurnameBirth, rpPlaceBirth, rpPassportIssue, rpPassportExpiry].forEach(el =>
  el.addEventListener("input", _saveRpInput)
);
rpGender.addEventListener("change", _saveRpInput);

function setWarmup(enabled) {
  btnWarmupOn.classList.toggle("active",  enabled);
  btnWarmupOff.classList.toggle("active", !enabled);
  chrome.storage.local.set({"warmup-enabled": enabled});
}
btnWarmupOn.addEventListener("click",  () => setWarmup(true));
btnWarmupOff.addEventListener("click", () => setWarmup(false));

function setRegChain(chain) {
  btnChainNone.classList.toggle("active", chain === "none");
  btnChainVisa.classList.toggle("active", chain === "visa");
  chrome.storage.local.set({"register-chain": chain});
}
btnChainNone.addEventListener("click", () => setRegChain("none"));
btnChainVisa.addEventListener("click", () => setRegChain("visa"));

function setCaptchaMode(mode) {
  const isHuman = mode === "human";
  btnHuman.classList.toggle("active", isHuman);
  btnApi.classList.toggle("active", !isHuman);
  apiOpts.style.display = isHuman ? "none" : "block";
  chrome.storage.local.set({"captcha-mode": mode});
}

btnHuman.addEventListener("click", () => setCaptchaMode("human"));
btnApi.addEventListener("click",   () => setCaptchaMode("api"));

selSolver.addEventListener("change", () => {
  chrome.storage.local.set({"captcha-solver": selSolver.value});
});

chkParallel.addEventListener("change", () => {
  chrome.storage.local.set({"captcha-parallel": chkParallel.checked});
});

function setEmailProvider(provider) {
  const isCf = provider === "cloudflare";
  btnEmailMailtm.classList.toggle("active",  !isCf);
  btnEmailCf.classList.toggle("active",       isCf);
  cfOpts.style.display = isCf ? "block" : "none";
  chrome.storage.local.set({"email-provider": provider});
}

btnEmailMailtm.addEventListener("click", () => setEmailProvider("mailtm"));
btnEmailCf.addEventListener("click",     () => setEmailProvider("cloudflare"));

// Auto-save CF config fields
function _saveCfConfig() {
  chrome.storage.local.set({
    "cf-mail-domain":    cfDomainEl.value.trim(),
    "cf-worker-url":     cfWorkerEl.value.trim(),
    "cf-worker-secret":  cfSecretEl.value,
  });
}
[cfDomainEl, cfWorkerEl, cfSecretEl].forEach(el => el.addEventListener("input", _saveCfConfig));

// Confirm service worker is alive
chrome.runtime.sendMessage({type: "ping"}, (resp) => {
  if (chrome.runtime.lastError || !resp) {
    swEl.textContent = "Extension unreachable";
    swEl.style.color = "#ff6b6b";
    return;
  }
  swEl.textContent = `v${resp.version} active`;
  swEl.style.color  = "#444";
});

// Save credentials
saveBtn.addEventListener("click", () => {
  const username = userEl.value.trim();
  const password = passEl.value;
  if (!username || !password) { setStatus("Both fields are required.", true); return; }
  chrome.runtime.sendMessage({type: "save-creds", username, password}, (resp) => {
    setStatus(resp?.ok ? "Credentials saved." : "Save failed.", !resp?.ok);
  });
});

// Login — navigate to target site and run login workflow
goBtn.addEventListener("click", () => {
  const username = userEl.value.trim();
  const password = passEl.value;
  if (!username || !password) { setStatus("Save credentials first.", true); return; }
  chrome.runtime.sendMessage({type: "save-creds", username, password}, () => {
    chrome.runtime.sendMessage({type: "go-to-site"});
    window.close();
  });
});

// Register (auto) — generate fake data + email, then run registration workflow
regBtn.addEventListener("click", () => {
  regBtn.disabled    = true;
  regBtn.textContent = "Preparing…";
  setStatus("Generating person data and email…");

  chrome.runtime.sendMessage({type: "start-register"}, (resp) => {
    regBtn.disabled    = false;
    regBtn.textContent = "Register (auto)";

    if (!resp?.ok) {
      setStatus(`Error: ${resp?.error ?? "unknown"}`, true);
      return;
    }
    setStatus(`${resp.email}`);
    window.close();
  });
});

// Register (real person) — use passport data from the form below
btnRegReal.addEventListener("click", () => {
  const realPerson = {
    firstName:      rpFirstname.value.trim(),
    lastName:       rpLastname.value.trim(),
    dob:            rpDob.value.trim(),
    gender:         rpGender.value,
    nationality:    rpNationality.value.trim(),
    traveldoc:      rpTraveldoc.value.trim(),
    surnameAtBirth: rpSurnameBirth.value.trim(),
    placeOfBirth:   rpPlaceBirth.value.trim(),
    passportIssue:  rpPassportIssue.value.trim(),
    passportExpiry: rpPassportExpiry.value.trim(),
  };
  const REQUIRED_RP_KEYS = ["firstName","lastName","dob","gender","nationality","traveldoc",
                             "surnameAtBirth","placeOfBirth","passportIssue","passportExpiry"];
  const missing = REQUIRED_RP_KEYS.filter(k => !realPerson[k]);
  if (missing.length) {
    setStatus(`Fill in: ${missing.join(", ")}`, true);
    return;
  }

  btnRegReal.disabled    = true;
  btnRegReal.textContent = "Preparing…";
  setStatus("Creating email account…");

  chrome.runtime.sendMessage({type: "start-register", realPerson}, (resp) => {
    btnRegReal.disabled    = false;
    btnRegReal.textContent = "Register";

    if (!resp?.ok) {
      setStatus(`Error: ${resp?.error ?? "unknown"}`, true);
      return;
    }
    setStatus(`${resp.email}`);
    window.close();
  });
});

// Clear real person form and remove from storage
btnRpClear.addEventListener("click", () => {
  rpFirstname.value = rpLastname.value = rpDob.value = "";
  rpGender.value = "";
  rpNationality.value = rpTraveldoc.value = "";
  rpSurnameBirth.value = rpPlaceBirth.value = "";
  rpPassportIssue.value = rpPassportExpiry.value = "";
  chrome.storage.local.remove("real-person-input");
  setStatus("Real person data cleared.");
});

// Auto-save visa fields
visaArrivalEl.addEventListener("input", () => {
  chrome.storage.local.set({"visa-arrival-date": visaArrivalEl.value.trim()});
});
visaPostoEl.addEventListener("input", () => {
  chrome.storage.local.set({"visa-consular-post": visaPostoEl.value.trim() || "5088"});
});

// Apply for Visa — save creds + visa config, then launch workflow
btnVisa.addEventListener("click", () => {
  const username = userEl.value.trim();
  const password = passEl.value;
  if (!username || !password) { setStatus("Save credentials first.", true); return; }

  const arrivalDate  = visaArrivalEl.value.trim();
  const postoId      = visaPostoEl.value.trim() || "5088";

  btnVisa.disabled    = true;
  btnVisa.textContent = "Starting…";

  chrome.runtime.sendMessage({type: "save-creds", username, password}, () => {
    chrome.storage.local.set({"visa-arrival-date": arrivalDate, "visa-consular-post": postoId}, () => {
      chrome.runtime.sendMessage({type: "go-visa"}, (resp) => {
        btnVisa.disabled    = false;
        btnVisa.textContent = "Apply for Visa";
        if (resp?.ok) {
          window.close();
        } else {
          setStatus("Failed to start visa workflow.", true);
        }
      });
    });
  });
});
