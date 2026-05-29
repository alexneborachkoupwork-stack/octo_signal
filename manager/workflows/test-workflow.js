'use strict';

const { generatePerson } = require('../identity');
const { createAccount }  = require('../mailtm');
const log = require('../logger');

/**
 * TemplatePool — round-robin selection across template profile UUIDs.
 * Returns UUIDs cyclically, or null if the pool is empty (fall back to blank profile).
 */
class TemplatePool {
  constructor(uuids = []) {
    this._uuids = uuids;
    this._idx   = 0;
  }

  next() {
    if (!this._uuids.length) return null;
    const uuid = this._uuids[this._idx % this._uuids.length];
    this._idx++;
    return uuid;
  }

  size() { return this._uuids.length; }
}

/**
 * ProxyPool — round-robin proxy rotation.
 * Accepts an array of proxy objects:
 *   [{ type:'socks5', host, port, login, password }, ...]
 * Returns proxies cyclically, or null if the pool is empty.
 */
class ProxyPool {
  constructor(proxies = []) {
    this._proxies = proxies;
    this._idx     = 0;
  }

  next() {
    if (!this._proxies.length) return null;
    const proxy = this._proxies[this._idx % this._proxies.length];
    this._idx++;
    return proxy;
  }

  size() { return this._proxies.length; }
}

/**
 * TestWorkflow — auto-tester that starts Octo profiles at an interval and
 * runs the `all-in-one` command on each.
 *
 * Usage:
 *   const workflow = new TestWorkflow({ sessionManager, config, options });
 *   await workflow.run();
 */
class TestWorkflow {
  /**
   * @param {object} opts
   * @param {SessionManager} opts.sessionManager
   * @param {object}         opts.config
   * @param {object}         opts.options
   * @param {number}         opts.options.maxSessions  - max concurrent sessions (default 50)
   * @param {number}         opts.options.intervalMs   - ms between session starts (default 45000)
   * @param {string}         opts.options.consulPost   - consular post ID (default from config)
   * @param {string}         opts.options.command        - workflow command (default 'all-in-one')
   * @param {Array}          opts.options.proxies        - proxy pool entries
   * @param {string[]}       opts.options.templateUuids  - template profile UUIDs (round-robin)
   */
  constructor({ sessionManager, config, options = {} }) {
    this._sm            = sessionManager;
    this._config        = config;
    this._maxSessions   = options.maxSessions ?? 50;
    this._intervalMs    = options.intervalMs  ?? 45_000;
    this._consulPost    = options.consulPost  ?? config.defaultConsulPost;
    this._command       = options.command     ?? 'all-in-one';
    this._proxyPool     = new ProxyPool(options.proxies ?? []);
    this._templatePool  = new TemplatePool(options.templateUuids ?? []);
    this._started       = 0;
    this._results       = [];
    this._timer         = null;
    this._stopped       = false;
  }

  /**
   * Run the test workflow.
   * Returns a Promise that resolves when all sessions have finished.
   */
  run() {
    return new Promise((resolve) => {
      log.ginfo(`TestWorkflow: starting  max=${this._maxSessions}  interval=${this._intervalMs}ms  command=${this._command}`);

      const tick = async () => {
        if (this._stopped || this._started >= this._maxSessions) {
          clearInterval(this._timer);
          this._waitForAll().then(resolve);
          return;
        }

        this._started++;
        const idx = this._started;
        log.ginfo(`TestWorkflow: launching session #${idx} of ${this._maxSessions}`);

        try {
          await this._launchSession(idx);
        } catch (err) {
          log.gerror(`TestWorkflow: launchSession #${idx} threw:`, err.message);
        }
      };

      // Fire immediately, then on interval
      tick();
      this._timer = setInterval(tick, this._intervalMs);
    });
  }

  stop() {
    this._stopped = true;
    if (this._timer) { clearInterval(this._timer); this._timer = null; }
  }

  // ── Private ──────────────────────────────────────────────────────────────────

  async _launchSession(idx) {
    const person       = generatePerson();
    const proxy        = this._proxyPool.next();
    const templateUuid = this._templatePool.next();

    log.ginfo(`TestWorkflow: session #${idx}  person=${person.firstName} ${person.lastName}  proxy=${proxy?.host ?? 'none'}  template=${templateUuid ?? 'none'}`);

    const emailAcct = await createAccount().catch(err => {
      log.gerror(`TestWorkflow: mail.tm account creation failed for session #${idx}:`, err.message);
      throw err;
    });
    log.ginfo(`TestWorkflow: session #${idx}  email=${emailAcct.email}`);

    const payload = {
      consulPost:    this._consulPost,
      captchaSolver: 'capsolver',
      emailAcct: {
        email:    emailAcct.email,
        password: emailAcct.password,
        jwt:      emailAcct.jwt,
      },
      realPerson: {
        firstName:   person.firstName,
        lastName:    person.lastName,
        dob:         person.dob,
        gender:      person.gender,
        nationality: person.nationality,
        traveldoc:   person.traveldoc,
        username:    person.username,
        password:    person.password,
      },
    };

    const session = this._sm.createSession(
      this._command,
      payload,
      proxy,
      (result) => {
        this._results.push({ idx, ...result });
        log.ginfo(`TestWorkflow: result #${idx}  ok=${result.ok}  status=${result.status ?? result.reason ?? ''}`);
        this._printStats();
      },
      templateUuid,
    );

    // Wire session for proxy rotation on retry
    session.on('state-change', (newState) => {
      if (newState === 'RETRYING' && this._proxyPool.size() > 0) {
        const nextProxy = this._proxyPool.next();
        session.setNextProxy(nextProxy);
        log.info(session.sessionId, `Rotating proxy to ${nextProxy?.host}:${nextProxy?.port}`);
      }
    });

    await session.start();
  }

  async _waitForAll() {
    // Hard cap: sessions run concurrently so a flat 15-minute budget covers any realistic workflow.
    const maxWaitMs = 15 * 60 * 1000;
    const deadline  = Date.now() + maxWaitMs;

    log.ginfo('TestWorkflow: all sessions launched, waiting for completion…');
    await new Promise(resolve => {
      const check = () => {
        if (this._sm.activeSessions().length === 0 || Date.now() >= deadline) return resolve();
        setTimeout(check, 2000);
      };
      check();
    });

    const stuck = this._sm.activeSessions().length;
    if (stuck > 0) log.gwarn(`TestWorkflow: hard timeout — ${stuck} session(s) still active`);
    this._printFinalReport();
  }

  _printStats() {
    const s = this._sm.stats();
    log.ginfo(`Stats: active=${s.active}  running=${s.running}  done=${s.done}  failed=${s.failed}  retrying=${s.retrying}`);
  }

  _printFinalReport() {
    const ok     = this._results.filter(r => r.ok).length;
    const failed = this._results.filter(r => !r.ok).length;
    log.ginfo(`─────────────────────────────────────────`);
    log.ginfo(`TestWorkflow COMPLETE`);
    log.ginfo(`  Total sessions: ${this._started}`);
    log.ginfo(`  Succeeded:      ${ok}`);
    log.ginfo(`  Failed:         ${failed}`);
    log.ginfo(`─────────────────────────────────────────`);
  }
}

module.exports = { TestWorkflow, ProxyPool, TemplatePool };
