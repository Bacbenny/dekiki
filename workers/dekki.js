// dekki — TieuLam Relay Worker v7
// Mirror của tieulam-relay, chạy song song làm fallback.
// Fixes: TIEULAM_API binding, getBase env-first, Replit relay fallback khi WAF block (403)

const TIEULAM_FRONTS = [
  "https://sv2.tieulam.info",
  "https://sv1.tieulam1.live",
];

const TIEULAM_HDR = {
  "Content-Type": "application/json",
  "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
  "Referer": "https://sv2.tieulam.info/",
  "Origin": "https://sv2.tieulam.info",
  "Accept": "application/json, text/plain, */*",
};

const CORS = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Methods": "GET,POST,OPTIONS",
  "Access-Control-Allow-Headers": "Content-Type,X-Relay-Token,Authorization",
};

function jsonResp(body, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { ...CORS, "Content-Type": "application/json" },
  });
}

function buildDomains(knownApi = "") {
  const vnMs = Date.now() + 7 * 3_600_000;
  const domains = [];
  for (let i = 0; i <= 7; i++) {
    const d = new Date(vnMs + i * 86_400_000);
    domains.push(fmtDomain(d));
  }
  for (let i = 1; i <= 21; i++) {
    const d = new Date(vnMs - i * 86_400_000);
    domains.push(fmtDomain(d));
  }
  const unique = [...new Set(domains)];
  if (!knownApi) return unique;
  return [knownApi, ...unique.filter(d => d !== knownApi)];
}

function fmtDomain(d) {
  const dd = String(d.getUTCDate()).padStart(2, "0");
  const mm = String(d.getUTCMonth() + 1).padStart(2, "0");
  const yyyy = d.getUTCFullYear();
  return `https://api.tlap${dd}${mm}${yyyy}.com`;
}

let _apiBase = "";
let _apiDiscoveredAt = 0;
let _lastGoodData = [];
let _lastGoodTs = 0;

const API_DISC_MS  = 3_600_000;
const DATA_FRESH_MS = 1_800_000;
const CF_CACHE_KEY  = "https://tieulam-relay-internal/matches-v6-dekki";
const KV_STALE_MS   = 7_200_000;

async function cacheGet() {
  try {
    const cached = await caches.default.match(new Request(CF_CACHE_KEY));
    if (!cached) return null;
    const body = await cached.json();
    if (body?.data) return body;
  } catch (_) {}
  return null;
}

async function cachePut(data, apiBase) {
  try {
    const body = JSON.stringify({ ts: Date.now(), data, api_base: apiBase });
    await caches.default.put(
      new Request(CF_CACHE_KEY),
      new Response(body, {
        headers: {
          "Content-Type": "application/json",
          "Cache-Control": `public, max-age=${Math.floor(KV_STALE_MS / 1000)}`,
        },
      })
    );
  } catch (_) {}
}

async function discoverFromFrontend() {
  for (const front of TIEULAM_FRONTS) {
    try {
      const html = await fetch(front, { signal: AbortSignal.timeout(8000) }).then(r => r.text());
      for (const m of html.matchAll(/src="(\/assets\/[^"]+\.js)"/g)) {
        try {
          const js = await fetch(front + m[1], { signal: AbortSignal.timeout(12000) }).then(r => r.text());
          const hit = js.match(/create\(\{baseURL:"(https:\/\/api\.tlap[^"]+)"\}/)
            || js.match(/"(https:\/\/api\.tlap[\w\-]+\.com)"/);
          if (hit) return hit[1];
        } catch (_) {}
      }
    } catch (_) {}
  }
  return null;
}

async function probeDomains(domains) {
  for (const base of domains.slice(0, 10)) {
    try {
      const r = await fetch(base + "/matches/graph", {
        method: "POST",
        headers: TIEULAM_HDR,
        body: JSON.stringify({ queries: [], limit: 1, page: 1 }),
        signal: AbortSignal.timeout(6000),
      });
      if (r.ok || r.status === 422 || r.status === 200) return base;
    } catch (_) {}
  }
  return null;
}

async function getApiBase(env) {
  const envApi = (env && env.TIEULAM_API) || "";
  // Nếu có env var cứng → luôn dùng, không probe thêm
  if (envApi) {
    if (_apiBase !== envApi) {
      _apiBase = envApi;
      _apiDiscoveredAt = Date.now();
    }
    return _apiBase;
  }
  if (!_apiBase) _apiBase = buildDomains("")[0];
  if (Date.now() - _apiDiscoveredAt < API_DISC_MS) return _apiBase;

  const fromFront = await discoverFromFrontend();
  if (fromFront) { _apiBase = fromFront; _apiDiscoveredAt = Date.now(); return _apiBase; }

  const found = await probeDomains(buildDomains(envApi));
  if (found) { _apiBase = found; _apiDiscoveredAt = Date.now(); return _apiBase; }

  _apiDiscoveredAt = Date.now() - API_DISC_MS + 300_000;
  return _apiBase;
}

