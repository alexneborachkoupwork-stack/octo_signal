'use strict';

const { v4: uuidv4 } = require('uuid');
const Session  = require('./session');
const log      = require('./logger');
const metrics  = require('./metrics');

// Slot message types that bypass the session state machine and go straight to the pool.
const SLOT_MSG_TYPES = new Set(['slot-observation', 'slot-assignment-request', 'slot-failure', 'slot-success']);

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
   * @param {SlotPool}  [opts.slotPool]  - shared slot intelligence pool
   */
  constructor({ config, octoApi, hubServer, slotPool = null }) {
    this._config    = config;
    this._octoApi   = octoApi;
    this._hub       = hubServer;
    this._slotPool  = slotPool;
    this._sessions  = new Map(); // sessionId → Session

    // Wire hub events to session routing
    this._hub.on('connected',    (botId, ws)    => this._onConnected(botId, ws));
    this._hub.on('message',      (botId, msg)   => this._onMessage(botId, msg));
    this._hub.on('disconnected', (botId)        => this._onDisconnected(botId));
  }

  // ── Public API ───────────────────────────────────────────────────────────────

  /**
   * Create a new session for the given workflow (creates a new Octo profile).
   *
   * @param {string}   workflowName
   * @param {object}   payload
   * @param {object}   [proxy]
   * @param {Function} [onResult]
   * @param {string}   [templateUuid]
   * @param {object}   [sessionOpts]    - extra Session constructor options (e.g. { keepProfile })
   * @returns {Session}
   */
  createSession(workflowName, payload = {}, proxy = null, onResult = null, templateUuid = null, sessionOpts = {}) {
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
      ...sessionOpts,
    });

    this._sessions.set(sessionId, session);

    metrics.recordSessionStart(workflowName);
    session.on('done',   () => { this._sessions.delete(sessionId); metrics.recordSessionDone();   this._slotPool?.releaseWorker(sessionId); });
    session.on('failed', () => { this._sessions.delete(sessionId); metrics.recordSessionFailed(); this._slotPool?.releaseWorker(sessionId); });
    session.on('state-change', (s) => { if (s === Session.STATES.RETRYING) metrics.recordSessionRetry(); });

    log.ginfo(`SessionManager: created  sessionId=${sessionId}  workflow=${workflowName}`);
    return session;
  }

  /**
   * Create a session that reuses an existing Octo profile (no profile creation).
   * Used by WorkflowChain to chain steps on the same browser profile.
   *
   * @param {string} workflowName
   * @param {object} payload
   * @param {string} existingProfileUuid
   * @param {object} [proxy]
   * @param {object} [sessionOpts]       - e.g. { keepProfile: true }
   * @returns {Session}
   */
  createSessionOnExistingProfile(workflowName, payload = {}, existingProfileUuid, proxy = null, sessionOpts = {}) {
    const sessionId = uuidv4();

    const session = new Session({
      sessionId,
      config:    this._config,
      octoApi:   this._octoApi,
      hubServer: this._hub,
      workflow:  { name: workflowName, payload },
      proxy,
      templateUuid:    null,
      existingProfile: existingProfileUuid,
      onResult: (r) => {
        log.info(sessionId, `Session result: ok=${r.ok}  status=${r.status ?? r.reason ?? ''}`);
        log.closeSession(sessionId);
      },
      ...sessionOpts,
    });

    this._sessions.set(sessionId, session);

    metrics.recordSessionStart(workflowName);
    session.on('done',   () => { this._sessions.delete(sessionId); metrics.recordSessionDone();   this._slotPool?.releaseWorker(sessionId); });
    session.on('failed', () => { this._sessions.delete(sessionId); metrics.recordSessionFailed(); this._slotPool?.releaseWorker(sessionId); });
    session.on('state-change', (s) => { if (s === Session.STATES.RETRYING) metrics.recordSessionRetry(); });

    log.ginfo(`SessionManager: created (existing profile)  sessionId=${sessionId}  profileUuid=${existingProfileUuid}  workflow=${workflowName}`);
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
    const type = msg.type;

    // Slot intelligence messages are handled here; they don't affect the session state machine.
    if (SLOT_MSG_TYPES.has(type) && this._slotPool) {
      this._handleSlotMessage(botId, msg);
      return;
    }

    // Dual trigger messages — log only, do not route to session state machine.
    if (type === 'target-post-available') {
      log.info(botId, `Worker ready for apply — postId=${msg.postId}`);
      return;
    }
    if (type === 'status-update') {
      log.info(botId, `status-update  state=${msg.state}`);
      return;
    }

    const session = this._sessions.get(botId);
    if (!session) {
      log.gwarn(`SessionManager: message from unknown botId  botId=${botId}  type=${type}`);
      return;
    }
    session.onMessage(msg);
  }

  _handleSlotMessage(botId, msg) {
    const pool = this._slotPool;
    const { type, postId, slots, visibleSlots, slotKey, reason } = msg;

    if (type === 'slot-observation') {
      if (postId && Array.isArray(slots)) {
        pool.observe(botId, postId, slots);
        log.debug(botId, `slot-observation  postId=${postId}  count=${slots.length}  pool=${pool.stats().available} avail`);
      }

    } else if (type === 'slot-assignment-request') {
      const assignment = pool.assign(botId, postId, visibleSlots ?? []);
      log.info(botId, `slot-assignment  primary=${assignment.primary?.slotKey ?? 'none'}  fallbacks=${assignment.fallbacks.length}`);
      this._hub.send(botId, { type: 'slot-assignment', ...assignment });

    } else if (type === 'slot-failure') {
      if (slotKey) {
        pool.reportFailure(slotKey, reason);
        metrics.recordBookingFailure();
        log.info(botId, `slot-failure  slotKey=${slotKey}  reason=${reason ?? ''}`);
      }

    } else if (type === 'slot-success') {
      if (slotKey) {
        pool.reportSuccess(slotKey);
        metrics.recordBookingSuccess();
        log.info(botId, `slot-success  slotKey=${slotKey}`);
      }
    }
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
