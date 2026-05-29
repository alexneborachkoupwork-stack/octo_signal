'use strict';

/**
 * SlotPool — real-time slot intelligence pool.
 *
 * Tracks slot observations from all workers, maintains confidence scores,
 * and assigns the best available primary + fallback slots on request.
 *
 * Slot key format: `${postId}-${date}-${time}`  e.g. "5084-2026-06-15-09:00"
 */

const CONFIDENCE_INIT     = 1;
const CONFIDENCE_MAX      = 10;
const CONFIDENCE_DECAY    = 0.4;   // subtracted when a slot is absent from an observation
const STALE_MS            = 10 * 60 * 1000;  // 10 min — gc threshold for consumed entries
const LIKELY_CONSUMED_THR = 3;               // failureCount before status = likely_consumed
const MAX_ASSIGNMENTS     = 4;              // max concurrent assignedSessions before deprioritising

class SlotPool {
  constructor() {
    /** @type {Map<string, SlotPoolEntry>} */
    this._pool = new Map();
    this._version = 0;

    // GC every 5 minutes
    this._gcTimer = setInterval(() => this.gc(), 5 * 60 * 1000);
  }

  // ── Public API ─────────────────────────────────────────────────────────────

  /**
   * Record an observation from a worker.
   * @param {string}   workerId
   * @param {number}   postId
   * @param {{date:string, time:string}[]} slots  — all slots the worker can currently see
   */
  observe(workerId, postId, slots) {
    const now  = Date.now();
    const seen = new Set();

    for (const { date, time } of slots) {
      const key = _key(postId, date, time);
      seen.add(key);

      if (this._pool.has(key)) {
        const e = this._pool.get(key);
        if (e.status === 'consumed') continue;
        e.observedCount++;
        if (!e.observedBy.includes(workerId)) e.observedBy.push(workerId);
        e.confidenceScore = Math.min(CONFIDENCE_MAX, e.confidenceScore + 1);
        if (e.status === 'stale' || e.status === 'likely_consumed') e.status = 'available';
        e.lastObservedAt = now;
        e.version        = ++this._version;
      } else {
        /** @type {SlotPoolEntry} */
        const entry = {
          slotKey:          key,
          postId,
          date,
          time,
          status:           'available',
          confidenceScore:  CONFIDENCE_INIT,
          observedCount:    1,
          observedBy:       [workerId],
          assignedSessions: [],
          lastObservedAt:   now,
          lastAssignedAt:   undefined,
          successCount:     0,
          failureCount:     0,
          version:          ++this._version,
        };
        this._pool.set(key, entry);
      }
    }

    // Decay confidence for slots from this postId that were absent in this observation
    for (const [key, e] of this._pool) {
      if (e.postId !== postId)           continue;
      if (e.status === 'consumed')       continue;
      if (seen.has(key))                 continue;
      e.confidenceScore = Math.max(0, e.confidenceScore - CONFIDENCE_DECAY);
      if (e.confidenceScore === 0 && e.status === 'available') {
        e.status = 'stale';
        e.version = ++this._version;
      }
    }
  }

  /**
   * Assign the best primary slot + up to 3 fallbacks to a worker.
   * Only considers slots visible to this worker (visibleSlots).
   * @param {string}   workerId
   * @param {number}   postId
   * @param {{date:string, time:string}[]} visibleSlots
   * @returns {{ primary: SlotInfo|null, fallbacks: SlotInfo[], version: number }}
   */
  assign(workerId, postId, visibleSlots) {
    if (!visibleSlots || visibleSlots.length === 0) {
      return { primary: null, fallbacks: [], version: this._version };
    }

    // Rank visible slots by score
    const ranked = visibleSlots
      .map(({ date, time }) => {
        const key = _key(postId, date, time);
        const e   = this._pool.get(key);
        return { key, date, time, entry: e };
      })
      .filter(({ entry }) => !entry || entry.status !== 'consumed')
      .sort((a, b) => _score(b.entry) - _score(a.entry));

    const assigned = [];
    for (const { key, date, time, entry } of ranked) {
      if (assigned.length >= 4) break;
      if (entry) {
        entry.assignedSessions.push(workerId);
        entry.lastAssignedAt = Date.now();
        if (entry.status === 'available') entry.status = 'consuming';
        entry.version = ++this._version;
      }
      assigned.push({ slotKey: key, postId, date, time });
    }

    const [primary = null, ...fallbacks] = assigned;
    return { primary, fallbacks, version: this._version };
  }

  /**
   * Mark a slot as successfully booked.
   * @param {string} slotKey
   */
  reportSuccess(slotKey) {
    const e = this._pool.get(slotKey);
    if (!e) return;
    e.status        = 'consumed';
    e.successCount++;
    e.version       = ++this._version;
  }

  /**
   * Record a booking failure for a slot.
   * @param {string} slotKey
   * @param {string} [reason]
   */
  reportFailure(slotKey, reason) {
    const e = this._pool.get(slotKey);
    if (!e) return;
    e.failureCount++;
    e.confidenceScore = Math.max(0, e.confidenceScore - 2);
    if (e.failureCount >= LIKELY_CONSUMED_THR) {
      e.status = 'likely_consumed';
    } else if (e.status === 'consuming') {
      e.status = 'available';
    }
    e.version = ++this._version;
  }

  /**
   * Remove a worker from all slot assignment lists (called on session end).
   * @param {string} workerId
   */
  releaseWorker(workerId) {
    for (const e of this._pool.values()) {
      const idx = e.assignedSessions.indexOf(workerId);
      if (idx !== -1) {
        e.assignedSessions.splice(idx, 1);
        if (e.status === 'consuming' && e.assignedSessions.length === 0) {
          e.status = 'available';
        }
        e.version = ++this._version;
      }
    }
  }

  /** Remove stale/consumed entries older than STALE_MS. */
  gc() {
    const cutoff = Date.now() - STALE_MS;
    for (const [key, e] of this._pool) {
      if ((e.status === 'consumed' || e.status === 'stale') && e.lastObservedAt < cutoff) {
        this._pool.delete(key);
      }
    }
  }

  /** Summary stats for observability. */
  stats() {
    let available = 0, consuming = 0, consumed = 0, stale = 0, likelyConsumed = 0;
    let totalConf = 0;
    for (const e of this._pool.values()) {
      totalConf += e.confidenceScore;
      if (e.status === 'available')        available++;
      else if (e.status === 'consuming')   consuming++;
      else if (e.status === 'consumed')    consumed++;
      else if (e.status === 'stale')       stale++;
      else if (e.status === 'likely_consumed') likelyConsumed++;
    }
    const n = this._pool.size;
    return {
      poolSize:       n,
      available,
      consuming,
      consumed,
      stale,
      likelyConsumed,
      avgConfidence:  n > 0 ? Math.round((totalConf / n) * 100) / 100 : 0,
      version:        this._version,
    };
  }

  /** Clean shutdown — clear GC timer. */
  destroy() {
    clearInterval(this._gcTimer);
  }
}

// ── Helpers ──────────────────────────────────────────────────────────────────

function _key(postId, date, time) {
  return `${postId}-${date}-${time}`;
}

/**
 * Score a slot entry for ranking.
 * Higher is better. Null entry (first time seen) gets a neutral score.
 */
function _score(entry) {
  if (!entry) return 0;
  // Penalty for heavy assignment load
  const assignmentPenalty = Math.max(0, entry.assignedSessions.length - MAX_ASSIGNMENTS) * 3;
  return entry.confidenceScore - entry.failureCount * 2 - assignmentPenalty;
}

module.exports = { SlotPool };
