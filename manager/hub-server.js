'use strict';

const http       = require('http');
const { URL }    = require('url');
const { EventEmitter } = require('events');
const { WebSocketServer } = require('ws');
const log = require('./logger');

// Minimal HTML page served at /*/worker-init?botId=<uuid>
// The extension's hub-init.js content script fires on any URL containing "worker-init",
// extracts botId from the query string, and sends WORKER_INIT to the service worker.
const WORKER_INIT_HTML = `<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>Hub Init</title></head>
<body>
  <p id="hub-status">Initializing session…</p>
  <p id="hub-bot-id"></p>
  <script>
    const p = new URLSearchParams(location.search);
    const botId = p.get('botId') || p.get('profileId') || '';
    document.getElementById('hub-bot-id').textContent = 'Session: ' + botId;
  </script>
</body></html>`;

// HubServer - combined HTTP + WebSocket server.
//
// HTTP: GET /worker-init?botId={botId}
//   returns WORKER_INIT_HTML so the extension content script fires
//
// WS: ws://HOST:PORT/ext?botId={botId}
//   accepts connections from extension service workers
//
// Events emitted:
//   'connected'    (botId, ws)  - new extension connected
//   'message'      (botId, msg) - JSON message received from extension
//   'disconnected' (botId)      - extension disconnected
class HubServer extends EventEmitter {
  constructor(config) {
    super();
    this._port    = config.port ?? 9000;
    this._sockets = new Map(); // botId → ws
    this._httpServer = null;
    this._wss        = null;
    this._pingInterval = null;
    this._slotPool   = null; // set by setSlotPool() after construction
  }

  /** Attach the shared SlotPool so /metrics can include slot stats. */
  setSlotPool(pool) {
    this._slotPool = pool;
  }

  start() {
    return new Promise((resolve) => {
      // ── HTTP server ────────────────────────────────────────────────────────
      this._httpServer = http.createServer((req, res) => {
        const url = new URL(req.url, `http://localhost:${this._port}`);

        if (url.pathname.includes('worker-init')) {
          res.writeHead(200, { 'Content-Type': 'text/html; charset=utf-8' });
          res.end(WORKER_INIT_HTML);
          return;
        }

        if (url.pathname === '/metrics') {
          try {
            const metrics  = require('./metrics');
            const slotPool = this._slotPool ?? null;
            const body = JSON.stringify(metrics.snapshot(slotPool), null, 2);
            res.writeHead(200, { 'Content-Type': 'application/json' });
            res.end(body);
          } catch (e) {
            res.writeHead(500);
            res.end(JSON.stringify({ error: e.message }));
          }
          return;
        }

        res.writeHead(404);
        res.end('Not found');
      });

      // ── WebSocket server (upgrade on /ext path) ────────────────────────────
      this._wss = new WebSocketServer({ server: this._httpServer });

      this._wss.on('connection', (ws, req) => {
        const url = new URL(req.url, `http://localhost:${this._port}`);

        if (!url.pathname.endsWith('/ext')) {
          log.gwarn(`HubServer: WS connection on unexpected path "${url.pathname}" — closing`);
          ws.close(1008, 'use /ext');
          return;
        }

        const botId = url.searchParams.get('botId') ?? url.searchParams.get('profileId') ?? null;

        if (!botId) {
          log.gwarn('WS connection without botId — closing');
          ws.close(1008, 'botId required');
          return;
        }

        // If there's already a socket for this botId (reconnect), close the old one
        if (this._sockets.has(botId)) {
          const old = this._sockets.get(botId);
          try { old.close(1001, 'replaced by reconnect'); } catch (_) {}
        }

        ws.isAlive = true;
        ws.on('pong', () => { ws.isAlive = true; });

        this._sockets.set(botId, ws);
        log.ginfo(`HubServer: extension connected  botId=${botId}`);
        this.emit('connected', botId, ws);

        ws.on('message', (raw) => {
          let msg;
          try { msg = JSON.parse(raw); } catch (_) { return; }
          this.emit('message', botId, msg);
        });

        ws.on('close', () => {
          if (this._sockets.get(botId) === ws) {
            this._sockets.delete(botId);
          }
          log.ginfo(`HubServer: extension disconnected botId=${botId}`);
          this.emit('disconnected', botId);
        });

        ws.on('error', (err) => {
          log.gwarn(`HubServer: WS error botId=${botId}`, err.message);
        });
      });

      this._httpServer.listen(this._port, () => {
        log.ginfo(`HubServer: listening on http/ws port ${this._port}`);

        // Send WS protocol-level ping frames every 5 s.
        // Chrome auto-responds with pong frames, preventing the browser from
        // treating the WS as idle and terminating the service worker.
        // Sockets that miss two consecutive pings (>10 s silent) are terminated.
        this._pingInterval = setInterval(() => {
          for (const [botId, ws] of this._sockets.entries()) {
            if (!ws.isAlive) {
              log.gwarn(`HubServer: ping timeout  botId=${botId} — closing`);
              ws.terminate();
              continue;
            }
            ws.isAlive = false;
            ws.ping();
          }
        }, 5000);

        resolve();
      });
    });
  }

  stop() {
    return new Promise((resolve) => {
      if (this._pingInterval) { clearInterval(this._pingInterval); this._pingInterval = null; }
      for (const ws of this._sockets.values()) {
        try { ws.close(1001, 'server shutting down'); } catch (_) {}
      }
      this._sockets.clear();
      this._wss?.close();
      this._httpServer?.close(() => resolve());
    });
  }

  /**
   * Send a JSON message to a specific session by botId.
   * Silently drops if that session is not connected.
   */
  send(botId, msg) {
    const ws = this._sockets.get(botId);
    if (!ws || ws.readyState !== 1 /* OPEN */) return;
    try {
      ws.send(JSON.stringify(msg));
    } catch (err) {
      log.gwarn(`HubServer: send error botId=${botId}`, err.message);
    }
  }

  /**
   * Broadcast a JSON message to all connected sessions.
   */
  broadcast(msg) {
    const payload = JSON.stringify(msg);
    for (const [botId, ws] of this._sockets.entries()) {
      if (ws.readyState === 1) {
        try { ws.send(payload); } catch (_) {}
      }
    }
  }

  isConnected(botId) {
    const ws = this._sockets.get(botId);
    return ws != null && ws.readyState === 1;
  }

  connectedBotIds() {
    return [...this._sockets.keys()].filter(id => this.isConnected(id));
  }

  /** Worker-init URL to navigate the profile to */
  workerInitUrl(botId) {
    return `http://localhost:${this._port}/worker-init?botId=${encodeURIComponent(botId)}`;
  }
}

module.exports = HubServer;
