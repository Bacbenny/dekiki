const FILM4K_BASE = "https://film4k.net";
const USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/138.0.0.0 Safari/537.36";
const CORS_HEADERS = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Methods": "GET,HEAD,OPTIONS",
  "Access-Control-Allow-Headers": "Content-Type,Authorization",
};

// --- Cache TTLs (seconds) ---
const SESSION_TTL = 240;        // 4 minutes — session cookie shared across isolates via KV
const STREAM_TTL = 60;          // Cloudflare KV requires at least 60 seconds
const CATALOG_TTL = 60;         // 1 minute — channel/event catalog cache
const EDGE_CACHE_TTL = 30;      // 30 seconds — Cloudflare edge caches the 302 redirect

// --- KV namespace binding (configured in wrangler.toml) ---
// FILM4K_KV is bound via Cloudflare dashboard or wrangler.toml
const KV = typeof FILM4K_KV !== "undefined" ? FILM4K_KV : null;

// --- L1 in-memory cache (per-isolate, ultra-fast) ---
const memCache = new Map();

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
  return Object.values(payload).filter((value) => value && typeof value === "object" && !Array.isArray(value));
}

function text(item, keys, fallback = "") {
  for (const key of keys) {
    if (item && item[key] !== undefined && item[key] !== null && String(item[key]).trim()) return String(item[key]).trim();
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
    for (const child of value) {
      const found = streamOf(child, depth + 1);
      if (found) return found;
    }
    return "";
  }
  if (typeof value !== "object") return "";
  for (const key of ["url", "stream_url", "streamUrl", "link", "playbackUrl", "playback_url", "manifest", "src", "stream", "hls", "m3u8", "mpd"]) {
    const found = streamOf(value[key], depth + 1);
    if (found) return found;
  }
  for (const key of ["sources", "streams", "qualities", "resolutions", "playlists", "source", "data"]) {
    const found = streamOf(value[key], depth + 1);
    if (found) return found;
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
    for (const child of value) {
      const found = clearKeyOf(child, depth + 1);
      if (found) return found;
    }
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
    const found = clearKeyOf(child, depth + 1);
    if (found) return found;
  }
  return null;
}

// ─── Session cookie: L1 (Map) → L2 (KV) → login ───
async function login() {
  // L1: in-memory
  const memSession = memCache.get("session");
  if (memSession && Date.now() - memSession.ts < SESSION_TTL * 1000) return memSession.cookie;

  // L2: KV (shared across all isolates)
  if (KV) {
    const kvCookie = await KV.get("session:cookie");
    if (kvCookie) {
      memCache.set("session", { ts: Date.now(), cookie: kvCookie });
      return kvCookie;
    }
  }

  // Login to film4k.net
  const username = typeof FILM4K_USERNAME !== "undefined" ? FILM4K_USERNAME : "";
  const password = typeof FILM4K_PASSWORD !== "undefined" ? FILM4K_PASSWORD : "";
  if (!username || !password) throw new Error("Film4k secrets are not configured");
  const response = await fetch(`${FILM4K_BASE}/api/auth/signin`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      "User-Agent": USER_AGENT,
      "Cache-Control": "no-cache",
    },
    body: JSON.stringify({ email: username, password }),
    redirect: "manual",
  });
  if (!response.ok && response.status !== 302) throw new Error(`Film4k login failed: ${response.status}`);
  const cookie = sessionCookie(response.headers.get("set-cookie"));
  if (!cookie) throw new Error("Film4k login returned no session cookie");

  // Store in L1 + L2
  memCache.set("session", { ts: Date.now(), cookie });
  if (KV) await KV.put("session:cookie", cookie, { expirationTtl: SESSION_TTL });
  return cookie;
}

async function apiJson(path, cookie) {
  const response = await fetch(`${FILM4K_BASE}${path}`, {
    headers: {
      ...apiHeaders(cookie),
      "Cache-Control": "no-cache",
      Pragma: "no-cache",
    },
  });
  if (!response.ok) throw new Error(`Film4k API failed: ${response.status}`);
  return response.json();
}

// ─── Event details: try all 3 API paths in PARALLEL (was sequential) ───
async function eventDetails(event, cookie) {
  const id = idOf(event);
  if (!id) return event;
  const paths = [
    `/api/tv/events/${encodeURIComponent(id)}/stream`,
    `/api/tv/event/${encodeURIComponent(id)}/stream`,
    `/api/tv/${encodeURIComponent(id)}/stream`,
  ];
  const results = await Promise.allSettled(
    paths.map((path) => apiJson(`${path}?_=${Date.now()}`, cookie))
  );
  for (const result of results) {
    if (result.status === "fulfilled") {
      const stream = streamOf(result.value);
      if (stream) {
        const details = { ...event, stream_url: stream };
        const clearKey = clearKeyOf(result.value);
        if (clearKey) details.clearKey = clearKey;
        return details;
      }
    }
  }
  return event;
}

