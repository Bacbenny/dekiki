const FILM4K_BASE = "https://film4k.net";
const USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/138.0.0.0 Safari/537.36";
const CORS_HEADERS = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Methods": "GET,HEAD,OPTIONS",
  "Access-Control-Allow-Headers": "Content-Type,Authorization",
};

const SESSION_TTL = 240000;
const CATALOG_TTL = 180000;
const STREAM_TTL = 25000;
const STREAM_URL_FIELDS = ["url", "stream_url", "streamUrl", "link", "playbackUrl", "playback_url", "manifest", "src", "stream", "hls", "m3u8", "mpd"];
const STREAM_CONTAINER_FIELDS = ["sources", "streams", "qualities", "resolutions", "playlists", "source", "data"];

const cache = new Map();
let loginPromise = null;
let catalogPromise = null;

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
  for (const key of STREAM_URL_FIELDS) {
    const found = streamOf(value[key], depth + 1);
    if (found) return found;
  }
  for (const key of STREAM_CONTAINER_FIELDS) {
    const found = streamOf(value[key], depth + 1);
    if (found) return found;
  }
  return "";
}

function sessionCookie(setCookie) {
  const match = String(setCookie || "").match(/(?:^|,\s*)([^=;,\s]+=[^;]*)/);
  return match ? match[1] : "";
}

async function login() {
  const session = cache.get("session");
  if (session && Date.now() - session.ts < SESSION_TTL) return session.cookie;
  if (loginPromise) return loginPromise;
  loginPromise = (async () => {
    try {
      const username = typeof FILM4K_USERNAME !== "undefined" ? FILM4K_USERNAME : "";
      const password = typeof FILM4K_PASSWORD !== "undefined" ? FILM4K_PASSWORD : "";
      if (!username || !password) throw new Error("Film4k secrets are not configured");
      const response = await fetch(`${FILM4K_BASE}/api/auth/signin`, {
        method: "POST",
        headers: { "Content-Type": "application/json", "User-Agent": USER_AGENT },
        body: JSON.stringify({ email: username, password }),
        redirect: "manual",
      });
      if (!response.ok && response.status !== 302) throw new Error(`Film4k login failed: ${response.status}`);
      const cookie = sessionCookie(response.headers.get("set-cookie"));
      if (!cookie) throw new Error("Film4k login returned no session cookie");
      cache.set("session", { ts: Date.now(), cookie });
      return cookie;
    } finally {
      loginPromise = null;
    }
  })();
  return loginPromise;
}

async function apiJson(path, cookie) {
  const response = await fetch(`${FILM4K_BASE}${path}`, { headers: apiHeaders(cookie) });
  if (!response.ok) throw new Error(`Film4k API failed: ${response.status}`);
  return response.json();
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

async function resolveOneStream(kind, id, cookie) {
  const cacheKey = `stream:${kind}:${id}`;
  const cached = cache.get(cacheKey);
  if (cached && Date.now() - cached.ts < STREAM_TTL) return cached.data;
  let stream = "";
  let clearKey = null;
  if (kind === "event") {
    const paths = [
      `/api/tv/events/${encodeURIComponent(id)}/stream`,
      `/api/tv/event/${encodeURIComponent(id)}/stream`,
      `/api/tv/${encodeURIComponent(id)}/stream`,
    ];
    for (const path of paths) {
      try {
        const payload = await apiJson(`${path}?_=${Date.now()}`, cookie);
        stream = streamOf(payload);
        if (stream) { clearKey = clearKeyOf(payload); break; }
      } catch (_) {}
    }
  } else {
    try {
      const payload = await apiJson(`/api/tv/${encodeURIComponent(id)}/stream?_=${Date.now()}`, cookie);
      stream = streamOf(payload);
      if (stream) clearKey = clearKeyOf(payload);
    } catch (_) {}
  }
  if (!stream) {
    const catalog = await loadCatalog();
    const records = kind === "event" ? catalog.events : catalog.channels;
    const item = records.find((candidate) => idOf(candidate) === id);
    stream = streamOf(item);
    if (stream) clearKey = clearKeyOf(item);
  }
  const data = { stream, clearKey };
  if (stream) cache.set(cacheKey, { ts: Date.now(), data });
  return data;
}

async function loadCatalog() {
  const cached = cache.get("catalog");
  if (cached && Date.now() - cached.ts < CATALOG_TTL) return cached.data;
  if (catalogPromise) return catalogPromise;
  catalogPromise = (async () => {
    try {
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
      const data = { cookie, events: eventRecords, channels };
      cache.set("catalog", { ts: Date.now(), data });
      return data;
    } finally {
      catalogPromise = null;
    }
  })();
  return catalogPromise;
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
  const clearKey = clearKeyOf(item);
  const lines = [`#EXTINF:-1 tvg-id="${m3uAttribute(id)}" tvg-name="${name}" tvg-logo="${logo}" group-title="${m3uAttribute(group)}",${name}`];
  if (clearKey) {
    lines.push("#KODIPROP:inputstream.adaptive.manifest_type=mpd");
    lines.push("#KODIPROP:inputstream.adaptive.license_type=clearkey");
    lines.push(`#KODIPROP:inputstream.adaptive.license_key=${clearKey.keyId}:${clearKey.key}`);
  }
  lines.push(stableUrl);
  return lines.join("\n");
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
    headers: { ...CORS_HEADERS, "Content-Type": "application/x-mpegURL; charset=utf-8", "Cache-Control": "public, max-age=180" },
  });
}

async function resolveStream(kind, id) {
  const cookie = await login();
  const result = await resolveOneStream(kind, id, cookie);
  if (!result.stream) return jsonResponse({ error: "Film4k stream is unavailable" }, 502);
  return Response.redirect(result.stream, 302);
}

async function handle(request) {
  if (request.method === "OPTIONS") return new Response(null, { status: 204, headers: CORS_HEADERS });
  const url = new URL(request.url);
  if (url.pathname === "/healthz") return jsonResponse({ ok: true, worker: "film4k", cached: cache.has("catalog"), streams: [...cache.keys()].filter((k) => k.startsWith("stream:")).length });
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
      cached_streams: [...cache.keys()].filter((k) => k.startsWith("stream:")).length,
    });
  }
  if (url.pathname === "/film4k/playlist.m3u" && request.method === "GET") return playlist(request);
  if (url.pathname === "/film4k/events" && request.method === "GET") {
    const catalog = await loadCatalog();
    return jsonResponse({ events: catalog.events, count: catalog.events.length });
  }
  if (url.pathname === "/film4k/cache/clear" && request.method === "GET") {
    cache.clear();
    catalogPromise = null;
    loginPromise = null;
    return jsonResponse({ ok: true, message: "cache cleared" });
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
