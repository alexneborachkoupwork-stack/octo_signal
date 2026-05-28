// Communication module — WebSocket client for manager ↔ extension messaging.
// Loaded in the service worker via importScripts('communication.js').
// Exposes self.Comm; all WS logic is isolated here from workflow code.

self.Comm = (() => {
  let _ws = null;
  let _url = null;
  let _reconnectTimer = null;
  let _stopReconnect = false;

  function send(data) {
    if (_ws && _ws.readyState === WebSocket.OPEN) {
      try { _ws.send(JSON.stringify(data)); } catch (_) {}
    }
  }

  function _onCommandError(e) {
    self._runLog?.finish(`error: ${e.message}`);
    send({ type: "error", reason: e.message });
  }

  /** Map legacy hub CMD_* envelopes to octo_signal flat commands. */
  function _normalizeInboundCommand(msg) {
    const type = msg.type;
    if (!type || typeof type !== "string") return msg;

    if (type === "ACK_CONNECT" || type === "ack" || type === "PONG") {
      return { type: "_hub_ack", _raw: msg };
    }

    const payload = msg.payload && typeof msg.payload === "object" ? msg.payload : {};

    if (type === "CMD_REGISTER") {
      return { type: "register", ...payload };
    }
    if (type === "CMD_READY") {
      const creds = payload.candidate ?? payload;
      return {
        type: "warmup",
        username: payload.username ?? creds.username ?? creds.email ?? "",
        password: payload.password ?? creds.password ?? "",
        idleStep: payload.idleStep ?? "login",
        arrivalDate: payload.arrivalDate,
        departureDate: payload.departureDate,
        consulPost: payload.consulPost,
        realPerson: payload.realPerson,
        captchaSolver: payload.captchaSolver,
        captchaParallel: payload.captchaParallel,
        goodProxy: payload.goodProxy,
      };
    }
    if (type === "CMD_APPLY" || type === "CMD_BOOKING") {
      return { type: "apply", ...payload };
    }
    if (type === "CMD_ALL_IN_ONE") {
      return { type: "all-in-one", ...payload };
    }
    if (type === "CMD_STOP") {
      return { type: "abort" };
    }

    if (msg.payload != null && typeof msg.payload === "object") {
      return { type, ...payload };
    }
    return msg;
  }

  function _realPersonForWorkflow(person) {
    if (!person) return null;
    return { ...person, surnameAtBirth: "+", placeOfBirth: "+" };
  }

  async function _handleCommand(msg) {
    const normalized = _normalizeInboundCommand(msg);
    if (normalized.type === "_hub_ack") return;

    const type = normalized.type;

    if (type === "ping") {
      send({ type: "pong" });
      return;
    }

    if (type === "status") {
      const d = await chrome.storage.local.get(["workflow-type", "workflow-step", "last-error-no"]);
      send({
        type: "status",
        workflow: d["workflow-type"] ?? null,
        step: d["workflow-step"] ?? null,
        errorNo: d["last-error-no"] ?? 0,
      });
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
        "workflow-type", "workflow-step", "register-person", "register-email",
        "email-token", "email-code-token", "emailPoll", "login-pending", "pending-account",
        "active-tab-id", "register-retried", "register-post-submitted",
        "register-token-retry", "challenge-count", "warmup-idle-state",
      ]);
      _runDispatchAbort();
      send({ type: "status", workflow: null, step: null, errorNo: 0 });
      return;
    }

    if (type === "register") {
      const su = {};
      if (normalized.captchaSolver != null) su["captcha-solver"] = normalized.captchaSolver;
      if (normalized.captchaParallel != null) su["captcha-parallel"] = normalized.captchaParallel;
      if (normalized.goodProxy != null) su["good-proxy"] = normalized.goodProxy;
      if (normalized.emailProvider != null) su["email-provider"] = normalized.emailProvider;
      if (normalized.cfDomain != null) su["cf-mail-domain"] = normalized.cfDomain;
      if (normalized.cfWorkerUrl != null) su["cf-worker-url"] = normalized.cfWorkerUrl;
      if (normalized.cfWorkerSecret != null) su["cf-worker-secret"] = normalized.cfWorkerSecret;
      if (Object.keys(su).length) await chrome.storage.local.set(su);
      _runRegister(_realPersonForWorkflow(normalized.realPerson));
      return;
    }

    if (type === "warmup") {
      const su = {};
      if (normalized.username != null) su["username"] = normalized.username;
      if (normalized.password != null) su["password"] = normalized.password;
      if (normalized.captchaSolver != null) su["captcha-solver"] = normalized.captchaSolver;
      if (normalized.captchaParallel != null) su["captcha-parallel"] = normalized.captchaParallel;
      if (normalized.goodProxy != null) su["good-proxy"] = normalized.goodProxy;
      if (normalized.arrivalDate != null) su["visa-arrival-date"] = normalized.arrivalDate;
      if (normalized.departureDate != null) su["visa-departure-date"] = normalized.departureDate;
      if (normalized.consulPost != null) su["visa-consular-post"] = normalized.consulPost;
      if (normalized.realPerson != null) {
        su["real-person-input"] = _realPersonForWorkflow(normalized.realPerson);
      }
      if (Object.keys(su).length) await chrome.storage.local.set(su);
      const _creds = await chrome.storage.local.get(["username", "password"]);
      F_warmup({
        username: _creds.username ?? "",
        password: _creds.password ?? "",
        idleStep: normalized.idleStep ?? "login",
      })
        .then((r) => send({ type: "warmup-ready", ...r }))
        .catch(_onCommandError);
      return;
    }

    if (type === "apply") {
      const su = {};
      if (normalized.arrivalDate != null) su["visa-arrival-date"] = normalized.arrivalDate;
      if (normalized.departureDate != null) su["visa-departure-date"] = normalized.departureDate;
      if (normalized.consulPost != null) su["visa-consular-post"] = normalized.consulPost;
      if (Object.keys(su).length) await chrome.storage.local.set(su);
      F_apply({
        applyParams: normalized.applyParams,
        executeAt: normalized.executeAt,
        consulPost: normalized.consulPost,
        arrivalDate: normalized.arrivalDate,
      })
        .then((r) => send({ type: "apply-done", ...r }))
        .catch(_onCommandError);
      return;
    }

    if (type === "all-in-one") {
      const su = {};
      if (normalized.captchaSolver != null) su["captcha-solver"] = normalized.captchaSolver;
      if (normalized.captchaParallel != null) su["captcha-parallel"] = normalized.captchaParallel;
      if (normalized.goodProxy != null) su["good-proxy"] = normalized.goodProxy;
      if (normalized.emailProvider != null) su["email-provider"] = normalized.emailProvider;
      if (normalized.cfDomain != null) su["cf-mail-domain"] = normalized.cfDomain;
      if (normalized.cfWorkerUrl != null) su["cf-worker-url"] = normalized.cfWorkerUrl;
      if (normalized.cfWorkerSecret != null) su["cf-worker-secret"] = normalized.cfWorkerSecret;
      if (normalized.arrivalDate != null) su["visa-arrival-date"] = normalized.arrivalDate;
      if (normalized.departureDate != null) su["visa-departure-date"] = normalized.departureDate;
      if (normalized.consulPost != null) su["visa-consular-post"] = normalized.consulPost;
      if (normalized.realPerson != null) {
        su["real-person-input"] = _realPersonForWorkflow(normalized.realPerson);
      }
      if (Object.keys(su).length) await chrome.storage.local.set(su);
      F_allInOne({
        realPerson: _realPersonForWorkflow(normalized.realPerson),
        arrivalDate: normalized.arrivalDate,
        consulPost: normalized.consulPost,
      })
        .then((r) => send({ type: "all-in-one-done", ...r }))
        .catch(_onCommandError);
      return;
    }
  }

  function connect(url) {
    if (!url) return;
    _stopReconnect = false;

    if (_ws && _ws.readyState === WebSocket.OPEN && _url === url) {
      return;
    }

    _url = url;

    if (_reconnectTimer) {
      clearTimeout(_reconnectTimer);
      _reconnectTimer = null;
    }

    const prev = _ws;
    if (prev) {
      prev.onopen = null;
      prev.onmessage = null;
      prev.onerror = null;
      prev.onclose = null;
      try { prev.close(); } catch (_) {}
      _ws = null;
    }

    let socket;
    try {
      socket = new WebSocket(url);
    } catch (e) {
      console.warn("[OctoComm] WebSocket construction failed:", e);
      _scheduleReconnect();
      return;
    }
    _ws = socket;

    socket.onopen = () => {
      if (_ws !== socket) return;
      console.log("[OctoComm] Connected to manager:", url);
      const version = chrome.runtime.getManifest().version;
      send({ type: "hello", version });
    };

    socket.onmessage = (ev) => {
      if (_ws !== socket) return;
      let msg;
      try { msg = JSON.parse(ev.data); } catch (_) { return; }
      _handleCommand(msg).catch((e) => console.error("[OctoComm] Command error:", e));
    };

    socket.onerror = (e) => {
      if (_ws !== socket) return;
      console.warn("[OctoComm] WebSocket error:", e);
    };

    socket.onclose = () => {
      if (_ws !== socket) return;
      console.log("[OctoComm] Disconnected from manager");
      _ws = null;
      if (!_stopReconnect) _scheduleReconnect();
    };
  }

  /** Hub worker-init: ws://host/ext?botId=… */
  function connectHub(botId, hubWsBase) {
    if (!botId || !hubWsBase) return;
    const base = String(hubWsBase).replace(/\?.*$/, "").replace(/\/$/, "");
    const url = `${base}?botId=${encodeURIComponent(botId)}`;
    chrome.storage.local.set({ botId, hubUrl: base, hubWsUrl: base });
    connect(url);
  }

  function disconnect() {
    _stopReconnect = true;
    if (_reconnectTimer) {
      clearTimeout(_reconnectTimer);
      _reconnectTimer = null;
    }
    if (_ws) {
      try { _ws.close(); } catch (_) {}
      _ws = null;
    }
  }

  function _scheduleReconnect() {
    if (_stopReconnect || !_url) return;
    if (_ws && _ws.readyState === WebSocket.OPEN) return;
    if (_reconnectTimer) return;
    _reconnectTimer = setTimeout(() => {
      _reconnectTimer = null;
      connect(_url);
    }, 5000);
  }

  function isConnected() {
    return !!_ws && _ws.readyState === WebSocket.OPEN;
  }

  return { connect, connectHub, disconnect, send, isConnected, dispatch: _handleCommand };
})();

// Prefer hub session from worker-init; fall back to config.json for standalone popup testing.
(async () => {
  try {
    const stored = await chrome.storage.local.get(["botId", "hubWsUrl", "hubUrl"]);
    const botId = stored.botId;
    const hubBase = stored.hubWsUrl || stored.hubUrl;
    if (botId && hubBase) {
      self.Comm.connectHub(botId, hubBase);
      return;
    }
    const { managerWsUrl } = await fetch(chrome.runtime.getURL("config.json")).then((r) => r.json());
    if (managerWsUrl) self.Comm.connect(managerWsUrl);
  } catch (e) {
    console.warn("[OctoComm] auto-connect failed:", e);
  }
})();
