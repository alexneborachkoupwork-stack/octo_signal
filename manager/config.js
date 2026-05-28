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
};