export default {
  async fetch(req, env) {
    if (req.method === "OPTIONS") return new Response(null, { headers: CORS });

    const url  = new URL(req.url);
    const path = url.pathname;

    // ── Auth ────────────────────────────────────────────────────────────────
    const secret = env.RELAY_SECRET;
    if (!secret) return jsonResp({ error: "RELAY_SECRET not configured" }, 500);

    const token = req.headers.get("X-Relay-Token") || url.searchParams.get("token") || "";
    if (token !== secret) return jsonResp({ error: "Unauthorized" }, 401);

    // ── Health check ────────────────────────────────────────────────────────
    if (path === "/healthz" || path === "/test-env") {
      const envApi  = (env && env.TIEULAM_API) || "";
      const domains = buildDomains(envApi);
      const probeResults = await Promise.allSettled(
        domains.slice(0, 4).map(base =>
          fetch(base + "/matches/graph", {
            method: "POST", headers: TIEULAM_HDR,
            body: JSON.stringify({ queries: [], limit: 1, page: 1 }),
            signal: AbortSignal.timeout(6000),
          }).then(r => ({ base, status: r.status, ok: r.ok || r.status === 422 }))
        )
      );
      const cfCache = await cacheGet();
      return jsonResp({
        ok: true,
        env: {
          relay_secret_set: !!secret,
          relay_secret_len: secret.length,
          tieulam_api_env: envApi || "(not set)",
        },
        domains_probe: domains.slice(0, 4),
        probe_results: probeResults.map(p =>
          p.status === "fulfilled" ? p.value : { error: p.reason?.message }
        ),
        memory_cache_size: _lastGoodData.length,
        cf_cache_ts: cfCache?.ts ?? null,
        cf_cache_size: cfCache?.data?.length ?? 0,
        current_api_base: _apiBase,
      });
    }

    if (path === "/status") {
      const cfCache = await cacheGet();
      return jsonResp({
        api_base: _apiBase,
        tieulam_api_env: (env && env.TIEULAM_API) || "(not set)",
        disc_age_ms: Date.now() - _apiDiscoveredAt,
        memory_cache: _lastGoodData.length,
        cf_cache_age_ms: cfCache ? Date.now() - cfCache.ts : null,
        cf_cache_size: cfCache?.data?.length ?? 0,
      });
    }

    // ── Memory cache ─────────────────────────────────────────────────────────
    if (_lastGoodData.length && Date.now() - _lastGoodTs < DATA_FRESH_MS)
      return jsonResp({ data: _lastGoodData, count: _lastGoodData.length, cached: true });

    // ── Fetch từ TieuLam API (direct → Replit relay fallback) ─────────────────
    let reqBody = { queries: [], limit: 50, page: 1 };
    try { reqBody = await req.clone().json(); } catch (_) {}

    try {
      const base = await getApiBase(env);

      // 1. Thử fetch trực tiếp
      let r = await fetch(`${base}/matches/graph`, {
        method: "POST",
        headers: TIEULAM_HDR,
        body: JSON.stringify(reqBody),
        signal: AbortSignal.timeout(12_000),
      });

      // 2. WAF block (4xx) → route qua Replit relay
      if (!r.ok && r.status >= 400 && r.status < 500) {
        const replitBase = (env.REPLIT_RELAY_URL || "").trim().replace(/\/$/, "");
        if (replitBase) {
          const rr = await fetch(`${replitBase}/tieulam`, {
            method: "POST",
            headers: {
              "Content-Type": "application/json",
              "X-Relay-Token": secret,
            },
            body: JSON.stringify({ api_base: base, body: reqBody }),
            signal: AbortSignal.timeout(20_000),
          });
          if (rr.ok) {
            const rd   = await rr.json();
            const data = Array.isArray(rd) ? rd : (rd.data || rd.matches || []);
            _lastGoodData = data; _lastGoodTs = Date.now();
            await cachePut(data, base);
            return jsonResp({ data, count: data.length, api_base: base, via: "replit" });
          }
        }
        _apiDiscoveredAt = 0;
        if (_lastGoodData.length)
          return jsonResp({ data: _lastGoodData, count: _lastGoodData.length, cached: true, stale: true, upstream_status: r.status });
        const cfCache = await cacheGet();
        if (cfCache?.data?.length)
          return jsonResp({ data: cfCache.data, count: cfCache.data.length, cached: true, stale: true, from_cf_cache: true, upstream_status: r.status });
        return jsonResp({ data: [], count: 0, error: `upstream_${r.status}`, api_base: base });
      }

      // 3. 5xx → rediscover, cache fallback
      if (!r.ok) {
        _apiDiscoveredAt = 0;
        if (_lastGoodData.length)
          return jsonResp({ data: _lastGoodData, count: _lastGoodData.length, cached: true, stale: true, upstream_status: r.status });
        const cfCache = await cacheGet();
        if (cfCache?.data?.length)
          return jsonResp({ data: cfCache.data, count: cfCache.data.length, cached: true, stale: true, from_cf_cache: true, upstream_status: r.status });
        return jsonResp({ data: [], count: 0, error: `upstream_${r.status}`, api_base: base });
      }

      const d    = await r.json();
      const data = Array.isArray(d) ? d : (d.data || d.matches || []);
      _lastGoodData = data; _lastGoodTs = Date.now();
      await cachePut(data, base);
      return jsonResp({ data, count: data.length, api_base: base });

    } catch (err) {
      _apiDiscoveredAt = 0;
      if (_lastGoodData.length)
        return jsonResp({ data: _lastGoodData, count: _lastGoodData.length, cached: true, stale: true, error: err.message });
      const cfCache = await cacheGet();
      if (cfCache?.data?.length)
        return jsonResp({ data: cfCache.data, count: cfCache.data.length, cached: true, stale: true, from_cf_cache: true, error: err.message });
      return jsonResp({ data: [], count: 0, error: err.message });
    }
  },
};
