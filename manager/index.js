'use strict';

/**
 * Octo Manager — CLI entry point.
 *
 * Usage:
 *   node index.js [options]
 *
 * Options:
 *   --workflow    <name>   test | register | warmup | apply  (default: test)
 *   --max-sessions <n>     max concurrent sessions           (default: 50)
 *   --interval    <ms>     ms between session starts         (default: 45000)
 *   --consul-post <id>     consular post ID                  (default: 5084)
 *   --proxies     <file>   path to JSON proxy list           (optional)
 *   --port        <n>      hub WebSocket port                (default: 9000)
 *   --octo-api-key <key>   Octo Browser API token            (default: env)
 *   --template    <uuid>   template profile UUID             (default: env)
 *
 * Environment:
 *   HUB_PORT, OCTO_API_URL, OCTO_API_KEY, TEMPLATE_PROFILE,
 *   MAX_RETRIES, BROWSER_START_TIMEOUT_MS, EXT_CONNECT_TIMEOUT_MS
 */

const fs   = require('fs');
const path = require('path');

const cfg            = require('./config');
const log            = require('./logger');
const OctoApi        = require('./octo-api');
const HubServer      = require('./hub-server');
const SessionManager = require('./session-manager');
const { SlotPool }     = require('./slot-pool');
const { WorkflowChain } = require('./workflow-chain');
const { TestWorkflow } = require('./workflows/test-workflow');

// ── CLI argument parser ───────────────────────────────────────────────────────

function parseArgs(argv) {
  const args = {};
  for (let i = 2; i < argv.length; i++) {
    const key = argv[i];
    if (key.startsWith('--')) {
      const name = key.slice(2);
      const val  = argv[i + 1] && !argv[i + 1].startsWith('--') ? argv[++i] : true;
      args[name] = val;
    }
  }
  return args;
}

// ── Bootstrap ─────────────────────────────────────────────────────────────────

