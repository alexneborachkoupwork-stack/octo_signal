'use strict';

const { EventEmitter } = require('events');
const { v4: uuidv4 }  = require('uuid');
const log             = require('./logger');
const { generatePerson } = require('./identity');
const { createAccount }  = require('./mailtm');

/**
 * WorkflowChain — sequences multiple workflow steps on a single persistent Octo profile.
 *
 * Steps:
 *   register   → create account
 *   sleep      → wait for server-side account activation
 *   warmup     → login + fill form + idle at schedule page
 *   apply      → book appointment from idle state
 *
 * The Octo profile is preserved between steps (keepProfile=true on every session except apply).
 * After apply (or on fatal failure) the profile is deleted.
 *
 * Events:
 *   'done'   (result)  — final step completed successfully
 *   'failed' (reason)  — chain aborted, profile cleaned up
 *   'step'   (name)    — emitted when each step starts
 */
class WorkflowChain extends EventEmitter {
  /**
   * @param {object}         opts
   * @param {SessionManager} opts.sessionManager
   * @param {OctoApi}        opts.octoApi
   * @param {object}         opts.config
   * @param {object}         opts.payload        - { realPerson, emailAcct, consulPost, arrivalDate, proxies:[] }
   * @param {string}         [opts.templateUuid] - clone this template for the first session
   * @param {number}         [opts.sleepMs]      - gap between register and warmup (default 90 000)
   */
  constructor({ sessionManager, octoApi, config, payload, templateUuid = null, sleepMs = 90_000 }) {
    super();
    this._sm           = sessionManager;
    this._octoApi      = octoApi;
    this._config       = config;
    this._payload      = payload;
    this._templateUuid = templateUuid;
    this._sleepMs      = sleepMs;

    this._chainId   = uuidv4();
    this._step      = null;
    this._profileUuid = null; // preserved across steps
  }

  /** Start the chain. Returns a Promise that resolves with the final result. */
  run() {
    return new Promise((resolve, reject) => {
      this._resolve = resolve;
      this._reject  = reject;
      this._runStep('register').catch(e => this._abort(e.message));
    });
  }

  // ── Steps ─────────────────────────────────────────────────────────────────

  async _runStep(name) {
    this._step = name;
    this.emit('step', name);
    log.ginfo(`WorkflowChain [${this._chainId.slice(0,8)}]: step=${name}`);

    if (name === 'register') {
      await this._stepRegister();
    } else if (name === 'sleep') {
      await this._stepSleep();
    } else if (name === 'warmup') {
      await this._stepWarmup();
    } else if (name === 'apply') {
      await this._stepApply();
    }
  }

  async _stepRegister() {
    const proxy = this._payload.proxies?.[0] ?? null;
    const session = this._sm.createSession(
      'register',
      {
        realPerson:    this._payload.realPerson,
        emailAcct:     this._payload.emailAcct,
        captchaSolver: this._payload.captchaSolver ?? 'capsolver',
      },
      proxy,
      null,
      this._templateUuid,
      { keepProfile: true },
    );

    this._profileUuid = null; // will be set after browser starts

    await new Promise((resolve, reject) => {
      session.once('done', (result) => {
        this._profileUuid = session.profileUuid; // preserved because keepProfile=true
        log.ginfo(`WorkflowChain [${this._chainId.slice(0,8)}]: register done  profileUuid=${this._profileUuid}`);
        resolve(result);
      });
      session.once('failed', (reason) => reject(new Error(`register failed: ${reason}`)));
      session.start().catch(reject);
    });

    await this._runStep('sleep');
  }

  async _stepSleep() {
    log.ginfo(`WorkflowChain [${this._chainId.slice(0,8)}]: sleeping ${this._sleepMs}ms for account activation`);
    await new Promise(r => setTimeout(r, this._sleepMs));
    await this._runStep('warmup');
  }

  async _stepWarmup() {
    if (!this._profileUuid) {
      throw new Error('WorkflowChain: no profileUuid for warmup step');
    }
    const proxy = this._payload.proxies?.[0] ?? null;

    // Re-start the existing profile (it was stopped after register, not deleted)
    log.ginfo(`WorkflowChain [${this._chainId.slice(0,8)}]: starting browser for warmup  uuid=${this._profileUuid}`);
    const session = this._sm.createSessionOnExistingProfile(
      'warmup',
      {
        username:      this._payload.emailAcct?.email ?? this._payload.realPerson?.username,
        password:      this._payload.emailAcct?.password ?? this._payload.realPerson?.password,
        idleStep:      this._payload.triggerMode ? 'form-monitor' : 'schedule',
        consulPost:    this._payload.consulPost,
        arrivalDate:   this._payload.arrivalDate,
        realPerson:    this._payload.realPerson,
        captchaSolver: this._payload.captchaSolver ?? 'capsolver',
        triggerMode:   this._payload.triggerMode ?? 'AUTO_TRIGGER',
        targetPostId:  this._payload.consulPost,
      },
      this._profileUuid,
      proxy,
      { keepProfile: true },
    );

    await new Promise((resolve, reject) => {
      session.once('done', (result) => {
        this._profileUuid = session.profileUuid; // still alive
        log.ginfo(`WorkflowChain [${this._chainId.slice(0,8)}]: warmup done`);
        resolve(result);
      });
      session.once('failed', (reason) => reject(new Error(`warmup failed: ${reason}`)));
      session.start().catch(reject);
    });

    await this._runStep('apply');
  }

  async _stepApply() {
    if (!this._profileUuid) {
      throw new Error('WorkflowChain: no profileUuid for apply step');
    }
    const proxy = this._payload.proxies?.[0] ?? null;

    const session = this._sm.createSessionOnExistingProfile(
      'apply',
      {
        consulPost:  this._payload.consulPost,
        arrivalDate: this._payload.arrivalDate,
      },
      this._profileUuid,
      proxy,
      { keepProfile: false }, // apply is the last step — delete profile on completion
    );

    await new Promise((resolve, reject) => {
      session.once('done', (result) => {
        log.ginfo(`WorkflowChain [${this._chainId.slice(0,8)}]: apply done  ok=${result.ok}`);
        this._resolve(result);
        this.emit('done', result);
        resolve();
      });
      session.once('failed', (reason) => {
        reject(new Error(`apply failed: ${reason}`));
      });
      session.start().catch(reject);
    });
  }

  async _abort(reason) {
    log.gerror(`WorkflowChain [${this._chainId.slice(0,8)}]: aborted at step=${this._step}  reason=${reason}`);
    // Clean up profile if we own one
    if (this._profileUuid) {
      try {
        await this._octoApi.stopProfile(this._profileUuid);
        await this._octoApi.deleteProfile(this._profileUuid);
      } catch (_) {}
      this._profileUuid = null;
    }
    this.emit('failed', reason);
    this._reject(new Error(reason));
  }
}

module.exports = { WorkflowChain };
