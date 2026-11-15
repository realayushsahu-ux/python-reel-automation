const encoder = new TextEncoder();

function json(data, status = 200) {
  return new Response(JSON.stringify(data), {
    status,
    headers: { "content-type": "application/json; charset=utf-8", "cache-control": "no-store" },
  });
}

function equalConstantTime(left, right) {
  if (left.length !== right.length) return false;
  let mismatch = 0;
  for (let i = 0; i < left.length; i += 1) mismatch |= left.charCodeAt(i) ^ right.charCodeAt(i);
  return mismatch === 0;
}

async function validSignature(secret, rawBody, supplied) {
  if (!secret || !supplied.startsWith("sha256=")) return false;
  const key = await crypto.subtle.importKey(
    "raw", encoder.encode(secret), { name: "HMAC", hash: "SHA-256" }, false, ["sign"],
  );
  const digest = new Uint8Array(await crypto.subtle.sign("HMAC", key, rawBody));
  const hex = [...digest].map((byte) => byte.toString(16).padStart(2, "0")).join("");
  return equalConstantTime(`sha256=${hex}`, supplied.toLowerCase());
}

function authorized(request, env) {
  const supplied = request.headers.get("authorization") || "";
  return Boolean(env.RELAY_TOKEN) && equalConstantTime(supplied, `Bearer ${env.RELAY_TOKEN}`);
}

async function receiveMeta(request, env) {
  const url = new URL(request.url);
  if (request.method === "GET") {
    const mode = url.searchParams.get("hub.mode") || "";
    const token = url.searchParams.get("hub.verify_token") || "";
    const challenge = url.searchParams.get("hub.challenge") || "";
    if (mode === "subscribe" && challenge && env.META_WEBHOOK_VERIFY_TOKEN
        && equalConstantTime(token, env.META_WEBHOOK_VERIFY_TOKEN)) {
      return new Response(challenge, { headers: { "content-type": "text/plain; charset=utf-8" } });
    }
    return new Response("Webhook verification failed", { status: 403 });
  }
  if (request.method !== "POST") return new Response("Method not allowed", { status: 405 });
  const rawBody = await request.arrayBuffer();
  if (rawBody.byteLength > 1024 * 1024) return new Response("Payload too large", { status: 413 });
  const signature = request.headers.get("x-hub-signature-256") || "";
  if (!(await validSignature(env.META_APP_SECRET, rawBody, signature))) {
    return new Response("Invalid Meta webhook signature", { status: 403 });
  }
  let payload;
  try {
    payload = JSON.parse(new TextDecoder().decode(rawBody));
  } catch {
    return new Response("Invalid JSON payload", { status: 400 });
  }

  const stored = [];
  for (const entry of Array.isArray(payload.entry) ? payload.entry : []) {
    for (const event of Array.isArray(entry.messaging) ? entry.messaging : []) {
      const message = event.message || {};
      if (message.is_echo || !message.mid) continue;
      const attachments = Array.isArray(message.attachments) ? message.attachments : [];
      const urls = attachments
        .map((attachment) => attachment?.payload?.url)
        .filter((value) => typeof value === "string" && value.length < 8192);
      const receivedAt = Number(event.timestamp);
      const captured = {
        received_at: Number.isFinite(receivedAt) && receivedAt > 0
          ? new Date(receivedAt).toISOString()
          : new Date().toISOString(),
        sender_id: String(event.sender?.id || ""),
        recipient_id: String(event.recipient?.id || ""),
        message_id: String(message.mid),
        shared_media_urls: urls,
      };
      const key = `event:${encodeURIComponent(captured.message_id)}`;
      await env.REEL_EVENTS.put(key, JSON.stringify(captured), { expirationTtl: 60 * 60 * 24 * 90 });
      stored.push(captured.message_id);
    }
  }
  return json({ status: "received", events: stored.length });
}

async function listEvents(url, env) {
  const cursor = url.searchParams.get("cursor") || undefined;
  const page = await env.REEL_EVENTS.list({ prefix: "event:", limit: 100, cursor });
  const events = (await Promise.all(page.keys.map((item) => env.REEL_EVENTS.get(item.name, "json"))))
    .filter(Boolean)
    .sort((a, b) => String(a.received_at).localeCompare(String(b.received_at)));
  return json({ events, cursor: page.list_complete ? null : page.cursor });
}

async function acknowledge(request, env) {
  let body;
  try {
    body = await request.json();
  } catch {
    return json({ error: "Invalid JSON body" }, 400);
  }
  const ids = Array.isArray(body.message_ids) ? body.message_ids : [];
  if (!ids.length || ids.length > 100 || ids.some((id) => typeof id !== "string" || id.length > 500)) {
    return json({ error: "message_ids must contain 1 to 100 message IDs" }, 400);
  }
  await Promise.all(ids.map((id) => env.REEL_EVENTS.delete(`event:${encodeURIComponent(id)}`)));
  return json({ acknowledged: ids.length });
}

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    if (url.pathname === "/meta/webhook") return receiveMeta(request, env);
    if (url.pathname === "/health" && request.method === "GET") return json({ ok: true });
    if (url.pathname.startsWith("/internal/")) {
      if (!authorized(request, env)) return json({ error: "Unauthorized" }, 401);
      if (url.pathname === "/internal/events" && request.method === "GET") return listEvents(url, env);
      if (url.pathname === "/internal/ack" && request.method === "POST") return acknowledge(request, env);
    }
    return new Response("Not found", { status: 404 });
  },
};
