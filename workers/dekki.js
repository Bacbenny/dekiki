const FILM4K_BASE = "https://film4k.net";
const USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/138.0.0.0 Safari/537.36";
const CORS_HEADERS = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Methods": "GET,HEAD,OPTIONS",
  "Access-Control-Allow-Headers": "Content-Type,Authorization",
};

// JWT from TV360 lives ~5 hours. Pre-warm every 55 min via cron.
// KV TTL is 4 hours so entries survive even if cron fails a few cycles.
const SESSION_TTL = 600;     // 10 min — session cookie in mem cache
const KV_SESSION_TTL = 1200; // 20 min — session cookie in KV
const STREAM_TTL = 14400;    // 4 hours — KV stream cache (JWT lives ~5h)
const CATALOG_TTL = 60;      // 1 min — channel list cache
const EDGE_CACHE = 30;       // 30 sec — edge cache for 302 redirects

const KV = typeof FILM4K_KV !== "undefined" ? FILM4K_KV : null;

// Per-isolate L1 cache
const memCache = new Map();

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function jsonResponse(body, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { ...CORS_HEADERS, "Content-Type": "application/json; charset=utf-8" },
  });
}

function apiHeaders(cookie) {
  return {
    Accept: "application/json",
    "User-Agent": USER_AGENT,
    Referer: `${FILM4K_BASE}/`,
    Origin: FILM4K_BASE,
    Cookie: cookie,
  };
}

function unwrap(payload, keys) {
  if (Array.isArray(payload)) return payload;
  if (!payload || typeof payload !== "object") return [];
  for (const key of keys) {
    if (Array.isArray(payload[key])) return payload[key];
  }
  if (Array.isArray(payload.data)) return payload.data;
  return Object.values(payload).filter((v) => v && typeof v === "object" && !Array.isArray(v));
}

function text(item, keys, fallback = "") {
  for (const key of keys) {
    if (item && item[key] !== undefined && item[key] !== null && String(item[key]).trim())
      return String(item[key]).trim();
  }
  return fallback;
}

function idOf(item) {
  return text(item, ["id", "event_id", "channel_id", "channelId", "tvg_id", "tvgId", "slug", "code"]);
}

