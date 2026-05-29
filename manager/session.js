'use strict';

const { EventEmitter } = require('events');
const log = require('./logger');

const STATES = {
  CREATED:             'CREATED',
  PROFILE_STARTING:    'PROFILE_STARTING',
  BROWSER_READY:       'BROWSER_READY',
  EXTENSION_CONNECTED: 'EXTENSION_CONNECTED',
  RUNNING:             'RUNNING',
  RETRYING:            'RETRYING',
  DONE:                'DONE',
  FAILED:              'FAILED',
};

/**
 * Session — lifecycle state machine for one Octo Browser profile + extension pair.
 *
 * Events emitted:
 *   'state-change' (newState, oldState)
 *   'done'   (result)      — workflow completed successfully
 *   'failed' (reason)      — workflow failed, session cleaned up
 */
class Session extends EventEmitter {
  /**
   * @param {object} opts
   * @param {string}    opts.sessionId  - UUID, also used as Octo profile botId
   * @param {object}    opts.config     - from config.js
   * @param {OctoApi}   opts.octoApi
   * @param {HubServer} opts.hubServer
   * @param {object}    opts.workflow   - { name, payload } — the command to send
   * @param {object}    [opts.proxy]    - proxy config for this session
   * @param {Function}  [opts.onResult] - called with final result when done/failed
   */
  constructor({ sessionId, config, octoApi, hubServer, workflow, proxy, templateUuid, existingProfile, onResult, keepProfile = false }) {
    super();
    this.sessionId    = sessionId;
    this._config      = config;
    this._octoApi     = octoApi;
    this._hub         = hubServer;
    this.workflow     = workflow;  // { name: 'all-in-one', payload: {...} }
    this._proxy       = proxy ?? null;
    this._templateUuid  = templateUuid ?? null;
    this._existingProfile = existingProfile ?? null; // skip profile creation, use this UUID
    this._onResult    = onResult ?? (() => {});
    this._keepProfile  = keepProfile;  // when true, stop() only stops browser; does not delete profile

    this.state       = STATES.CREATED;
    this.profileUuid = null;   // set after profile creation
    this.debugPort   = null;   // set after browser start
    this.lastCommand = null;   // kept for retry resend
    this.retryCount  = 0;
    this.result      = null;

    this._connectTimer = null;
    this._connectCount    = 0; // incremented each time the extension connects
    this._commandConnectId = 0; // _connectCount value when the last command was sent
  }

  // ── Lifecycle ────────────────────────────────────────────────────────────────

  /**
   * Create the Octo profile, start the browser, navigate to worker-init.
   */
  async start() {
    this._setState(STATES.PROFILE_STARTING);
    try {
      // 1. Create or reuse profile
      if (this._existingProfile) {
        // WorkflowChain hands us an already-created profile — no creation needed
        this.profileUuid = this._existingProfile;
        log.info(this.sessionId, `Reusing existing profile  uuid=${this.profileUuid}`);
      } else if (this._templateUuid) {
        log.info(this.sessionId, `Cloning template profile  template=${this._templateUuid}`);
        const clone = await this._octoApi.cloneProfile(
          this._templateUuid,
          `session-${this.sessionId}`,
        );
        this.profileUuid = clone.uuid;
      } else {
        log.info(this.sessionId, 'Creating Octo profile');
        const profile = await this._octoApi.createProfile({
          name:  `session-${this.sessionId}`,
          proxy: this._proxy,
          tags:  ['auto-tester'],
        });
        this.profileUuid = profile.uuid ?? profile.id ?? profile;
      }
      log.info(this.sessionId, `Profile ready  uuid=${this.profileUuid}`);

      // 2. Start browser
      log.info(this.sessionId, 'Starting browser');
      const { port } = await Promise.race([
        this._octoApi.startProfile(this.profileUuid),
        new Promise((_, reject) => setTimeout(
          () => reject(new Error(`Browser start timeout after ${this._config.browserStartTimeoutMs}ms`)),
          this._config.browserStartTimeoutMs
        )),
      ]);
      this.debugPort = port;
      log.info(this.sessionId, `Browser started  debug_port=${port}`);
      this._setState(STATES.BROWSER_READY);

      // 3. Navigate to worker-init so hub-init.js fires
      const workerInitUrl = this._hub.workerInitUrl(this.sessionId);
      log.info(this.sessionId, `Navigating to worker-init  url=${workerInitUrl}`);
      if (this.debugPort) {
        await this._octoApi.navigateToWorkerInit(this.debugPort, workerInitUrl);
      }

      // 4. Arm a timeout waiting for extension to connect
      this._connectTimer = setTimeout(() => {
        if (this.state === STATES.BROWSER_READY) {
          this._fail('Extension connect timeout');
        }
      }, this._config.extensionConnectTimeoutMs ?? 60_000);

    } catch (err) {
      log.error(this.sessionId, 'start() failed:', err.message);
      await this._fail(`Profile start error: ${err.message}`);
    }
  }

