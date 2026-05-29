'use strict';

const { EventEmitter } = require('events');
const log = require('./logger');

/**
 * CsvScheduler — dispatches `all-in-one` sessions driven by an AccountStore.
 *
 * Maintains up to maxSessions concurrent workers. When a worker finishes (success
 * or failure) the freed slot is immediately filled from the next PENDING account.
 * Runs until every account is DONE or TERMINATED.
 *
 * Events:
 *   'exhausted' ({ done, failed, total }) — all accounts processed
 */
class CsvScheduler extends EventEmitter {
  /**
   * @param {object}         opts
   * @param {AccountStore}   opts.accountStore
   * @param {SessionManager} opts.sessionManager
   * @param {HubServer}      opts.hubServer
   * @param {object}         opts.config
   * @param {object}         [opts.options]
   * @param {number}         [opts.options.maxSessions=50]
   * @param {number}         [opts.options.intervalMs=15000]   - stagger between launches
   * @param {string}         [opts.options.consulPost]
   * @param {string}         [opts.options.triggerMode='AUTO_TRIGGER']
   * @param {Array}          [opts.options.proxies=[]]
   * @param {Array}          [opts.options.templateUuids=[]]
   * @param {number}         [opts.options.maxWorkflowTimeoutMs]  - per-worker hard timeout
   * @param {number}         [opts.options.slotWaitMs]             - idle time after no-slots before retry
   * @param {number}         [opts.options.slotWaitCheckIntervalMs]- how often to poll slot-wait expiry
   */
  constructor({ accountStore, sessionManager, hubServer, config, options = {} }) {
    super();
    this._store    = accountStore;
    this._sm       = sessionManager;
    this._hub      = hubServer;
    this._config   = config;

    this._maxSessions          = options.maxSessions          ?? 50;
    this._intervalMs           = options.intervalMs           ?? 15_000;
    this._consulPost           = options.consulPost           ?? config.defaultConsulPost ?? '5084';
    this._triggerMode          = options.triggerMode          ?? 'AUTO_TRIGGER';
    this._proxies              = options.proxies              ?? [];
    this._templateUuids        = options.templateUuids        ?? [];
    this._maxWorkflowTimeoutMs = options.maxWorkflowTimeoutMs ?? config.maxWorkflowTimeoutMs ?? 8 * 60 * 60 * 1000;
    this._slotWaitMs           = options.slotWaitMs           ?? config.slotWaitMs           ?? 20 * 60 * 1000;
    this._slotWaitCheckMs      = options.slotWaitCheckIntervalMs ?? config.slotWaitCheckIntervalMs ?? 60_000;

    // accountId → { session, timeoutHandle }
    this._activeWorkers  = new Map();
    this._proxyIdx       = 0;
    this._templateIdx    = 0;
    this._stopped        = false;
    this._doneCount      = 0;
    this._failedCount    = 0;
    this._resolve        = null;
    this._slotWaitTimer  = null; // fires when a slot-wait account's delay expires

    // Wire hub messages for status enrichment (status-update, register-done, etc.)
    this._hub.on('message', (botId, msg) => this._onHubMessage(botId, msg));
  }

  // ── Public API ────────────────────────────────────────────────────────────────

  /**
   * Start the scheduler. Returns a Promise that resolves when all accounts are
   * DONE or TERMINATED.
   */
  run() {
    log.ginfo(`CsvScheduler: start  maxSessions=${this._maxSessions}  triggerMode=${this._triggerMode}`);

    // Re-queue any accounts stuck in RUNNING from a previous interrupted run
    for (const acc of this._store.getRunning()) {
      log.gwarn(`CsvScheduler: re-queuing stale RUNNING account  accountId=${acc.accountId}`);
      this._store.update(acc.accountId, { status: 'PENDING', workerId: '', workflowState: '' });
    }

    this._store.startAutoFlush();
    this._fillSlots();

    return new Promise((resolve) => {
      this._resolve = resolve;
      this._checkExhausted(); // handles zero-account edge case immediately
    });
  }

  stop() {
    this._stopped = true;
    this._stopSlotWaitTimer();
    for (const { session, timeoutHandle } of this._activeWorkers.values()) {
      clearTimeout(timeoutHandle);
      session.stop().catch(() => {});
    }
    this._activeWorkers.clear();
    this._store.stopAutoFlush();
    this._store.saveAll();
  }

  // ── Slot management ───────────────────────────────────────────────────────────

  _fillSlots() {
    if (this._stopped) return;

    const free = this._maxSessions - this._activeWorkers.size;
    if (free <= 0) return;

    // Accounts not yet active, not finished, and not currently waiting for slots
    const pending = this._store.getQueue().filter(a =>
      a.status !== 'RUNNING' && a.status !== 'SLOT_WAIT' && !this._activeWorkers.has(a.accountId)
    );

    const batch = pending.slice(0, free);
    if (batch.length === 0) return;

    // Launch sequentially with optional stagger; don't block the event loop
    (async () => {
      for (let i = 0; i < batch.length; i++) {
        if (this._stopped) break;
        await this._launchWorker(batch[i]);
        if (this._intervalMs > 0 && i < batch.length - 1) {
          await new Promise(r => setTimeout(r, this._intervalMs));
        }
      }
    })().catch(e => log.gerror('CsvScheduler: _fillSlots error:', e.message));
  }