async function main() {
  const args = parseArgs(process.argv);

  // Override config from CLI
  if (args.port)        cfg.port           = Number(args.port);
  if (args['octo-api-key']) cfg.octoApiKey = args['octo-api-key'];
  if (args.template)    cfg.templateProfile = args.template;

  const workflow    = args.workflow     ?? 'test';
  const maxSessions = Number(args['max-sessions'] ?? 50);
  const intervalMs  = Number(args.interval         ?? 15_000);
  const consulPost  = args['consul-post']           ?? cfg.defaultConsulPost;

  // Load proxy pool from file if specified
  let proxies = [];
  if (args.proxies) {
    try {
      proxies = JSON.parse(fs.readFileSync(path.resolve(args.proxies), 'utf8'));
      log.ginfo(`Loaded ${proxies.length} proxies from ${args.proxies}`);
    } catch (err) {
      log.gerror('Failed to load proxy file:', err.message);
      process.exit(1);
    }
  }

  // ── Resolve template profiles ─────────────────────────────────────────────
  // CLI --template <uuid> takes priority; otherwise discover by tag from Cloud API.
  let templateUuids = [];
  if (cfg.templateProfile) {
    templateUuids = [cfg.templateProfile];
  } else if (cfg.octoTemplateTag) {
    try {
      const octoApiTmp = new OctoApi({ localUrl: cfg.octoLocalUrl, cloudUrl: cfg.octoCloudUrl, apiKey: cfg.octoApiKey });
      const profiles = await octoApiTmp.findProfilesByTag(cfg.octoTemplateTag);
      templateUuids = profiles.map(p => p.uuid);
    } catch (err) {
      log.gwarn(`Failed to resolve template profiles by tag "${cfg.octoTemplateTag}": ${err.message}`);
    }
  }

  log.ginfo('─────────────────────────────────────────────────────');
  log.ginfo('Octo Manager starting');
  log.ginfo(`  workflow:     ${workflow}`);
  log.ginfo(`  maxSessions:  ${maxSessions}`);
  log.ginfo(`  intervalMs:   ${intervalMs}`);
  log.ginfo(`  consulPost:   ${consulPost}`);
  log.ginfo(`  port:         ${cfg.port}`);
  log.ginfo(`  octoLocalUrl: ${cfg.octoLocalUrl}`);
  log.ginfo(`  proxies:      ${proxies.length}`);
  log.ginfo(`  templates:    ${templateUuids.length ? templateUuids.join(', ') : 'none (blank profile)'}`);
  log.ginfo('─────────────────────────────────────────────────────');

  // ── Initialise layers ──────────────────────────────────────────────────────

  const octoApi        = new OctoApi({ localUrl: cfg.octoLocalUrl, cloudUrl: cfg.octoCloudUrl, apiKey: cfg.octoApiKey });
  const hubServer      = new HubServer(cfg);
  const slotPool       = new SlotPool();
  hubServer.setSlotPool(slotPool);
  const sessionManager = new SessionManager({ config: cfg, octoApi, hubServer, slotPool });

  await hubServer.start();
  log.ginfo(`Hub server ready on ws://localhost:${cfg.port}/ext`);

  // ── Graceful shutdown ──────────────────────────────────────────────────────

  async function shutdown(signal) {
    log.ginfo(`\nReceived ${signal} — shutting down`);
    await sessionManager.stopAll().catch(() => {});
    slotPool.destroy();
    await hubServer.stop().catch(() => {});
    process.exit(0);
  }
  process.on('SIGINT',  () => shutdown('SIGINT'));
  process.on('SIGTERM', () => shutdown('SIGTERM'));

  // ── Launch workflow ────────────────────────────────────────────────────────

  if (workflow === 'test' || workflow === 'all-in-one') {
    const testWf = new TestWorkflow({
      sessionManager,
      config: cfg,
      options: { maxSessions, intervalMs, consulPost, proxies, templateUuids, command: 'all-in-one' },
    });
    await testWf.run();
    await shutdown('workflow-done');

  } else if (workflow === 'register') {
    // Single session: register only
    const { generatePerson } = require('./identity');
    const { buildRegisterPayload } = require('./workflows/register-workflow');
    const person  = generatePerson();
    const proxy   = proxies[0] ?? null;
    const session = sessionManager.createSession('register', buildRegisterPayload({
      realPerson: person,
    }), proxy);
    await session.start();

  } else if (workflow === 'warmup') {
    const { buildWarmupPayload } = require('./workflows/idle-workflow');
    const proxy   = proxies[0] ?? null;
    const session = sessionManager.createSession('warmup', buildWarmupPayload({
      idleStep: args['idle-step'] ?? 'login',
      consulPost,
    }), proxy);
    await session.start();

  } else if (workflow === 'apply') {
    const { buildApplyPayload } = require('./workflows/apply-workflow');
    const proxy   = proxies[0] ?? null;
    const session = sessionManager.createSession('apply', buildApplyPayload(), proxy);
    await session.start();

  } else if (workflow === 'chain') {
    // Multi-step chain: register → sleep → warmup-idle → apply
    // Runs a single chain by default; use --max-sessions for concurrent chains.
    const { generatePerson } = require('./identity');
    const { createAccount }  = require('./mailtm');
    const chains = [];
    const chainCount = maxSessions > 0 ? maxSessions : 1;
    for (let i = 0; i < chainCount; i++) {
      const person    = generatePerson();
      const emailAcct = await createAccount().catch(err => {
        log.gerror(`Failed to create mail.tm account for chain #${i+1}:`, err.message);
        throw err;
      });
      const proxy     = proxies[i % Math.max(proxies.length, 1)] ?? null;
      const template  = templateUuids[i % Math.max(templateUuids.length, 1)] ?? null;
      const chain = new WorkflowChain({
        sessionManager,
        octoApi,
        config: cfg,
        payload: {
          realPerson: person,
          emailAcct,
          consulPost,
          captchaSolver: 'capsolver',
          proxies: proxy ? [proxy] : [],
        },
        templateUuid: template,
        sleepMs: 90_000,
      });
      chain.on('step',   (s) => log.ginfo(`Chain #${i+1}: step=${s}`));
      chain.on('done',   (r) => log.ginfo(`Chain #${i+1}: DONE  ok=${r.ok}`));
      chain.on('failed', (e) => log.gerror(`Chain #${i+1}: FAILED  reason=${e}`));
      chains.push(chain.run().catch(e => log.gerror(`Chain #${i+1} threw:`, e.message)));
      if (intervalMs > 0 && i < chainCount - 1) await new Promise(r => setTimeout(r, intervalMs));
    }
    await Promise.all(chains);
    await shutdown('workflow-done');

  } else {
    log.gerror(`Unknown workflow: ${workflow}`);
    process.exit(1);
  }
}

main().catch(err => {
  console.error('Fatal error:', err);
  process.exit(1);
});
