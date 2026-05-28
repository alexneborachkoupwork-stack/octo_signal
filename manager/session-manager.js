'use strict';

const { v4: uuidv4 } = require('uuid');
const Session = require('./session');
const log     = require('./logger');

/**
 * SessionManager — creates and tracks all Session instances.
 * Routes inbound hub events to the correct session by botId (= sessionId).
 */
class SessionManager {
  /**
   * @param {object}    opts
   * @param {object}    opts.config
   * @param {OctoApi}   opts.octoApi
   * @param {HubServer} opts.hubServer
   */
  constructor({ config, octoApi, hubServer }) {
    this._config    = config;
    this._octoApi   = octoApi;
    this._hub       = hubServer;
    this._sessions  = new Map(); // sessionId → Session

    // Wire hub events to session routing
    this._hub.on('connected',    (botId, ws)    => this._onConnected(botId, ws));
    this._hub.on('message',      (botId, msg)   => this._onMessage(botId, msg));
    this._hub.on('disconnected', (botId)        => this._onDisconnected(botId));
  }

  // ── Public API ───────────────────────────────────────────────────────────────

  /**
   * Create a new session for the given workflow.
   *
   * @param {string} workflowName  - e.g. 'all-in-one', 'register', 'warmup'
   * @param {object} payload       - command payload merged with { type, sessionId }
   * @param {object} [proxy]       - proxy config for this session
   * @param {Function} [onResult]  - called when session finishes
   * @returns {Session}
   */
  createSession(workflowName, payload = {}, proxy = null, onResult = null, templateUuid = null) {
    const sessionId = uuidv4();

    const session = new Session({
      sessionId,
      config:    this._config,
      octoApi:   this._octoApi,
      hubServer: this._hub,
      workflow:  { name: workflowName, payload },
      proxy,
      templateUuid,
      onResult:  onResult ?? ((r) => {
        log.info(sessionId, `Session result: ok=${r.ok}  status=${r.status ?? r.reason ?? ''}`);
        log.closeSession(sessionId);
      }),
    });

    this._sessions.set(sessionId, session);

    session.on('done',   ()       => this._sessions.delete(sessionId));
    session.on('failed', ()       => this._sessions.delete(sessionId));

    log.ginfo(`SessionManager: created  sessionId=${sessionId}  workflow=${workflowName}`);
    return session;
  }

  getSession(sessionId) {
    return this._sessions.get(sessionId);
  }

  activeSessions() {
    return [...this._sessions.values()];
  }

  stats() {
    const all = this.activeSessions();
    return {
      active:  all.length,
      running: all.filter(s => s.state === Session.STATES.RUNNING).length,
      done:    all.filter(s => s.state === Session.STATES.DONE).length,
      failed:  all.filter(s => s.state === Session.STATES.FAILED).length,
      retrying:all.filter(s => s.state === Session.STATES.RETRYING).length,
    };
  }

  async stopAll() {
    await Promise.all(this.activeSessions().map(s => s.stop().catch(() => {})));
    this._sessions.clear();
  }

  // ── Hub event routing ────────────────────────────────────────────────────────

  _onConnected(botId) {
    const session = this._sessions.get(botId);
    if (!session) {
      log.gwarn(`SessionManager: unknown botId connected  botId=${botId}`);
      return;
    }
    session.onExtensionConnected();
  }

  _onMessage(botId, msg) {
    const session = this._sessions.get(botId);
    if (!session) {
      log.gwarn(`SessionManager: message from unknown botId  botId=${botId}  type=${msg.type}`);
      return;
    }
    session.onMessage(msg);
  }

  _onDisconnected(botId) {
    const session = this._sessions.get(botId);
    if (!session) return;
    // If session is still running / retrying, it means the extension disconnected mid-workflow.
    // The extension will auto-reconnect via its backoff; sessions wait up to extensionConnectTimeoutMs.
    const { state } = session;
    if (state === Session.STATES.RUNNING || state === Session.STATES.RETRYING) {
      log.warn(botId, 'Extension disconnected mid-workflow — waiting for reconnect');
    }
  }
}

module.exports = SessionManager;