  async _launchWorker(acc) {
    const proxy    = this._proxies.length
      ? this._proxies[this._proxyIdx++ % this._proxies.length]
      : null;
    const template = this._templateUuids.length
      ? this._templateUuids[this._templateIdx++ % this._templateUuids.length]
      : null;

    this._store.update(acc.accountId, {
      status:       'RUNNING',
      workerId:     '',       // filled once session is created
      startedAt:    new Date().toISOString(),
      workflow:     'all-in-one',
      workflowState: '',
    });

    const session = this._sm.createSession(
      'all-in-one',
      this._buildPayload(acc),
      proxy,
      null,
      template,
      { keepProfile: false },
    );

    this._store.update(acc.accountId, { workerId: session.sessionId });
    log.ginfo(`CsvScheduler: launched  accountId=${acc.accountId}  sessionId=${session.sessionId}`);

    // Hard timeout — terminate if worker runs too long
    const timeoutHandle = setTimeout(() => {
      if (!this._activeWorkers.has(acc.accountId)) return;
      log.gwarn(`CsvScheduler: timeout  accountId=${acc.accountId}  sessionId=${session.sessionId}`);
      this._store.update(acc.accountId, {
        status:        'TERMINATED',
        terminated:    true,
        failureReason: 'TIMEOUT',
        finishedAt:    new Date().toISOString(),
      });
      this._activeWorkers.delete(acc.accountId);
      session.stop().catch(() => {});
      this._onWorkerFinished();
    }, this._maxWorkflowTimeoutMs);

    this._activeWorkers.set(acc.accountId, { session, timeoutHandle });

    // ── Done handler ─────────────────────────────────────────────────────────
    session.once('done', (result) => {
      this._clearWorker(acc.accountId);
      this._doneCount++;
      this._store.update(acc.accountId, {
        status:        'DONE',
        workflowState: 'DONE',
        finishedAt:    new Date().toISOString(),
        fileName:      result.fileName ?? '',
      });
      log.ginfo(`CsvScheduler: done  accountId=${acc.accountId}  done=${this._doneCount}`);
      this._onWorkerFinished();
    });

    // ── Failed handler ───────────────────────────────────────────────────────
    session.once('failed', (reason) => {
      this._clearWorker(acc.accountId);

      // ── No-slots: idle and retry, don't burn retryCount ──────────────────
      const noSlots = /no available slots|no.*slot.*found|calendar did not appear/i.test(reason);
      if (noSlots) {
        const waitMins = Math.round(this._slotWaitMs / 60_000);
        log.gwarn(`CsvScheduler: no-slots — idling ${waitMins}min before retry  accountId=${acc.accountId}`);
        // Store retry timestamp in-memory (_slotRetryAt is not a CSV column, won't be persisted)
        const record = this._store.get(acc.accountId);
        if (record) record._slotRetryAt = Date.now() + this._slotWaitMs;
        this._store.update(acc.accountId, {
          status:        'SLOT_WAIT',
          workerId:      '',
          workflowState: 'SLOT_WAIT',
          failureReason: reason,
        });
        this._startSlotWaitTimer();
        this._onWorkerFinished();
        return;
      }

      this._failedCount++;

      // Auth/config errors are not retryable — terminate immediately
      const fatal = /40[13]|unauthorized|forbidden|invalid.*key/i.test(reason);

      // Re-read latest acc state (may have been updated by hub messages)
      const latest     = this._store.get(acc.accountId) ?? acc;
      const retryCount = (Number(latest.retryCount) || 0) + 1;
      const maxRetries = this._config.maxRetries ?? 2;

      if (!fatal && retryCount <= maxRetries) {
        log.gwarn(`CsvScheduler: failed (retry ${retryCount}/${maxRetries})  accountId=${acc.accountId}  reason=${reason}`);
        this._store.update(acc.accountId, {
          status:        'PENDING',
          retryCount,
          failureReason: reason,
          workerId:      '',
          workflowState: '',
        });
      } else {
        if (fatal) log.gerror(`CsvScheduler: terminated (fatal)  accountId=${acc.accountId}  reason=${reason}`);
        else       log.gerror(`CsvScheduler: terminated  accountId=${acc.accountId}  reason=${reason}`);
        this._store.update(acc.accountId, {
          status:        'TERMINATED',
          terminated:    true,
          failureReason: reason,
          finishedAt:    new Date().toISOString(),
          retryCount,
        });
      }
      this._onWorkerFinished();
    });

    await session.start();
  }

  _clearWorker(accountId) {
    const entry = this._activeWorkers.get(accountId);
    if (entry) clearTimeout(entry.timeoutHandle);
    this._activeWorkers.delete(accountId);
  }

