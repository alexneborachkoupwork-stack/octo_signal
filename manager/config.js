'use strict';

module.exports = {
  // Hub WebSocket + HTTP server port
  port: Number(process.env.HUB_PORT ?? 9000),

  // Octo Browser local API (start/stop — no path suffix)
  octoLocalUrl: process.env.OCTO_API_URL  ?? 'http://127.0.0.1:58888',
  // Octo Browser cloud API (profile CRUD)
  octoCloudUrl: process.env.OCTO_CLOUD_URL ?? 'https://app.octobrowser.net/api/v2/automation',
  octoApiKey:   process.env.OCTO_API_KEY   ?? '',


  // How many times to retry a session on proxy-burn or device-flag before giving up
  maxRetries: Number(process.env.MAX_RETRIES ?? 2),

  // Seconds to wait for a profile's browser to start before declaring it failed
  browserStartTimeoutMs: Number(process.env.BROWSER_START_TIMEOUT_MS ?? 30_000),

  // Seconds to wait for the extension to connect after browser starts
  extensionConnectTimeoutMs: Number(process.env.EXT_CONNECT_TIMEOUT_MS ?? 60_000),

  // Default consular post ID
  defaultConsulPost: process.env.CONSUL_POST ?? '5084',

  // Template profile tag — profiles with this tag are cloned for each session.
  // Override with --template <uuid> on the CLI to use a single specific profile.
  octoTemplateTag: process.env.OCTO_TEMPLATE_TAG ?? 'template',

  // ── CSV runtime workflow ───────────────────────────────────────────────────

  // Path to the accounts CSV file (relative to cwd or absolute)
  accountsCsvPath: process.env.ACCOUNTS_CSV ?? './accounts.csv',

  // How often (ms) to flush dirty CSV rows to disk
  csvFlushIntervalMs: Number(process.env.CSV_FLUSH_INTERVAL_MS ?? 5_000),

  // Per-worker hard timeout — worker is terminated if it runs longer than this
  maxWorkflowTimeoutMs: Number(process.env.MAX_WORKFLOW_TIMEOUT_MS ?? 8 * 60 * 60 * 1000),

  // Heartbeat interval for dead-session detection (ms)
  workerHeartbeatMs: Number(process.env.WORKER_HEARTBEAT_MS ?? 15_000),

  // A worker that misses this many ms of heartbeat is considered dead
  workerDeadThresholdMs: Number(process.env.WORKER_DEAD_THRESHOLD_MS ?? 60_000),

  // ── Slot-wait (no available slots) ────────────────────────────────────────

  // How long to idle before retrying after a "no available slots" failure.
  // Does NOT count against maxRetries — the account simply waits.
  slotWaitMs: Number(process.env.SLOT_WAIT_MS ?? 20 * 60 * 1000),

  // How often to poll for slot-wait accounts whose timer has expired.
  slotWaitCheckIntervalMs: Number(process.env.SLOT_WAIT_CHECK_INTERVAL_MS ?? 60_000),
};
