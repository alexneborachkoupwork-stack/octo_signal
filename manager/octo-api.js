'use strict';

const path  = require('path');
const axios = require('axios');

// Octo Browser API — two transports:
//   Local  (port 58888): profile start/stop — no /api/v1 prefix
//   Cloud  (app.octobrowser.net): profile CRUD — /api/v2/automation
class OctoApi {
  constructor({ localUrl, cloudUrl, apiKey, extensionPath } = {}) {
    const headers = {
      'X-Octo-Api-Token': apiKey ?? '',
      'Content-Type': 'application/json',
    };
    this._local = axios.create({
      baseURL: localUrl ?? 'http://127.0.0.1:58888',
      headers,
      timeout: 30_000,
    });
    this._cloud = axios.create({
      baseURL: cloudUrl ?? 'https://app.octobrowser.net/api/v2/automation',
      headers,
      timeout: 30_000,
    });

    // Retry 429 responses with exponential backoff.
    // Octo Cloud API rate-limits profile CRUD; without this every session fails
    // immediately when the API is under load.
    this._cloud.interceptors.response.use(null, async (err) => {
      const status = err.response?.status;
      const cfg    = err.config;
      if (status !== 429) throw err;
      cfg._retries = (cfg._retries ?? 0) + 1;
      if (cfg._retries > 6) throw err;                   // give up after 6 retries
      const delay = Math.min(2000 * (2 ** (cfg._retries - 1)), 60_000); // 2s,4s,8s…60s
      await new Promise(r => setTimeout(r, delay));
      return this._cloud.request(cfg);
    });

    // Extension directory to load in every profile — forward slashes required for Chromium
    this._extPath = (extensionPath ?? path.join(__dirname, '..', 'extension'))
      .replace(/\\/g, '/');
  }

  // ── Profile CRUD ────────────────────────────────────────────────────────────

  /**
   * Find a profile by display name.
   * Lists all profiles via cloud API (UUIDs only) then fetches each detail in parallel.
   */
  async findProfileByName(name) {
    let page = 0;
    while (true) {
      const r = await this._cloud.get('/profiles', { params: { page } });
      const list = r.data?.data ?? [];
      if (!list.length) return null;

      // Fetch titles in parallel — cloud list only returns {uuid}
      const details = await Promise.all(
        list.map(p =>
          this._cloud.get(`/profiles/${p.uuid}`)
            .then(d => d.data?.data ?? null)
            .catch(() => null)
        )
      );
      const found = details.find(p => p && (p.title ?? p.name) === name);
      if (found) return found;

      const totalCount = r.data?.total_count ?? 0;
      if ((page + 1) * list.length >= totalCount) return null;
      page++;
    }
  }

  /**
   * Find all profiles that carry a given tag.
   * Pages through the full profile list and returns every matching profile.
   * @param {string} tag
   * @returns {Promise<Array<{uuid, title, tags}>>}
   */
  async findProfilesByTag(tag) {
    const found = [];
    let page = 0;
    while (true) {
      const r = await this._cloud.get('/profiles', { params: { page } });
      const list = r.data?.data ?? [];
      if (!list.length) break;

      const details = await Promise.all(
        list.map(p =>
          this._cloud.get(`/profiles/${p.uuid}`)
            .then(d => d.data?.data ?? null)
            .catch(() => null)
        )
      );
      for (const p of details) {
        if (!p) continue;
        const tags = p.tags ?? [];
        if (tags.includes(tag)) found.push(p);
      }

      const totalCount = r.data?.total_count ?? 0;
      if ((page + 1) * list.length >= totalCount) break;
      page++;
    }
    return found;
  }

  /**
   * Clone an existing profile by reading its config and creating a new profile with the same
   * proxy, fingerprint, and launch_args.  Octo has no native copy endpoint.
   * @param {string} templateUuid  - UUID of the profile to clone from
   * @param {string} [title]       - display name for the clone
   * @returns {Promise<{uuid: string}>}
   */
  async cloneProfile(templateUuid, title) {
    const tr = await this._cloud.get(`/profiles/${templateUuid}`);
    const tmpl = tr.data?.data ?? tr.data;
    const body = { title: title ?? `clone-${templateUuid.slice(0, 8)}`, tags: ['auto-session'] };

    if (tmpl.proxy) body.proxy = tmpl.proxy;
    // Always generate a fresh fingerprint per clone — passing only {os} causes Octo to
    // randomise canvas seed, AudioContext seed, screen geometry, etc. independently.
    // Sharing the template's full fingerprint makes all clones correlatable to Google Enterprise.
    body.fingerprint = { os: tmpl.fingerprint?.os ?? 'win' };
    // Copy Octo-managed extensions (installed via the UI, stored as "id@version" strings).
    if (Array.isArray(tmpl.extensions) && tmpl.extensions.length) {
      body.extensions = tmpl.extensions;
    }

    // Preserve all extensions the template has, then add ours if not already present.
    const baseArgs = Array.isArray(tmpl.launch_args) ? tmpl.launch_args : [];
    const otherArgs  = baseArgs.filter(a => !a.startsWith('--load-extension='));
    const extArgList = baseArgs.filter(a =>  a.startsWith('--load-extension='));
    // Flatten comma-separated paths from all --load-extension= args, then deduplicate.
    const tmplPaths  = extArgList.flatMap(a => a.slice('--load-extension='.length).split(',').filter(Boolean));
    const allPaths   = [...new Set([...tmplPaths, this._extPath])];
    body.launch_args = [...otherArgs, `--load-extension=${allPaths.join(',')}`];

    const cr = await this._cloud.post('/profiles', body);
    const created = cr.data?.data ?? cr.data;
    const uuid = created?.uuid ?? created?.id;
    if (!uuid) throw new Error(`cloneProfile: no UUID in create response: ${JSON.stringify(cr.data)}`);
    return { uuid };
  }