  _onWorkerFinished() {
    if (this._stopped) return;
    if (this._checkExhausted()) return;
    this._fillSlots();
  }

  _checkExhausted() {
    // Accounts that can be launched right now
    const launchable = this._store.getQueue().filter(a =>
      a.status !== 'RUNNING' && a.status !== 'SLOT_WAIT' && !this._activeWorkers.has(a.accountId)
    );
    if (launchable.length > 0 || this._activeWorkers.size > 0) return false;

    // Slot-waiting accounts are still alive — not exhausted yet
    const slotWaiting = this._store.all().filter(a => a.status === 'SLOT_WAIT');
    if (slotWaiting.length > 0) {
      log.ginfo(`CsvScheduler: all active workers idle — ${slotWaiting.length} account(s) in SLOT_WAIT`);
      return false;
    }

    const total = this._store.size();
    log.ginfo(`CsvScheduler: exhausted  done=${this._doneCount}  failed=${this._failedCount}  total=${total}`);
    this._stopSlotWaitTimer();
    this._store.stopAutoFlush();
    this._store.saveAll();

    if (this._resolve) {
      const result = { done: this._doneCount, failed: this._failedCount, total };
      this._resolve(result);
      this._resolve = null;
      this.emit('exhausted', result);
    }
    return true;
  }

  // ── Slot-wait timer ───────────────────────────────────────────────────────────

  _startSlotWaitTimer() {
    if (this._slotWaitTimer) return;
    this._slotWaitTimer = setInterval(() => this._checkSlotWaits(), this._slotWaitCheckMs);
    this._slotWaitTimer.unref?.();
  }

  _stopSlotWaitTimer() {
    if (this._slotWaitTimer) { clearInterval(this._slotWaitTimer); this._slotWaitTimer = null; }
  }

  _checkSlotWaits() {
    if (this._stopped) return;
    const now = Date.now();
    let woke  = 0;
    for (const acc of this._store.all()) {
      if (acc.status !== 'SLOT_WAIT') continue;
      // _slotRetryAt is in-memory only; if missing (e.g. restart) treat as expired
      if (acc._slotRetryAt && acc._slotRetryAt > now) continue;
      log.ginfo(`CsvScheduler: slot-wait expired, re-queuing  accountId=${acc.accountId}`);
      acc._slotRetryAt = null;
      this._store.update(acc.accountId, { status: 'PENDING', workerId: '', workflowState: '' });
      woke++;
    }
    if (woke > 0) this._fillSlots();

    // Stop timer if no more slot-waiters
    if (!this._store.all().some(a => a.status === 'SLOT_WAIT')) {
      this._stopSlotWaitTimer();
    }
  }

  // ── Hub message enrichment ────────────────────────────────────────────────────

  _onHubMessage(botId, msg) {
    // Find account by workerId (= sessionId = botId)
    const acc = this._accountByWorkerId(botId);
    if (!acc) return;
    const accountId = acc.accountId;

    switch (msg.type) {
      case 'status-update':
        this._store.update(accountId, { workflowState: msg.state, status: 'RUNNING' });
        break;

      case 'register-done':
        if (msg.ok !== false) {
          this._store.update(accountId, {
            registeredSessionId: msg.sessionId ?? botId,
            username:            msg.username  ?? acc.username,
            password:            msg.password  ?? acc.password,
            emailAddress:        msg.email     ?? acc.emailAddress,
            status:              'REGISTERED',
            workflowState:       'REGISTERED',
          });
        }
        break;

      case 'warmup-done':
        if (msg.ok !== false) {
          this._store.update(accountId, {
            loggedInAt:    new Date().toISOString(),
            status:        'LOGGED_IN',
            workflowState: 'READY_FOR_APPLY_IDLE',
          });
        }
        break;

      case 'apply-done':
        if (msg.ok !== false) {
          this._store.update(accountId, {
            fileName:      msg.fileName ?? '',
            finishedAt:    new Date().toISOString(),
            status:        'DONE',
            workflowState: 'DONE',
          });
        }
        break;
    }
  }

  _accountByWorkerId(sessionId) {
    for (const acc of this._store.all()) {
      if (acc.workerId === sessionId) return acc;
    }
    return null;
  }

  // ── Payload builder ───────────────────────────────────────────────────────────

  _buildPayload(acc) {
    return {
      realPerson: {
        firstName:      acc.firstName      ?? '',
        lastName:       acc.lastName       ?? '',
        dateOfBirth:    acc.dateOfBirth    ?? '',
        gender:         acc.gender         ?? '',
        nationality:    acc.nationality    ?? '',
        passportNumber: acc.passportNumber ?? '',
        username:       acc.username       ?? '',
        password:       acc.password       ?? '',
      },
      emailAddress:  acc.emailAddress ?? undefined,
      consulPost:    this._consulPost,
      triggerMode:   this._triggerMode,
      captchaSolver: 'capsolver',
    };
  }
}

module.exports = CsvScheduler;