  /**
   * Called by SessionManager when the extension WS connects.
   */
  onExtensionConnected() {
    this._connectCount++;
    if (this._connectTimer) { clearTimeout(this._connectTimer); this._connectTimer = null; }
    log.info(this.sessionId, 'Extension connected');
    if (this.state === STATES.RUNNING || this.state === STATES.DONE || this.state === STATES.FAILED) {
      log.info(this.sessionId, `Reconnect in state ${this.state} — skipping command re-send`);
      return;
    }
    this._setState(STATES.EXTENSION_CONNECTED);
    this._sendWorkflowCommand();
  }

  // Types that carry no workflow signal — logged at DEBUG only.
  static _KEEPALIVE = new Set(['ping','pong','hello','status']);

  /**
   * Called by SessionManager when an inbound message arrives from the extension.
   */
  onMessage(msg) {
    // Ignore messages that arrive after the session has already settled — prevents
    // double-fail / double-stop from stale sockets or delayed extension messages.
    if (this.state === STATES.DONE || this.state === STATES.FAILED) return;

    const type = msg.type;
    if (Session._KEEPALIVE.has(type)) {
      log.debug(this.sessionId, '← ext', type);
    } else {
      log.info(this.sessionId, '← ext', type, msg.ok != null ? `ok=${msg.ok}` : '');
    }

    // Workflow completion
    if (type.endsWith('-done')) {
      if (msg.ok === false) {
        // Extension finished but reported failure (e.g. F5 captcha, login rejected).
        // If the extension included a nextAction hint and we have retries left, retry.
        // Otherwise fail the session so the profile is cleaned up immediately.
        const reason = msg.error ?? msg.status ?? msg.reason ?? `${type} ok=false`;
        if (msg.nextAction && this.retryCount < this._config.maxRetries) {
          log.warn(this.sessionId, `Workflow done-failure  type=${type}  reason=${reason}  nextAction=${msg.nextAction}`);
          this._retry(msg.nextAction).catch(e => this._fail(e.message));
        } else {
          log.warn(this.sessionId, `Workflow reported failure  type=${type}  reason=${reason}`);
          this._fail(reason);
        }
      } else {
        this.result = msg;
        log.info(this.sessionId, `Workflow done  type=${type}  status=${msg.status ?? 'ok'}`);
        this._setState(STATES.DONE);
        this.stop().catch(() => {});
        this.emit('done', this.result);         // removes from sessions map (synchronous)
        this._onResult({ ok: true, sessionId: this.sessionId, ...msg }); // stats correct
      }
      return;
    }

    // Error from extension
    if (type === 'error') {
      const { reason, proxyStatus, nextAction } = msg;
      log.warn(this.sessionId, `error: ${reason}  proxyStatus=${proxyStatus}  nextAction=${nextAction}`);

      if (this.retryCount < this._config.maxRetries && nextAction) {
        this._retry(nextAction).catch(e => this._fail(e.message));
      } else {
        this._fail(reason ?? 'extension error');
      }
      return;
    }

    // hello while RUNNING on a NEWER connection means the WS was replaced after the command
    // was sent — the extension never received it.  Resend.
    // If hello arrives on the SAME connection as the command send (_connectCount ==
    // _commandConnectId), it's just the normal post-open hello — don't resend.
    if (type === 'hello' && this.state === STATES.RUNNING) {
      if (this._connectCount > this._commandConnectId) {
        log.info(this.sessionId, 'Extension hello on fresh reconnect — resending command');
        this._sendWorkflowCommand();
      }
      return;
    }

  }

