// Communication module — WebSocket client for manager ↔ extension messaging.
// Loaded in the service worker via importScripts('communication.js').
// Exposes self.Comm; all WS logic is isolated here from workflow code.

self.Comm = (() => {
  let _ws = null;
  let _url = null;
  let _reconnectTimer = null;
  let _stopReconnect = false;

  // ── Outbound ──────────────────────────────────────────────────────────────

  function send(data) {
    if (_ws && _ws.readyState === WebSocket.OPEN) {
      try { _ws.send(JSON.stringify(data)); } catch (_) {}
    }
  }

  // ── Inbound command → storage → workflow trigger ─────────────────────────

  async function _handleCommand(msg) {
    const type = msg.type;

    if (type === "ping") {
      send({type: "pong"});
      return;
    }

    if (type === "status") {
      const d = await chrome.storage.local.get(["workflow-type","workflow-step","last-error-no"]);
      send({type: "status", workflow: d["workflow-type"] ?? null, step: d["workflow-step"] ?? null, errorNo: d["last-error-no"] ?? 0});
      return;
    }

    if (type === "check-proxy") {
      _runCheckProxy();
      return;
    }

    if (type === "check-solver-balances") {
      _runCheckSolverBalances();
      return;
    }

    if (type === "abort") {
      await chrome.storage.local.remove([
        "workflow-type","workflow-step","register-person","register-email",
        "email-token","email-code-token","emailPoll","login-pending","pending-account",
        "active-tab-id","register-retried","register-post-submitted",
        "register-token-retry","challenge-count","warmup-idle-state",
      ]);
      _runDispatchAbort();
      send({type: "status", workflow: null, step: null, errorNo: 0});
      return;
    }

    if (type === "register") {
      const su = {};
      if (msg.captchaSolver  != null) su["captcha-solver"]   = msg.captchaSolver;
      if (msg.captchaParallel!= null) su["captcha-parallel"] = msg.captchaParallel;
      if (msg.goodProxy      != null) su["good-proxy"]       = msg.goodProxy;
      if (msg.emailProvider  != null) su["email-provider"]   = msg.emailProvider;
      if (msg.cfDomain       != null) su["cf-mail-domain"]   = msg.cfDomain;
      if (msg.cfWorkerUrl    != null) su["cf-worker-url"]    = msg.cfWorkerUrl;
      if (msg.cfWorkerSecret != null) su["cf-worker-secret"] = msg.cfWorkerSecret;
      if (Object.keys(su).length) await chrome.storage.local.set(su);
      let realPerson = null;
      if (msg.realPerson) {
        realPerson = {...msg.realPerson, surnameAtBirth: "+", placeOfBirth: "+"};
      }
      _runRegister(realPerson);
      return;
    }

    if (type === "warmup") {
      const su = {};
      if (msg.username       != null) su["username"]           = msg.username;
      if (msg.password       != null) su["password"]           = msg.password;
      if (msg.captchaSolver  != null) su["captcha-solver"]     = msg.captchaSolver;
      if (msg.captchaParallel!= null) su["captcha-parallel"]   = msg.captchaParallel;
      if (msg.goodProxy      != null) su["good-proxy"]         = msg.goodProxy;
      if (msg.arrivalDate    != null) su["visa-arrival-date"]   = msg.arrivalDate;
      if (msg.departureDate  != null) su["visa-departure-date"] = msg.departureDate;
      if (msg.consulPost     != null) su["visa-consular-post"]  = msg.consulPost;
      if (msg.realPerson     != null) {
        su["real-person-input"] = {...msg.realPerson, surnameAtBirth: "+", placeOfBirth: "+"};
      }
      if (Object.keys(su).length) await chrome.storage.local.set(su);
      const _creds = await chrome.storage.local.get(["username","password"]);
      F_warmup({username: _creds.username ?? "", password: _creds.password ?? "", idleStep: msg.idleStep ?? "login"})
        .then(r  => send({type: "warmup-ready", ...r}))
        .catch(e => { self._runLog?.finish(`error: ${e.message}`); send({type: "error", reason: e.message}); });
      return;
    }

    if (type === "apply") {
      F_apply()
        .then(r  => send({type: "apply-done", ...r}))
        .catch(e => { self._runLog?.finish(`error: ${e.message}`); send({type: "error", reason: e.message}); });
      return;
    }

    if (type === "all-in-one") {
      const su = {};
      if (msg.captchaSolver  != null) su["captcha-solver"]     = msg.captchaSolver;
      if (msg.captchaParallel!= null) su["captcha-parallel"]   = msg.captchaParallel;
      if (msg.goodProxy      != null) su["good-proxy"]         = msg.goodProxy;
      if (msg.emailProvider  != null) su["email-provider"]     = msg.emailProvider;
      if (msg.cfDomain       != null) su["cf-mail-domain"]     = msg.cfDomain;
      if (msg.cfWorkerUrl    != null) su["cf-worker-url"]      = msg.cfWorkerUrl;
      if (msg.cfWorkerSecret != null) su["cf-worker-secret"]   = msg.cfWorkerSecret;
      if (msg.arrivalDate    != null) su["visa-arrival-date"]   = msg.arrivalDate;
      if (msg.departureDate  != null) su["visa-departure-date"] = msg.departureDate;
      if (msg.consulPost     != null) su["visa-consular-post"]  = msg.consulPost;
      if (msg.realPerson     != null) {
        su["real-person-input"] = {...msg.realPerson, surnameAtBirth: "+", placeOfBirth: "+"};
      }
      if (Object.keys(su).length) await chrome.storage.local.set(su);
      let realPerson = null;
      if (msg.realPerson) {
        realPerson = {...msg.realPerson, surnameAtBirth: "+", placeOfBirth: "+"};
      }
      F_allInOne({realPerson, arrivalDate: msg.arrivalDate, consulPost: msg.consulPost})
        .then(r  => send({type: "all-in-one-done", ...r}))
        .catch(e => { self._runLog?.finish(`error: ${e.message}`); send({type: "error", reason: e.message}); });
      return;
    }
  }

  // ── Connection ────────────────────────────────────────────────────────────

  function connect(url) {
    if (!url) return;
    _url = url;
    _stopReconnect = false;

    if (_ws) {
      try { _ws.close(); } catch (_) {}
    }

    try {
      _ws = new WebSocket(url);
    } catch (e) {
      console.warn("[OctoComm] WebSocket construction failed:", e);
      _scheduleReconnect();
      return;
    }

    _ws.onopen = () => {
      console.log("[OctoComm] Connected to manager:", url);
      if (_reconnectTimer) { clearTimeout(_reconnectTimer); _reconnectTimer = null; }
      const version = chrome.runtime.getManifest().version;
      send({type: "hello", version});
    };

    _ws.onmessage = (ev) => {
      let msg;
      try { msg = JSON.parse(ev.data); } catch (_) { return; }
      _handleCommand(msg).catch(e => console.error("[OctoComm] Command error:", e));
    };

    _ws.onerror = (e) => {
      console.warn("[OctoComm] WebSocket error:", e);
    };

    _ws.onclose = () => {
      console.log("[OctoComm] Disconnected from manager");
      _ws = null;
      if (!_stopReconnect) _scheduleReconnect();
    };
  }

  function disconnect() {
    _stopReconnect = true;
    if (_reconnectTimer) { clearTimeout(_reconnectTimer); _reconnectTimer = null; }
    if (_ws) { try { _ws.close(); } catch (_) {} _ws = null; }
  }

  function _scheduleReconnect() {
    if (_stopReconnect || !_url) return;
    _reconnectTimer = setTimeout(() => connect(_url), 5000);
  }

  function isConnected() {
    return !!_ws && _ws.readyState === WebSocket.OPEN;
  }

  return {connect, disconnect, send, isConnected, dispatch: _handleCommand};
})();

// Auto-connect on load using config.json.
(async () => {
  try {
    const {managerWsUrl} = await fetch(chrome.runtime.getURL('config.json')).then(r => r.json());
    if (managerWsUrl) self.Comm.connect(managerWsUrl);
  } catch (e) {
    console.warn("[OctoComm] config.json read failed:", e);
  }
})();