function streamOf(value, depth = 0) {
  if (depth > 6 || value === null || value === undefined) return "";
  if (typeof value === "string" && /^https?:\/\//i.test(value)) return value;
  if (Array.isArray(value)) {
    for (const child of value) { const f = streamOf(child, depth + 1); if (f) return f; }
    return "";
  }
  if (typeof value !== "object") return "";
  for (const key of ["url", "stream_url", "streamUrl", "link", "playbackUrl", "playback_url", "manifest", "src", "stream", "hls", "m3u8", "mpd"]) {
    const f = streamOf(value[key], depth + 1); if (f) return f;
  }
  for (const key of ["sources", "streams", "qualities", "resolutions", "playlists", "source", "data"]) {
    const f = streamOf(value[key], depth + 1); if (f) return f;
  }
  return "";
}

function sessionCookie(setCookie) {
  const match = String(setCookie || "").match(/(?:^|,\s*)([^=;,\s]+=[^;]*)/);
  return match ? match[1] : "";
}

function clearKeyOf(value, depth = 0) {
  if (depth > 6 || value === null || value === undefined) return null;
  if (Array.isArray(value)) {
    for (const child of value) { const f = clearKeyOf(child, depth + 1); if (f) return f; }
    return null;
  }
  if (typeof value !== "object") return null;
  const candidate = value.clearKey || value.clear_key;
  if (candidate && typeof candidate === "object") {
    const keyId = candidate.keyId || candidate.key_id;
    const key = candidate.key;
    if (keyId && key) return { keyId: String(keyId), key: String(key) };
  }
  for (const child of Object.values(value)) {
    const f = clearKeyOf(child, depth + 1); if (f) return f;
  }
  return null;
}

// Race multiple fetches and return the first successful result with a stream.
// Uses Promise.any — returns ASAP when the fastest path succeeds instead of
// waiting for all paths to settle.
async function raceStream(paths, cookie) {
  const fetches = paths.map((path) =>
    fetch(`${FILM4K_BASE}${path}`, { headers: apiHeaders(cookie) })
      .then((r) => r.json())
      .then((data) => {
        const stream = streamOf(data);
        if (!stream) throw new Error("no stream in response");
        return { stream, clearKey: clearKeyOf(data) };
      })
  );
  try {
    return await Promise.any(fetches);
  } catch {
    return { stream: "", clearKey: null };
  }
}

// ---------------------------------------------------------------------------
// Auth: L1 → L2 (KV) → login
// ---------------------------------------------------------------------------

async function login() {
  const mem = memCache.get("session");
  if (mem && Date.now() - mem.ts < SESSION_TTL * 1000) return mem.cookie;

  if (KV) {
    const kvCookie = await KV.get("session:cookie");
    if (kvCookie) {
      memCache.set("session", { ts: Date.now(), cookie: kvCookie });
      return kvCookie;
    }
  }

  const username = typeof FILM4K_USERNAME !== "undefined" ? FILM4K_USERNAME : "";
  const password = typeof FILM4K_PASSWORD !== "undefined" ? FILM4K_PASSWORD : "";
  if (!username || !password) throw new Error("Film4k secrets not configured");

  const response = await fetch(`${FILM4K_BASE}/api/auth/signin`, {
    method: "POST",
    headers: { "Content-Type": "application/json", "User-Agent": USER_AGENT },
    body: JSON.stringify({ email: username, password }),
    redirect: "manual",
  });
  if (!response.ok && response.status !== 302) throw new Error(`Film4k login failed: ${response.status}`);
  const cookie = sessionCookie(response.headers.get("set-cookie"));
  if (!cookie) throw new Error("Film4k login returned no session cookie");

  memCache.set("session", { ts: Date.now(), cookie });
  if (KV) await KV.put("session:cookie", cookie, { expirationTtl: KV_SESSION_TTL });
  return cookie;
}

async function apiJson(path, cookie) {
  const response = await fetch(`${FILM4K_BASE}${path}`, { headers: apiHeaders(cookie) });
  if (!response.ok) throw new Error(`Film4k API failed: ${response.status}`);
  return response.json();
}

// ---------------------------------------------------------------------------
// Event stream resolution — races 3 API paths, returns first success
// ---------------------------------------------------------------------------

async function eventDetails(event, cookie) {
  const id = idOf(event);
  if (!id) return event;
  const paths = [
    `/api/tv/events/${encodeURIComponent(id)}/stream`,
    `/api/tv/event/${encodeURIComponent(id)}/stream`,
    `/api/tv/${encodeURIComponent(id)}/stream`,
  ];
  const result = await raceStream(paths, cookie);
  if (result.stream) {
    const details = { ...event, stream_url: result.stream };
    if (result.clearKey) details.clearKey = result.clearKey;
    return details;
  }
  return event;
}

// ---------------------------------------------------------------------------
// Channel stream resolution — races 3 API paths, returns first success
// ---------------------------------------------------------------------------

async function resolveChannelStream(id, cookie) {
  const paths = [
    `/api/tv/${encodeURIComponent(id)}/stream`,
    `/api/tv/channels/${encodeURIComponent(id)}/stream`,
    `/api/tv/channel/${encodeURIComponent(id)}/stream`,
  ];
  return raceStream(paths, cookie);
}

// ---------------------------------------------------------------------------
// Catalog
// ---------------------------------------------------------------------------

async function loadCatalog() {
  const cached = memCache.get("catalog");
  if (cached && Date.now() - cached.ts < CATALOG_TTL * 1000) return cached.data;

  const cookie = await login();
  const [eventsPayload, channelsPayload] = await Promise.all([
    apiJson(`/api/tv/events`, cookie).catch(() => null),
    apiJson(`/api/tv/channels`, cookie),
  ]);
  const rawEvents = eventsPayload ? unwrap(eventsPayload, ["events", "data", "items", "results"]) : [];
  const channels = unwrap(channelsPayload, ["channels", "data", "items", "results"]);
  const eventRecords = rawEvents.length
    ? rawEvents
    : channels.filter((ch) => {
        if (String(ch.id || "").startsWith("ants:")) return false;
        const groupText = `${ch.group || ""} ${ch.category || ""}`;
        const name = String(ch.name || ch.title || "");
        return /event|sự kiện|sukien|trực tiếp|tructiep/i.test(groupText)
          || /^TV360\+\s*\d+/i.test(name);
      });
  const events = await Promise.all(eventRecords.map((ev) => eventDetails(ev, cookie)));
  const data = { cookie, events, channels };
  memCache.set("catalog", { ts: Date.now(), data });
  return data;
}

// ---------------------------------------------------------------------------
// Playlist
// ---------------------------------------------------------------------------

function m3uAttribute(value) {
  return String(value || "").replace(/[\r\n"]/g, " ").trim();
}

function entry(item, group, kind, origin) {
  const id = idOf(item);
  if (!id) return "";
  const name = m3uAttribute(text(item, ["name", "title", "channel_name", "event_name", "label"], "Film4k"));
  const logo = m3uAttribute(text(item, ["logo", "icon", "thumbnail", "image", "poster"]));
  const stableUrl = `${origin}/film4k/stream/${kind}/${encodeURIComponent(id)}`;
  return `#EXTINF:-1 tvg-id="${m3uAttribute(id)}" tvg-name="${name}" tvg-logo="${logo}" group-title="${m3uAttribute(group)}",${name}\n${stableUrl}`;
}

async function playlist(request) {
  const catalog = await loadCatalog();
  const origin = new URL(request.url).origin;
  const lines = ["#EXTM3U"];
  for (const item of catalog.events) {
    const line = entry(item, "Sự Kiện TV360", "event", origin);
    if (line) lines.push(line);
  }
  for (const item of catalog.channels) {
    const group = text(item, ["group", "category", "group_title"], "Film4k");
    const line = entry(item, group, "channel", origin);
    if (line) lines.push(line);
  }
  return new Response(`${lines.join("\n")}\n`, {
    headers: { ...CORS_HEADERS, "Content-Type": "application/x-mpegURL; charset=utf-8", "Cache-Control": "public, max-age=60" },
  });
}

// ---------------------------------------------------------------------------
// Stream resolution: L1 → L2 (KV) → live API
// ---------------------------------------------------------------------------

function redirectResponse(url, fromCache) {
  const headers = { Location: url, ...CORS_HEADERS };
  // Allow edge caching for cache hits — reduces Worker invocations.
  // Cache misses use no-store since the URL might not be optimal yet.
  headers["Cache-Control"] = fromCache ? `public, max-age=${EDGE_CACHE}` : "no-store";
  return new Response(null, { status: 302, headers });
}

async function resolveStream(kind, id) {
  const cacheKey = `stream:${kind}:${id}`;

  // L1: in-memory (instant, ~0ms)
  const memHit = memCache.get(cacheKey);
  if (memHit && Date.now() - memHit.ts < STREAM_TTL * 1000) {
    return redirectResponse(memHit.url, true);
  }

  // L2: KV — should always be warm thanks to cron pre-warm (~50ms)
  if (KV) {
    const kvEntry = await KV.getWithMetadata(cacheKey);
    if (kvEntry && kvEntry.value) {
      memCache.set(cacheKey, { ts: Date.now(), url: kvEntry.value });
      return redirectResponse(kvEntry.value, true);
    }
  }

  // Cache miss — fetch fresh JWT from API
  const cookie = await login();
  const result = kind === "event"
    ? { stream: streamOf(await eventDetails({ id }, cookie)), clearKey: null }
    : await resolveChannelStream(id, cookie);

  if (!result.stream) return jsonResponse({ error: "Film4k stream is unavailable" }, 502);

  memCache.set(cacheKey, { ts: Date.now(), url: result.stream });
  if (KV) await KV.put(cacheKey, result.stream, { expirationTtl: STREAM_TTL });

  return redirectResponse(result.stream, false);
}

// ---------------------------------------------------------------------------
// Cron pre-warm: called every 55 minutes by Cloudflare Cron Triggers.
// Fetches a fresh JWT for every channel and stores it in KV so that
// stream requests are always served from cache (near 0ms, never stale).
// ---------------------------------------------------------------------------

async function preWarm() {
  if (!KV) return { ok: false, reason: "KV not bound" };

  // Single fresh login — no need to invalidate existing session
  memCache.delete("session");
  if (KV) await KV.delete("session:cookie");
  const freshCookie = await login();

  const channelsPayload = await apiJson(`/api/tv/channels`, freshCookie).catch(() => null);
  if (!channelsPayload) return { ok: false, reason: "channels API failed" };
  const channels = unwrap(channelsPayload, ["channels", "data", "items", "results"]);

  const eventsPayload = await apiJson(`/api/tv/events`, freshCookie).catch(() => null);
  const rawEvents = eventsPayload ? unwrap(eventsPayload, ["events", "data", "items", "results"]) : [];
  const events = await Promise.all(rawEvents.map((ev) => eventDetails(ev, freshCookie)));

  // Resolve channel streams in parallel, store each into KV
  const channelResults = await Promise.allSettled(
    channels
      .filter((ch) => !String(idOf(ch) || "").startsWith("ants:"))
      .map(async (ch) => {
        const id = idOf(ch);
        if (!id) return null;
        const { stream } = await resolveChannelStream(id, freshCookie);
        if (!stream) return null;
        const cacheKey = `stream:channel:${id}`;
        await KV.put(cacheKey, stream, { expirationTtl: STREAM_TTL });
        memCache.set(cacheKey, { ts: Date.now(), url: stream });
        return id;
      })
  );

  // Resolve event streams and store into KV
  const eventResults = await Promise.allSettled(
    events.map(async (ev) => {
      const id = idOf(ev);
      if (!id) return null;
      const stream = streamOf(ev);
      if (!stream) return null;
      const cacheKey = `stream:event:${id}`;
      await KV.put(cacheKey, stream, { expirationTtl: STREAM_TTL });
      memCache.set(cacheKey, { ts: Date.now(), url: stream });
      return id;
    })
  );

  const warmedChannels = channelResults.filter((r) => r.status === "fulfilled" && r.value).length;
  const warmedEvents = eventResults.filter((r) => r.status === "fulfilled" && r.value).length;

  // Update catalog cache
  memCache.set("catalog", {
    ts: Date.now(),
    data: { cookie: freshCookie, events, channels },
  });

  return { ok: true, channels: warmedChannels, events: warmedEvents };
}

// ---------------------------------------------------------------------------
// Request handler
// ---------------------------------------------------------------------------

async function handle(request) {
  if (request.method === "OPTIONS") return new Response(null, { status: 204, headers: CORS_HEADERS });
  const url = new URL(request.url);

  if (url.pathname === "/healthz") {
    return jsonResponse({
      ok: true,
      worker: "film4k",
      kv_bound: !!KV,
      mem_keys: memCache.size,
      mem_has_session: memCache.has("session"),
      mem_has_catalog: memCache.has("catalog"),
    });
  }

  if (url.pathname === "/film4k/debug" && request.method === "GET") {
    const cookie = await login();
    const [ev, ch] = await Promise.all([
      apiJson(`/api/tv/events`, cookie).catch((e) => ({ error: String(e.message || e) })),
      apiJson(`/api/tv/channels`, cookie).catch((e) => ({ error: String(e.message || e) })),
    ]);
    return jsonResponse({
      events_raw: JSON.stringify(ev).slice(0, 2000),
      channels_raw: JSON.stringify(ch).slice(0, 500),
      events_keys: ev && typeof ev === "object" ? Object.keys(ev) : [],
      channels_keys: ch && typeof ch === "object" ? Object.keys(ch) : [],
      kv_bound: !!KV,
      mem_cache_size: memCache.size,
    });
  }

  if (url.pathname === "/film4k/playlist.m3u" && request.method === "GET") return playlist(request);

  if (url.pathname === "/film4k/events" && request.method === "GET") {
    const catalog = await loadCatalog();
    return jsonResponse({ events: catalog.events, count: catalog.events.length });
  }

  if (url.pathname === "/film4k/streams" && request.method === "GET") {
    const catalog = await loadCatalog();
    const cookie = catalog.cookie;
    const results = await Promise.all(
      catalog.channels.map(async (ch) => {
        const id = idOf(ch);
        if (!id || String(id).startsWith("ants:")) return null;
        const { stream, clearKey } = await resolveChannelStream(id, cookie);
        return stream ? { id, stream_url: stream, clearKey: clearKey || null } : null;
      })
    );
    const mapping = {};
    for (const r of results) {
      if (r && r.stream_url) mapping[r.id] = { url: r.stream_url, clearKey: r.clearKey };
    }
    return jsonResponse({ streams: mapping, count: Object.keys(mapping).length });
  }

  // Manual trigger for pre-warm (also called by cron)
  if (url.pathname === "/film4k/prewarm" && request.method === "GET") {
    const result = await preWarm();
    return jsonResponse(result);
  }

  if (url.pathname === "/film4k/cache/clear" && request.method === "GET") {
    memCache.clear();
    if (KV) {
      try {
        const list = await KV.list({ prefix: "stream:" });
        await Promise.all(list.keys.map((k) => KV.delete(k.name)));
        await KV.delete("session:cookie");
      } catch (_) {}
    }
    return jsonResponse({ ok: true, message: "L1 + L2 cache cleared" });
  }

  const match = url.pathname.match(/^\/film4k\/stream\/(event|channel)\/([^/]+)$/);
  if (match && (request.method === "GET" || request.method === "HEAD")) {
    return resolveStream(match[1], decodeURIComponent(match[2]));
  }
  return jsonResponse({ error: "Not found" }, 404);
}

// ---------------------------------------------------------------------------
// Entry points
// ---------------------------------------------------------------------------

addEventListener("fetch", (event) => {
  event.respondWith(
    handle(event.request).catch((err) =>
      jsonResponse({ error: "Film4k request failed", detail: String(err.message || err) }, 502)
    )
  );
});

// Cloudflare Cron Trigger — runs every 55 minutes (configured in deploy_workers.py)
addEventListener("scheduled", (event) => {
  event.waitUntil(
    preWarm().then((result) => {
      console.log(`[cron] pre-warm complete: channels=${result.channels} events=${result.events}`);
    }).catch((err) => {
      console.error(`[cron] pre-warm failed: ${err.message || err}`);
    })
  );
});
