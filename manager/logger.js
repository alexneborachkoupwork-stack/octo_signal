'use strict';

const fs   = require('fs');
const path = require('path');

const LOG_DIR = path.join(__dirname, '..', 'logs', 'manager');
fs.mkdirSync(LOG_DIR, { recursive: true });

function ts() {
  return new Date().toISOString();
}

function fmt(level, sessionId, ...args) {
  const sid = sessionId ? `[${sessionId.slice(0, 8)}]` : '[global  ]';
  return `${ts()}  ${level}  ${sid}  ${args.map(a =>
    typeof a === 'object' ? JSON.stringify(a) : String(a)
  ).join(' ')}`;
}

// Per-session file streams, keyed by sessionId
const _streams = new Map();

function _stream(sessionId) {
  if (!sessionId) return null;
  if (_streams.has(sessionId)) return _streams.get(sessionId);
  const file = path.join(LOG_DIR, `session_${sessionId}.log`);
  const s = fs.createWriteStream(file, { flags: 'a' });
  _streams.set(sessionId, s);
  return s;
}

function _write(level, sessionId, ...args) {
  const line = fmt(level, sessionId, ...args);
  // fs.writeSync bypasses Node's stdout stream buffer — output appears immediately
  // even when stdout is piped through npm on Windows.
  fs.writeSync(1, line + '\n');
  const s = _stream(sessionId);
  if (s) s.write(line + '\n');
}

module.exports = {
  info:  (sessionId, ...a) => _write('INFO ', sessionId, ...a),
  warn:  (sessionId, ...a) => _write('WARN ', sessionId, ...a),
  error: (sessionId, ...a) => _write('ERROR', sessionId, ...a),
  debug: (sessionId, ...a) => _write('DEBUG', sessionId, ...a),

  // Convenience: global (no session context)
  ginfo:  (...a) => _write('INFO ', null, ...a),
  gwarn:  (...a) => _write('WARN ', null, ...a),
  gerror: (...a) => _write('ERROR', null, ...a),

  closeSession(sessionId) {
    const s = _streams.get(sessionId);
    if (s) { s.end(); _streams.delete(sessionId); }
  },
};
