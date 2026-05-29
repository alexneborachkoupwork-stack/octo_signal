'use strict';

/**
 * Metrics — lightweight in-process counters for production observability.
 *
 * Exposed via GET /metrics on the hub server.
 * All values are reset on process restart (in-memory only).
 */
class Metrics {
  constructor() {
    this._startedAt = Date.now();

    // Worker distribution
    this.activeWorkers    = 0;
    this.warmedWorkers    = 0;
    this.selectingWorkers = 0;

    // Booking outcomes
    this.successfulBookings = 0;
    this.failedBookings     = 0;
    this.fallbackUsages     = 0; // times a fallback slot was used instead of primary

    // Latency (submit → confirmation)
    this._submitLatencySamples = [];

    // reCAPTCHA
    this.recaptchaSolves  = 0;
    this.recaptchaFails   = 0;

    // Proxy health  { proxyHost → { ok, fail } }
    this._proxyHealth = new Map();

    // Workflow distribution  { workflowName → count }
    this._workflowDist = new Map();

    // Session counts
    this.sessionsStarted  = 0;
    this.sessionsDone     = 0;
    this.sessionsFailed   = 0;
    this.sessionsRetrying = 0;
  }

  // ── Recording methods ────────────────────────────────────────────────────

  recordSessionStart(workflowName) {
    this.sessionsStarted++;
    this.activeWorkers++;
    this._workflowDist.set(workflowName, (this._workflowDist.get(workflowName) ?? 0) + 1);
  }

  recordSessionDone() {
    this.sessionsDone++;
    this.activeWorkers = Math.max(0, this.activeWorkers - 1);
  }

  recordSessionFailed() {
    this.sessionsFailed++;
    this.activeWorkers = Math.max(0, this.activeWorkers - 1);
  }

  recordSessionRetry() {
    this.sessionsRetrying++;
  }

  recordWarmup() {
    this.warmedWorkers++;
  }

  recordSelecting(delta = 1) {
    this.selectingWorkers = Math.max(0, this.selectingWorkers + delta);
  }

  recordBookingSuccess(usedFallback = false) {
    this.successfulBookings++;
    if (usedFallback) this.fallbackUsages++;
  }

  recordBookingFailure() {
    this.failedBookings++;
  }

  recordSubmitLatency(ms) {
    this._submitLatencySamples.push(ms);
    if (this._submitLatencySamples.length > 500) this._submitLatencySamples.shift();
  }

  recordRecaptcha(success) {
    if (success) this.recaptchaSolves++; else this.recaptchaFails++;
  }

  recordProxyResult(host, success) {
    if (!host) return;
    const e = this._proxyHealth.get(host) ?? { ok: 0, fail: 0 };
    if (success) e.ok++; else e.fail++;
    this._proxyHealth.set(host, e);
  }

  // ── Snapshot ─────────────────────────────────────────────────────────────

  snapshot(slotPool = null) {
    const slotStats  = slotPool?.stats() ?? null;
    const avgLatency = this._submitLatencySamples.length
      ? Math.round(this._submitLatencySamples.reduce((a, b) => a + b, 0) / this._submitLatencySamples.length)
      : null;
    const totalBookings = this.successfulBookings + this.failedBookings;
    const fallbackRate  = totalBookings > 0 ? Math.round((this.fallbackUsages / totalBookings) * 1000) / 1000 : 0;
    const totalRcp      = this.recaptchaSolves + this.recaptchaFails;
    const rcpSuccessRate = totalRcp > 0 ? Math.round((this.recaptchaSolves / totalRcp) * 1000) / 1000 : null;

    return {
      uptimeSeconds:       Math.floor((Date.now() - this._startedAt) / 1000),
      workers: {
        active:     this.activeWorkers,
        warmed:     this.warmedWorkers,
        selecting:  this.selectingWorkers,
        started:    this.sessionsStarted,
        done:       this.sessionsDone,
        failed:     this.sessionsFailed,
        retrying:   this.sessionsRetrying,
      },
      bookings: {
        successful:    this.successfulBookings,
        failed:        this.failedBookings,
        fallbackUsages: this.fallbackUsages,
        fallbackRate,
      },
      recaptcha: {
        solves:      this.recaptchaSolves,
        fails:       this.recaptchaFails,
        successRate: rcpSuccessRate,
      },
      latency: {
        avgSubmitMs: avgLatency,
        samples:     this._submitLatencySamples.length,
      },
      slotPool: slotStats,
      workflowDistribution: Object.fromEntries(this._workflowDist),
      proxyHealth:          Object.fromEntries(
        [...this._proxyHealth.entries()].map(([host, v]) => [host, v])
      ),
    };
  }
}

// Singleton — import once, share everywhere.
module.exports = new Metrics();