  /**
   * Create a new blank profile (used when no template is configured).
   * @param {object} opts
   * @param {string}   opts.name  - display title
   * @param {object}   [opts.proxy]
   * @param {string[]} [opts.tags]
   */
  async createProfile({ name, proxy, tags = ['auto-tester'] } = {}) {
    const body = {
      title:       name,
      tags,
      fingerprint: { os: 'win' },
      launch_args: [`--load-extension=${this._extPath}`],
    };

    if (proxy) {
      body.proxy = {
        type:     proxy.type     ?? 'socks5',
        host:     proxy.host,
        port:     Number(proxy.port),
        login:    proxy.login    ?? '',
        password: proxy.password ?? '',
      };
    }

    const r = await this._cloud.post('/profiles', body);
    return r.data.data ?? r.data;
  }

  /**
   * Start a profile (open Chromium).
   * Returns { uuid, port, wsUrl }.
   */
  async startProfile(uuid) {
    const r = await this._local.post('/api/profiles/start', {
      uuid,
      headless:   false,
      debug_port: true,
    });
    const data = r.data;
    return {
      uuid,
      port:  data.debug_port ?? data.port ?? null,
      wsUrl: data.ws_endpoint ?? null,
    };
  }

  /**
   * Stop a profile (close the Chromium window).
   */
  async stopProfile(uuid) {
    await this._local.post('/api/profiles/stop', { uuid }).catch(() => {});
  }

  /**
   * Delete a profile permanently (profile must be stopped first).
   */
  async deleteProfile(uuid) {
    await this._cloud.delete('/profiles', { data: { uuids: [uuid] } }).catch(() => {});
  }

  /**
   * Re-randomise fingerprint for an existing profile (profile must be stopped).
   */
  async renewFingerprint(uuid) {
    await this._cloud.patch(`/profiles/${uuid}`, { fingerprint: { os: 'win' } });
  }

  /**
   * Update proxy settings on an existing profile (profile must be stopped).
   */
  async updateProxy(uuid, proxy) {
    await this._cloud.patch(`/profiles/${uuid}`, {
      proxy: {
        type:     proxy.type     ?? 'socks5',
        host:     proxy.host,
        port:     Number(proxy.port),
        login:    proxy.login    ?? '',
        password: proxy.password ?? '',
      },
    });
  }

  // ── Browser automation helpers ──────────────────────────────────────────────

  /**
   * Open a new tab in the running profile via CDP and navigate to url.
   * @param {string|number} debugPort
   * @param {string}        url
   */
  async openTab(debugPort, url) {
    const r = await axios.put(
      `http://127.0.0.1:${debugPort}/json/new?${encodeURIComponent(url)}`,
      null,
      { timeout: 10_000 }
    );
    return r.data;
  }

  /**
   * Close every open tab in the running profile.
   * Prevents stale hub-init tabs (restored from Octo's saved tab state) from
   * triggering duplicate WORKER_INIT messages and the resulting WS reconnect loop.
   */
  async closeAllTabs(debugPort) {
    try {
      const r = await axios.get(`http://127.0.0.1:${debugPort}/json`, { timeout: 5_000 });
      const tabs = Array.isArray(r.data) ? r.data : [];
      // Only close regular page targets — never touch service_worker, extension, or
      // background_page targets (closing those kills the extension SW and wipes in-memory state).
      const pages = tabs.filter(t => t.type === 'page');
      await Promise.all(
        pages.map(t =>
          axios.get(`http://127.0.0.1:${debugPort}/json/close/${t.id}`, { timeout: 3_000 }).catch(() => {})
        )
      );
    } catch (_) {}
  }

  async navigateToWorkerInit(debugPort, url) {
    await this.closeAllTabs(debugPort);
    await this.openTab(debugPort, url);
  }
}

module.exports = OctoApi;
