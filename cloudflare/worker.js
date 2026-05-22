/**
 * Octo Probe — Cloudflare Email Receiver Worker
 *
 * Two responsibilities:
 *   1. Email handler  — receives inbound mail via Cloudflare Email Routing,
 *      parses text out of the MIME body, stores it in KV under the recipient's
 *      local part with a 2-hour TTL.
 *   2. HTTP API       — lets the extension poll for messages and clean up.
 *
 * Environment bindings (set in Cloudflare dashboard or wrangler.toml):
 *   MAIL_KV    — KV namespace (binding name)
 *   API_SECRET — shared secret; set with:  wrangler secret put API_SECRET
 *
 * HTTP API
 *   GET    /<localpart>   — returns JSON array of stored messages (newest last)
 *   DELETE /<localpart>   — clears the inbox
 *   All requests require header:  X-Secret: <API_SECRET>
 */

// ---------------------------------------------------------------------------
// MIME text extractor
// ---------------------------------------------------------------------------

function _extractText(raw) {
  // Handle multipart messages by splitting on the boundary.
  const boundaryMatch = raw.match(/boundary="?([^"\r\n;]+)"?/i);
  if (boundaryMatch) {
    const bound = "--" + boundaryMatch[1];
    const parts  = raw.split(bound);
    const texts  = [];

    for (const part of parts) {
      const sep = part.indexOf("\n\n");
      if (sep < 0) continue;
      const headers = part.slice(0, sep).toLowerCase();
      const isText  = headers.includes("text/plain") || headers.includes("text/html");
      if (!isText) continue;

      let body = part.slice(sep + 2).replace(/--\s*$/, "").trim();

      if (headers.includes("base64")) {
        try { body = atob(body.replace(/\s+/g, "")); } catch (_) { /* keep raw */ }
      } else if (headers.includes("quoted-printable")) {
        body = body
          .replace(/=\r?\n/g, "")
          .replace(/=([0-9A-Fa-f]{2})/g, (_, h) => String.fromCharCode(parseInt(h, 16)));
      }

      // Strip HTML tags so UUID/token patterns match plain text inside links.
      if (headers.includes("text/html")) body = body.replace(/<[^>]+>/g, " ");

      texts.push(body);
    }

    if (texts.length) return texts.join("\n");
  }

  // Non-multipart: skip headers, return everything after the first blank line.
  const bodyStart = raw.indexOf("\n\n");
  return bodyStart >= 0 ? raw.slice(bodyStart + 2) : raw;
}

// ---------------------------------------------------------------------------
// Email handler  (triggered by Cloudflare Email Routing catch-all)
// ---------------------------------------------------------------------------

async function handleEmail(message, env) {
  const local = message.to.split("@")[0].toLowerCase();
  const key   = `inbox:${local}`;

  // Read the full raw MIME message from the stream.
  const rawText = await new Response(message.raw).text();
  const text    = _extractText(rawText);

  const existing = (await env.MAIL_KV.get(key, "json")) ?? [];
  existing.push({
    from: message.from,
    to:   message.to,
    text,                   // extracted body — what the extension searches
    ts:   Date.now(),
  });

  // Keep the last 10 messages; expire the whole inbox after 2 hours.
  await env.MAIL_KV.put(key, JSON.stringify(existing.slice(-10)), {
    expirationTtl: 7200,
  });
}

// ---------------------------------------------------------------------------
// HTTP fetch handler  (extension polling API)
// ---------------------------------------------------------------------------

async function handleFetch(request, env) {
  // Auth guard.
  if (request.headers.get("X-Secret") !== env.API_SECRET) {
    return new Response("Unauthorized", { status: 401 });
  }

  const url   = new URL(request.url);
  const local = url.pathname.replace(/^\/+/, "").toLowerCase();
  if (!local) return new Response("Bad Request", { status: 400 });

  const key = `inbox:${local}`;

  if (request.method === "DELETE") {
    await env.MAIL_KV.delete(key);
    return new Response("OK");
  }

  // GET — return messages (or empty array if none yet).
  const messages = (await env.MAIL_KV.get(key, "json")) ?? [];
  return new Response(JSON.stringify(messages), {
    headers: {
      "Content-Type": "application/json",
      "Access-Control-Allow-Origin": "*",
    },
  });
}

// ---------------------------------------------------------------------------
// Export
// ---------------------------------------------------------------------------

export default {
  email: handleEmail,
  fetch: handleFetch,
};
