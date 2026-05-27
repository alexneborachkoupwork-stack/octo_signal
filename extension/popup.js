const userEl       = document.getElementById("inp-user");
const passEl       = document.getElementById("inp-pass");
const saveBtn      = document.getElementById("btn-save");
const goBtn        = document.getElementById("btn-go");
const regBtn       = document.getElementById("btn-register");
const statusEl     = document.getElementById("status");
const swEl         = document.getElementById("sw-status");
const selSolver    = document.getElementById("sel-solver");
const chkParallel  = document.getElementById("chk-parallel");

// Email provider elements
const btnEmailMailtm = document.getElementById("btn-email-mailtm");
const btnEmailCf     = document.getElementById("btn-email-cf");

// Visa section elements
const visaArrivalEl = document.getElementById("visa-arrival");
const visaPostoEl   = document.getElementById("visa-posto");
const btnVisa       = document.getElementById("btn-visa");

// Real person form elements
const rpFirstname  = document.getElementById("rp-firstname");
const rpLastname   = document.getElementById("rp-lastname");
const rpDob        = document.getElementById("rp-dob");
const rpGender     = document.getElementById("rp-gender");
const rpTraveldoc  = document.getElementById("rp-traveldoc");
const btnRegReal   = document.getElementById("btn-reg-real");
const btnRpClear   = document.getElementById("btn-rp-clear");

function setStatus(msg, isError = false) {
  statusEl.textContent = msg;
  statusEl.className   = isError ? "error" : "";
}

// Load saved credentials, captcha settings, real person data, and workflow state on open.
chrome.storage.local.get(
  ["username","password","captcha-solver","captcha-parallel",
   "workflow-type","workflow-step","real-person-input",
   "visa-arrival-date","visa-consular-post",
   "email-provider"],
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

    selSolver.value     = data["captcha-solver"]   ?? "capsolver";
    chkParallel.checked = data["captcha-parallel"] ?? false;

    setEmailProvider(data["email-provider"] ?? "mailtm");

    // Restore real person form
    const rp = data["real-person-input"] ?? {};
    if (rp.firstName)      rpFirstname.value      = rp.firstName;
    if (rp.lastName)       rpLastname.value       = rp.lastName;
    if (rp.dob)            rpDob.value            = rp.dob;
    if (rp.gender)         rpGender.value         = rp.gender;
    if (rp.traveldoc)      rpTraveldoc.value      = rp.traveldoc;
  }
);

// Auto-save real person fields to storage whenever they change.
function _saveRpInput() {
  chrome.storage.local.set({"real-person-input": {
    firstName:      rpFirstname.value.trim(),
    lastName:       rpLastname.value.trim(),
    dob:            rpDob.value.trim(),
    gender:         rpGender.value,
    nationality:    "CPV",
    traveldoc:      rpTraveldoc.value.trim(),
    surnameAtBirth: "+",
    placeOfBirth:   "+",
  }});
}
[rpFirstname, rpLastname, rpDob, rpTraveldoc].forEach(el =>
  el.addEventListener("input", _saveRpInput)
);
rpGender.addEventListener("change", _saveRpInput);

selSolver.addEventListener("change", () => {
  chrome.storage.local.set({"captcha-solver": selSolver.value});
});

chkParallel.addEventListener("change", () => {
  chrome.storage.local.set({"captcha-parallel": chkParallel.checked});
});

function setEmailProvider(provider) {
  const isCf = provider === "cloudflare";
  btnEmailMailtm.classList.toggle("active", !isCf);
  btnEmailCf.classList.toggle("active",      isCf);
  chrome.storage.local.set({"email-provider": provider});
}

btnEmailMailtm.addEventListener("click", () => setEmailProvider("mailtm"));
btnEmailCf.addEventListener("click",     () => setEmailProvider("cloudflare"));


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

// Warmup (login) — dispatch through the shared command layer
goBtn.addEventListener("click", () => {
  const username = userEl.value.trim();
  const password = passEl.value;
  if (!username || !password) { setStatus("Save credentials first.", true); return; }
  chrome.runtime.sendMessage({type: "comm-dispatch", payload: {
    type: "warmup", username, password, idleStep: "login",
  }});
  window.close();
});

// Register (auto) — full pipeline: register → login → apply
regBtn.addEventListener("click", () => {
  chrome.runtime.sendMessage({type: "comm-dispatch", payload: {type: "all-in-one"}});
  window.close();
});

// Register (real person) — dispatch through the shared command layer
btnRegReal.addEventListener("click", () => {
  const realPerson = {
    firstName:      rpFirstname.value.trim(),
    lastName:       rpLastname.value.trim(),
    dob:            rpDob.value.trim(),
    gender:         rpGender.value,
    nationality:    "CPV",
    traveldoc:      rpTraveldoc.value.trim(),
    surnameAtBirth: "+",
    placeOfBirth:   "+",
  };
  const REQUIRED_RP_KEYS = ["firstName","lastName","dob","gender","traveldoc"];
  const missing = REQUIRED_RP_KEYS.filter(k => !realPerson[k]);
  if (missing.length) {
    setStatus(`Fill in: ${missing.join(", ")}`, true);
    return;
  }
  chrome.runtime.sendMessage({type: "comm-dispatch", payload: {type: "register", realPerson}});
  setStatus("Register started.");
  window.close();
});

// Clear real person form and remove from storage
btnRpClear.addEventListener("click", () => {
  rpFirstname.value = rpLastname.value = rpDob.value = "";
  rpGender.value = "";
  rpTraveldoc.value = "";
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

// Apply for Visa — dispatch through the shared command layer
btnVisa.addEventListener("click", () => {
  const arrivalDate = visaArrivalEl.value.trim();
  const postoId     = visaPostoEl.value.trim() || "5088";

  btnVisa.disabled    = true;
  btnVisa.textContent = "Starting…";

  // Flush latest visa config to storage, then dispatch apply.
  chrome.storage.local.set({"visa-arrival-date": arrivalDate, "visa-consular-post": postoId}, () => {
    chrome.runtime.sendMessage({type: "comm-dispatch", payload: {type: "apply"}});
    btnVisa.disabled    = false;
    btnVisa.textContent = "Apply for Visa";
    window.close();
  });
});
