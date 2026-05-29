'use strict';

const fs   = require('fs');
const path = require('path');
const { v4: uuidv4 } = require('uuid');
const log  = require('./logger');

// Canonical CSV header names (column order written on save)
const CSV_HEADERS = [
  'Account Id',
  'First Name',
  'Last Name',
  'Date of Birth',
  'Gender',
  'Nationality',
  'Passport Number',
  'User Name',
  'Password',
  'Email Address',
  'Registered Session Id',
  'Logged In',
  'Status',
  'File Name',
  'Worker Id',
  'Workflow',
  'Workflow State',
  'Started At',
  'Finished At',
  'Retry Count',
  'Terminated',
  'Failure Reason',
];

// Lowercase header → AccountRecord field name
const HEADER_TO_FIELD = {
  'account id':            'accountId',
  'first name':            'firstName',
  'last name':             'lastName',
  'date of birth':         'dateOfBirth',
  'gender':                'gender',
  'nationality':           'nationality',
  'passport number':       'passportNumber',
  'user name':             'username',
  'password':              'password',
  'email address':         'emailAddress',
  'registered session id': 'registeredSessionId',
  'logged in':             'loggedInAt',
  'status':                'status',
  'file name':             'fileName',
  'worker id':             'workerId',
  'workflow':              'workflow',
  'workflow state':        'workflowState',
  'started at':            'startedAt',
  'finished at':           'finishedAt',
  'retry count':           'retryCount',
  'terminated':            'terminated',
  'failure reason':        'failureReason',
};

const FIELD_TO_HEADER = Object.fromEntries(
  Object.entries(HEADER_TO_FIELD).map(([h, f]) => [f, h])
);

// ── Credential generator ──────────────────────────────────────────────────────

const CHARS_LOWER = 'abcdefghijklmnopqrstuvwxyz';
const CHARS_UPPER = 'ABCDEFGHIJKLMNOPQRSTUVWXYZ';
const CHARS_DIGIT = '0123456789';
const CHARS_SPEC  = '@#$!';

function _randomFrom(pool) {
  return pool[Math.floor(Math.random() * pool.length)];
}

function _normalize(str) {
  return str
    .toLowerCase()
    .normalize('NFD').replace(/[̀-ͯ]/g, '')
    .replace(/[^a-z0-9]/g, '');
}

/**
 * Generate a deterministic-ish username + random-suffix password for an account.
 * username: firstNameWord1.lastNameLastWord + passport_last4
 * password: 12-char mix — uppercase + lowercase + digit + special + random fill
 */
function generateCredentials(rec) {
  const fnWord  = _normalize(rec.firstName.trim().split(/\s+/)[0]);
  const lnWords = rec.lastName.trim().split(/\s+/);
  const lnWord  = _normalize(lnWords[lnWords.length - 1]);
  const ppSufx  = (rec.passportNumber ?? '').slice(-4).padStart(4, '0');

  const username = `${fnWord}.${lnWord}${ppSufx}`;

  // Build a 12-char password: 1 upper, 1 lower, 1 digit, 1 special, 8 random lower/digit
  const pool = CHARS_LOWER + CHARS_LOWER + CHARS_DIGIT + CHARS_DIGIT;
  let fill = '';
  for (let i = 0; i < 8; i++) fill += _randomFrom(pool);
  const password = [
    _randomFrom(CHARS_UPPER),
    _randomFrom(CHARS_LOWER),
    _randomFrom(CHARS_DIGIT),
    _randomFrom(CHARS_SPEC),
    ...fill,
  ].join('');

  return { username, password };
}

// ── CSV helpers ───────────────────────────────────────────────────────────────

function parseCsvLine(line) {
  const fields = [];
  let cur = '';
  let inQuote = false;
  for (let i = 0; i < line.length; i++) {
    const c = line[i];
    if (c === '"') {
      if (inQuote && line[i + 1] === '"') { cur += '"'; i++; }
      else inQuote = !inQuote;
    } else if (c === ',' && !inQuote) {
      fields.push(cur); cur = '';
    } else {
      cur += c;
    }
  }
  fields.push(cur);
  return fields;
}

