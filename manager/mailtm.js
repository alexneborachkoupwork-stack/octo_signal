'use strict';

const https = require('https');

const BASE = 'https://api.mail.tm';

function _randomstring(len) {
  const chars = 'abcdefghijklmnopqrstuvwxyz0123456789';
  let s = '';
  for (let i = 0; i < len; i++) s += chars[Math.floor(Math.random() * chars.length)];
  return s;
}

function _request(method, path, body) {
  return new Promise((resolve, reject) => {
    const payload = body ? JSON.stringify(body) : null;
    const url = new URL(BASE + path);
    const opts = {
      hostname: url.hostname,
      path:     url.pathname + url.search,
      method,
      headers: { 'Content-Type': 'application/json' },
    };
    if (payload) opts.headers['Content-Length'] = Buffer.byteLength(payload);
    const req = https.request(opts, res => {
      let data = '';
      res.on('data', c => { data += c; });
      res.on('end', () => resolve({ status: res.statusCode, body: data }));
    });
    req.on('error', reject);
    if (payload) req.write(payload);
    req.end();
  });
}

async function _domain() {
  const r = await _request('GET', '/domains?page=1');
  if (r.status !== 200) throw new Error(`mail.tm /domains failed (${r.status})`);
  const list = JSON.parse(r.body);
  const domains = (list['hydra:member'] ?? list).filter(d => d.isActive !== false);
  if (!domains.length) throw new Error('No active mail.tm domains');
  return domains[Math.floor(Math.random() * domains.length)].domain;
}

/**
 * Creates a mail.tm account and returns { email, password, jwt }.
 * Retries up to `retries` times on transient failures.
 */
async function createAccount(retries = 5) {
  let lastErr;
  for (let i = 0; i < retries; i++) {
    try {
      const domain   = await _domain();
      const local    = _randomstring(10 + Math.floor(Math.random() * 5));
      const email    = `${local}@${domain}`;
      const password = _randomstring(16) + _randomstring(16);

      const cr = await _request('POST', '/accounts', { address: email, password });
      if (cr.status !== 201) throw new Error(`account creation failed (${cr.status}): ${cr.body}`);

      const tr = await _request('POST', '/token', { address: email, password });
      if (tr.status !== 200) throw new Error(`token request failed (${tr.status}): ${tr.body}`);

      const { token } = JSON.parse(tr.body);
      if (!token) throw new Error('mail.tm did not return JWT');

      return { email, password, jwt: token };
    } catch (err) {
      lastErr = err;
    }
  }
  throw lastErr;
}

module.exports = { createAccount };
