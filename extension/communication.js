// Communication module — WebSocket client for manager ↔ extension messaging.
// Loaded in the service worker via importScripts('communication.js').
// Exposes self.Comm; all WS logic is isolated here from workflow code.

self.Comm = (() => {
  let _ws = null;
  let _url = null;
  let _reconnectTimer = null;
  let _stopReconnect = false;
  let _reconnectDelay = 6000;
  let _botId = null; // set by connectHub(), included in every outbound message
  let _generation = 0; // incremented on every connect() call; stale onclose handlers compare against this

  function send(data) {
    if (_ws && _ws.readyState === WebSocket.OPEN) {
      try {
        // Automatically attach sessionId (= botId) to every outbound message
        const msg = _botId ? { sessionId: _botId, ...data } : data;
        _ws.send(JSON.stringify(msg));
      } catch (_) {}
    }
  }

  async function _onCommandError(e) {
    await self._runLog?.finish(`error: ${e.message}`);
    send({
      type:        "error",
      reason:      e.message,
      proxyStatus: e.proxyStatus ?? null,
      nextAction:  e.nextAction  ?? null,
    });
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
      if (normalized.captchaSolver    != null) su["captcha-solver"]       = normalized.captchaSolver;
      if (normalized.captchaDirectClick != null) su["captcha-direct-click"] = !!normalized.captchaDirectClick;
      if (normalized.emailProvider    != null) su["email-provider"]       = normalized.emailProvider;
      if (normalized.cfDomain         != null) su["cf-mail-domain"]       = normalized.cfDomain;
      if (normalized.cfWorkerUrl      != null) su["cf-worker-url"]        = normalized.cfWorkerUrl;
      if (normalized.cfWorkerSecret   != null) su["cf-worker-secret"]     = normalized.cfWorkerSecret;
      if (Object.keys(su).length) await chrome.storage.local.set(su);
      _runRegister(normalized.realPerson);
      return;
    }

    if (type === "warmup") {
      const su = {};
      if (normalized.username           != null) su["username"]             = normalized.username;
      if (normalized.password           != null) su["password"]             = normalized.password;
      if (normalized.captchaSolver      != null) su["captcha-solver"]       = normalized.captchaSolver;
      if (normalized.captchaDirectClick != null) su["captcha-direct-click"] = !!normalized.captchaDirectClick;
      if (normalized.arrivalDate != null) su["visa-arrival-date"] = normalized.arrivalDate;
      if (normalized.departureDate != null) su["visa-departure-date"] = normalized.departureDate;
      if (normalized.consulPost != null) su["visa-consular-post"] = normalized.consulPost;
      if (normalized.realPerson != null) {
        su["real-person-input"] = normalized.realPerson;
      }
      if (Object.keys(su).length) await chrome.storage.local.set(su);
      const _creds = await chrome.storage.local.get(["username", "password"]);
      F_warmup({
        username: _creds.username ?? "",
        password: _creds.password ?? "",
        idleStep: normalized.idleStep ?? "login",
      })
        .then((r) => send({ type: "warmup-done", ...r }))
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
      if (normalized.captchaSolver      != null) su["captcha-solver"]       = normalized.captchaSolver;
      if (normalized.captchaDirectClick != null) su["captcha-direct-click"] = !!normalized.captchaDirectClick;
      if (normalized.emailProvider      != null) su["email-provider"]       = normalized.emailProvider;
      if (normalized.cfDomain           != null) su["cf-mail-domain"]       = normalized.cfDomain;
      if (normalized.cfWorkerUrl        != null) su["cf-worker-url"]        = normalized.cfWorkerUrl;
      if (normalized.cfWorkerSecret     != null) su["cf-worker-secret"]     = normalized.cfWorkerSecret;
      if (normalized.arrivalDate != null) su["visa-arrival-date"] = normalized.arrivalDate;
      if (normalized.departureDate != null) su["visa-departure-date"] = normalized.departureDate;
      if (normalized.consulPost != null) su["visa-consular-post"] = normalized.consulPost;
      if (normalized.realPerson != null) {
        su["real-person-input"] = normalized.realPerson;
      }
      if (Object.keys(su).length) await chrome.storage.local.set(su);
      F_allInOne({
        realPerson:  normalized.realPerson,
        emailAcct:   normalized.emailAcct,
        arrivalDate: normalized.arrivalDate,
        consulPost:  normalized.consulPost,
      })
        .then((r) => {
          // Only assert proxyStatus:"ok" on genuine success — don't misreport when F5 fails.
          const extra = r.ok !== false ? { proxyStatus: "ok" } : {};
          send({ type: "all-in-one-done", ...r, ...extra });
        })
        .catch(_onCommandError);
      return;
    }
  }

  function connect(url) {
    if (!url) return;
    _stopReconnect = false;
    // Bump generation so any pending onclose from the old socket knows it's stale
    // and does not schedule a spurious reconnect.
    const myGen = ++_generation;

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

    _ws.onopen = () => {
      if (_generation !== myGen) return;
      _reconnectDelay = 6000;
      if (_reconnectTimer) {
        clearTimeout(_reconnectTimer);
        _reconnectTimer = null;
      }
      console.log("[OctoComm] Connected to manager:", url);
      send({ type: "hello", version: chrome.runtime.getManifest().version });
    };

    _ws.onmessage = (ev) => {
      if (_generation !== myGen) return;
      let msg;
      try { msg = JSON.parse(ev.data); } catch (_) { return; }
      _handleCommand(msg).catch((e) => console.error("[OctoComm] Command error:", e));
    };

    socket.onerror = (e) => {
      if (_ws !== socket) return;
      console.warn("[OctoComm] WebSocket error:", e);
    };

    _ws.onclose = () => {
      // Stale close — a newer connect() already replaced this socket; don't reconnect.
      if (_generation !== myGen) return;
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
    // Idempotent: if a healthy socket to the same endpoint already exists, don't replace it.
    // Both the IIFE auto-reconnect and WORKER_INIT call connectHub — whichever arrives second
    // must not tear down the connection where the command was already sent.
    if (_url === url && _ws && (_ws.readyState === WebSocket.OPEN || _ws.readyState === WebSocket.CONNECTING)) {
      _botId = botId;
      return;
    }
    _botId = botId;
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
    const delay = _reconnectDelay;
    _reconnectTimer = setTimeout(() => {
      _reconnectTimer = null;
      connect(_url);
    }, delay);
    _reconnectDelay = Math.min(_reconnectDelay * 2, 60000);
  }

  function isConnected() {
    return !!_ws && _ws.readyState === WebSocket.OPEN;
  }

  // Reconnect if the WS is not open — called by the ws-ping alarm to recover from
  // SW suspension (SW sleep drops the WS silently without firing onclose).
  // When the SW was terminated (not just suspended), _url is null; fall back to
  // reading botId + hubWsUrl from storage to reconstruct the connection URL.
  function reconnectIfNeeded() {
    if (_stopReconnect) return;
    if (_url) {
      if (!_ws || _ws.readyState === WebSocket.CLOSED || _ws.readyState === WebSocket.CLOSING) {
        connect(_url);
      }
      return;
    }
    // _url is null — SW was terminated and restarted; read from storage.
    chrome.storage.local.get(["botId", "hubWsUrl", "hubUrl"]).then(stored => {
      const botId   = stored.botId;
      const hubBase = stored.hubWsUrl || stored.hubUrl;
      if (botId && hubBase && !_url) {
        connectHub(botId, hubBase);
      }
    }).catch(() => {});
  }

  return { connect, connectHub, disconnect, send, isConnected, reconnectIfNeeded, dispatch: _handleCommand, getBotId: () => _botId };
})();

// Prefer hub session from worker-init; fall back to config.json for standalone popup testing.
// Delay the storage read so WORKER_INIT (fired by hub-init.js on the worker-init tab) has time
// to call connectHub() with the correct sessionId first.  Without the delay, a profile cloned
// from a template inherits stale botId/hubWsUrl in storage and the IIFE would connect with
// that old botId, kicking the real live session off the hub.
// If WORKER_INIT fires before the delay expires, getBotId() will be non-null and we skip.
(async () => {
  try {
    await new Promise(r => setTimeout(r, 500));
    if (self.Comm.getBotId()) return; // WORKER_INIT beat us to it — don't double-connect
    const stored = await chrome.storage.local.get(["botId", "hubWsUrl", "hubUrl"]);
    if (self.Comm.getBotId()) return; // check again after async storage read
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