async function loadCatalog() {
  // L1
  const cached = memCache.get("catalog");
  if (cached && Date.now() - cached.ts < CATALOG_TTL * 1000) return cached.data;

  const cookie = await login();
  const [eventsPayload, channelsPayload] = await Promise.all([
    apiJson(`/api/tv/events?_=${Date.now()}`, cookie).catch(() => null),
    apiJson(`/api/tv/channels?_=${Date.now()}`, cookie),
  ]);
  const rawEvents = eventsPayload ? unwrap(eventsPayload, ["events", "data", "items", "results"]) : [];
  const channels = unwrap(channelsPayload, ["channels", "data", "items", "results"]);
  const eventRecords = rawEvents.length
    ? rawEvents
    : channels.filter((channel) => {
        if (String(channel.id || "").startsWith("ants:")) return false;
        const groupText = `${channel.group || ""} ${channel.category || ""}`;
        const name = String(channel.name || channel.title || "");
        return /event|sự kiện|sukien|trực tiếp|tructiep/i.test(groupText)
          || /^TV360\+\s*\d+/i.test(name);
      });
  // Event details already parallelized inside eventDetails (3 paths in parallel)
  // plus all events resolved in parallel here
  const events = await Promise.all(eventRecords.map((event) => eventDetails(event, cookie)));
  const data = { cookie, events, channels };
  memCache.set("catalog", { ts: Date.now(), data });
  return data;
}

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

// ─── Stream resolution: L1 (Map) → L2 (KV) → API → cache in both ───
async function resolveStream(kind, id) {
  const cacheKey = `stream:${kind}:${id}`;

  // L1: in-memory (sub-millisecond)
  const memHit = memCache.get(cacheKey);
  if (memHit && Date.now() - memHit.ts < STREAM_TTL * 1000) {
    return Response.redirect(memHit.url, 302);
  }

  // L2: KV (1-5ms, shared across all edge isolates)
  if (KV) {
    const kvUrl = await KV.get(cacheKey);
    if (kvUrl) {
      // Promote to L1
      memCache.set(cacheKey, { ts: Date.now(), url: kvUrl });
      return Response.redirect(kvUrl, 302);
    }
  }

  // Cache miss — resolve from film4k.net API
  const cookie = await login();
  let stream = "";

  if (kind === "event") {
    stream = streamOf(await eventDetails({ id }, cookie));
  } else {
    const paths = [
      `/api/tv/${encodeURIComponent(id)}/stream`,
      `/api/tv/channels/${encodeURIComponent(id)}/stream`,
      `/api/tv/channel/${encodeURIComponent(id)}/stream`,
    ];
    const results = await Promise.allSettled(
      paths.map((path) => apiJson(`${path}?_=${Date.now()}`, cookie))
    );
    for (const result of results) {
      if (result.status === "fulfilled") {
        stream = streamOf(result.value);
        if (stream) break;
      }
    }
  }

  // Fallback: search catalog for pre-resolved stream_url
  if (!stream) {
    const catalog = await loadCatalog();
    const records = kind === "event" ? catalog.events : catalog.channels;
    const item = records.find((candidate) => idOf(candidate) === id);
    stream = streamOf(item);
  }

  if (!stream) return jsonResponse({ error: "Film4k stream is unavailable" }, 502);

  // Store in L1 + L2
  memCache.set(cacheKey, { ts: Date.now(), url: stream });
  if (KV) await KV.put(cacheKey, stream, { expirationTtl: STREAM_TTL });

  // 302 with edge cache header — Cloudflare CDN caches this redirect
  return new Response(null, {
    status: 302,
    headers: {
      Location: stream,
      "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
      ...CORS_HEADERS,
    },
  });
}

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
      apiJson(`/api/tv/events?_=${Date.now()}`, cookie).catch((e) => ({ error: String(e.message || e) })),
      apiJson(`/api/tv/channels?_=${Date.now()}`, cookie).catch((e) => ({ error: String(e.message || e) })),
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

  // Pre-resolve stream URLs for all channels — used by film4k_fetch.py
  // to embed direct stream URLs in the m3u for instant playback (no Worker round-trip)
  if (url.pathname === "/film4k/streams" && request.method === "GET") {
    const catalog = await loadCatalog();
    const cookie = catalog.cookie;
    const results = await Promise.all(
      catalog.channels.map(async (ch) => {
        const id = idOf(ch);
        if (!id || String(id).startsWith("ants:")) return null;
        try {
          const payload = await apiJson(`/api/tv/${encodeURIComponent(id)}/stream?_=${Date.now()}`, cookie);
          const stream = streamOf(payload);
          const clearKey = clearKeyOf(payload);
          return { id, stream_url: stream, clearKey: clearKey || null };
        } catch (_) {
          return { id, stream_url: "", clearKey: null };
        }
      })
    );
    const mapping = {};
    for (const r of results) {
      if (r && r.stream_url) mapping[r.id] = { url: r.stream_url, clearKey: r.clearKey };
    }
    return jsonResponse({ streams: mapping, count: Object.keys(mapping).length });
  }

  if (url.pathname === "/film4k/cache/clear" && request.method === "GET") {
    memCache.clear();
    if (KV) {
      // KV clear is best-effort — list and delete stream: keys
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

addEventListener("fetch", (event) => {
  event.respondWith(handle(event.request).catch((error) => jsonResponse({ error: "Film4k request failed", detail: String(error.message || error) }, 502)));
});