  /**
   * Stop the browser (and delete the profile unless keepProfile is true).
   * Called after DONE or FAILED.
   */
  async stop() {
    if (this._connectTimer) { clearTimeout(this._connectTimer); this._connectTimer = null; }
    if (!this.profileUuid) return;
    const uuid = this.profileUuid;
    if (!this._keepProfile) this.profileUuid = null; // null immediately so concurrent/double calls are no-ops
    log.info(this.sessionId, `Stopping profile  keepProfile=${this._keepProfile}`);
    try {
      await this._octoApi.stopProfile(uuid);
      if (!this._keepProfile) await this._octoApi.deleteProfile(uuid);
    } catch (err) {
      log.warn(this.sessionId, 'stop() cleanup error:', err.message);
    }
  }

  // ── Retry logic ──────────────────────────────────────────────────────────────

  async _retry(action) {
    this.retryCount++;
    this._setState(STATES.RETRYING);
    log.info(this.sessionId, `Retry #${this.retryCount}  action=${action}`);

    try {
      // Stop browser (profile still exists)
      await this._octoApi.stopProfile(this.profileUuid);

      if (action === 'rotate_proxy') {
        if (this._proxy) {
          // TestWorkflow's state-change listener fires setNextProxy() synchronously
          // before this await, so this._proxy already holds the rotated value.
          await this._octoApi.updateProxy(this.profileUuid, this._proxy);
          log.info(this.sessionId, 'Proxy updated');
        }
      } else if (action === 'change_device') {
        await this._octoApi.renewFingerprint(this.profileUuid);
        log.info(this.sessionId, 'Fingerprint renewed');
      }

      // Restart browser
      const { port } = await Promise.race([
        this._octoApi.startProfile(this.profileUuid),
        new Promise((_, reject) => setTimeout(
          () => reject(new Error(`Browser start timeout after ${this._config.browserStartTimeoutMs}ms`)),
          this._config.browserStartTimeoutMs
        )),
      ]);
      this.debugPort = port;

      // The extension may have auto-connected from stored botId/hubUrl while startProfile()
      // was awaiting — in that case the session is already EXTENSION_CONNECTED or RUNNING.
      // Don't go backwards in the state machine; skip navigate and the connect timer.
      if (this.state !== STATES.RETRYING) {
        log.info(this.sessionId, `Extension already connected during restart (state=${this.state}) — skipping navigate`);
        return;
      }

      this._setState(STATES.BROWSER_READY);

      // Navigate to worker-init again
      const workerInitUrl = this._hub.workerInitUrl(this.sessionId);
      if (this.debugPort) {
        await this._octoApi.navigateToWorkerInit(this.debugPort, workerInitUrl);
      }

      // Arm connect timeout
      this._connectTimer = setTimeout(() => {
        if (this.state === STATES.BROWSER_READY) {
          this._fail('Extension connect timeout (retry)');
        }
      }, this._config.extensionConnectTimeoutMs ?? 60_000);

    } catch (err) {
      log.error(this.sessionId, '_retry() failed:', err.message);
      await this._fail(`Retry error: ${err.message}`);
    }
  }

  /** Update the proxy for next retry (called by TestWorkflow from the proxy pool) */
  setNextProxy(proxy) {
    this._proxy = proxy;
  }

  // ── Helpers ──────────────────────────────────────────────────────────────────

  _sendWorkflowCommand() {
    const cmd = {
      type:      this.workflow.name,
      sessionId: this.sessionId,
      ...this.workflow.payload,
    };
    this.lastCommand = cmd;
    this._commandConnectId = this._connectCount;
    if (this.state !== STATES.RUNNING) this._setState(STATES.RUNNING);
    log.info(this.sessionId, '→ ext', cmd.type);
    this._hub.send(this.sessionId, cmd);
  }

  _setState(newState) {
    const old = this.state;
    this.state = newState;
    log.debug(this.sessionId, `state ${old} → ${newState}`);
    this.emit('state-change', newState, old);
  }

  async _fail(reason) {
    log.error(this.sessionId, `FAILED: ${reason}`);
    this._setState(STATES.FAILED);
    this.emit('failed', reason);                  // removes from sessions map (synchronous)
    this._onResult({ ok: false, sessionId: this.sessionId, reason }); // stats correct
    await this.stop().catch(() => {});
  }

  /**
   * Force-terminate this session from outside (e.g. scheduler timeout).
   * Triggers the full _fail lifecycle: emits 'failed', stops browser, deletes profile.
   * No-op if session already settled.
   */
  terminate(reason = 'terminated by scheduler') {
    if (this.state === STATES.DONE || this.state === STATES.FAILED) return;
    this._fail(reason).catch(() => {});
  }
}

Session.STATES = STATES;
module.exports = Session;