function escapeCsvField(v) {
  const s = String(v ?? '');
  if (s.includes(',') || s.includes('"') || s.includes('\n')) {
    return '"' + s.replace(/"/g, '""') + '"';
  }
  return s;
}

// ── AccountStore ──────────────────────────────────────────────────────────────

/**
 * AccountStore — loads a CSV file into memory and flushes dirty rows periodically.
 *
 * Runtime truth is the in-memory Map. CSV is persistence only.
 */
class AccountStore {
  /**
   * @param {string} csvPath        - absolute or relative path to CSV file
   * @param {object} [opts]
   * @param {number} [opts.flushIntervalMs=5000]  - how often to flush dirty rows
   */
  constructor(csvPath, { flushIntervalMs = 5000 } = {}) {
    this._csvPath        = path.resolve(csvPath);
    this._flushIntervalMs = flushIntervalMs;
    this._accounts       = new Map();   // accountId → AccountRecord
    this._fileHeaders    = null;        // header row as read from file (preserves column order)
    this._flushTimer     = null;
  }

  // ── Load ─────────────────────────────────────────────────────────────────────

  load() {
    if (!fs.existsSync(this._csvPath)) {
      log.gwarn(`AccountStore: CSV not found at ${this._csvPath} — starting empty`);
      this._fileHeaders = CSV_HEADERS.slice();
      return;
    }
    const raw   = fs.readFileSync(this._csvPath, 'utf8');
    const lines = raw.split(/\r?\n/).filter(l => l.trim());
    if (lines.length === 0) {
      this._fileHeaders = CSV_HEADERS.slice();
      return;
    }

    this._fileHeaders = parseCsvLine(lines[0]);

    // Map column index → AccountRecord field name
    const colMap = {};
    for (let i = 0; i < this._fileHeaders.length; i++) {
      const key = this._fileHeaders[i].toLowerCase().trim();
      if (HEADER_TO_FIELD[key]) colMap[i] = HEADER_TO_FIELD[key];
    }

    let loaded = 0, skipped = 0;
    for (let r = 1; r < lines.length; r++) {
      const vals = parseCsvLine(lines[r]);
      const rec  = {};
      for (const [i, field] of Object.entries(colMap)) {
        rec[field] = vals[Number(i)] ?? '';
      }

      // Require at least one identity field to be non-empty
      if (!rec.firstName && !rec.lastName && !rec.passportNumber) {
        skipped++;
        continue;
      }

      if (!rec.accountId) rec.accountId = uuidv4();
      rec.retryCount = Number(rec.retryCount) || 0;
      rec.terminated = rec.terminated === 'true' || rec.terminated === '1' || rec.terminated === true;

      // SLOT_WAIT from a previous run — elapsed time unknown, re-queue immediately
      if (rec.status === 'SLOT_WAIT') { rec.status = 'PENDING'; }

      // RUNNING from a crashed/killed run — re-queue so the workflow restarts cleanly
      if (rec.status === 'RUNNING') { rec.status = 'PENDING'; }

      // Generate credentials at load time if absent
      if (!rec.username || !rec.password) {
        const creds = generateCredentials(rec);
        if (!rec.username) rec.username = creds.username;
        if (!rec.password) rec.password = creds.password;
        rec.isDirty = true; // persist generated credentials on next flush
      } else {
        rec.isDirty = false;
      }

      this._accounts.set(rec.accountId, rec);
      loaded++;
    }
    log.ginfo(`AccountStore: loaded ${loaded} accounts (${skipped} skipped) from ${this._csvPath}`);
  }

  // ── Queue helpers ─────────────────────────────────────────────────────────────

  /** All accounts not yet finished (not DONE, not TERMINATED). */
  getQueue() {
    return [...this._accounts.values()].filter(a =>
      !a.terminated && a.status !== 'DONE'
    );
  }

  /** Accounts currently in RUNNING state. */
  getRunning() {
    return [...this._accounts.values()].filter(a => a.status === 'RUNNING');
  }

  all() {
    return [...this._accounts.values()];
  }

  size() {
    return this._accounts.size;
  }

  // ── CRUD ──────────────────────────────────────────────────────────────────────

  get(accountId) {
    return this._accounts.get(accountId);
  }

  /**
   * Merge fields into an existing account and mark it dirty for the next flush.
   */
  update(accountId, fields) {
    const rec = this._accounts.get(accountId);
    if (!rec) {
      log.gwarn(`AccountStore: update called for unknown accountId=${accountId}`);
      return;
    }
    Object.assign(rec, fields, { isDirty: true });
  }

  // ── Persistence ───────────────────────────────────────────────────────────────

  startAutoFlush() {
    if (this._flushTimer) return;
    this._flushTimer = setInterval(() => this.flush(), this._flushIntervalMs);
    this._flushTimer.unref?.(); // don't keep Node alive just for flushing
  }

  stopAutoFlush() {
    if (this._flushTimer) { clearInterval(this._flushTimer); this._flushTimer = null; }
  }

  /** Write only dirty rows. Called on flush timer. */
  flush() {
    const dirty = [...this._accounts.values()].filter(a => a.isDirty);
    if (dirty.length === 0) return;
    this._writeAll();
    for (const a of dirty) a.isDirty = false;
    log.ginfo(`AccountStore: flushed ${dirty.length} dirty rows`);
  }

  /** Force-write all rows (used on shutdown). */
  saveAll() {
    this._writeAll();
    for (const a of this._accounts.values()) a.isDirty = false;
    log.ginfo(`AccountStore: saved all ${this._accounts.size} accounts to ${this._csvPath}`);
  }

  _writeAll() {
    const headers = this._fileHeaders ?? CSV_HEADERS.slice();
    const rows    = [headers.map(escapeCsvField).join(',')];

    for (const rec of this._accounts.values()) {
      const row = headers.map(h => {
        const field = HEADER_TO_FIELD[h.toLowerCase().trim()];
        const val   = field ? rec[field] : '';
        return escapeCsvField(val);
      });
      rows.push(row.join(','));
    }

    const content = rows.join('\n') + '\n';
    const tmp     = this._csvPath + '.tmp';
    fs.writeFileSync(tmp, content, 'utf8');
    fs.renameSync(tmp, this._csvPath);
  }
}

module.exports = AccountStore;
