// ════════════════════════════════════════════════════════════════════════════
// dekki — TieuLam Relay Worker v4
// Fixes: proper Referer/UA headers, dynamic domain list (VN tz),
//        parallel probe (Promise.any), CF Cache for cold-start resilience,
//        NEVER 502 — serve stale or empty, /healthz test endpoint
// ════════════════════════════════════════════════════════════════════════════

const TIEULAM_FRONTS = [
  "https://sv2.tieulam.info",
  "https://sv1.tieulam1.live",
];

// Headers that TieuLam API expects (blocks requests without proper Referer)
const TIEULAM_HDR = {
  "Content-Type": "application/json",
  "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
  "Referer": "https://sv2.tieulam.info/",
  "Origin": "https://sv2.tieulam.info",
  "Accept": "application/json, text/plain, */*",
};

// ─── Dynamic domain list (VN UTC+7) ─────────────────────────────────────────
function buildDomains() {
  const vnMs = Date.now() + 7 * 3_600_000; // shift to VN time for date computation
  const domains = [];
  // Today + next 7 days first (TieuLam may pre-register future domains)
  for (let i = 0; i <= 7; i++) {
    const d = new Date(vnMs + i * 86_400_000);
    domains.push(fmtDomain(d));
  }
  // Then past 14 days (fallback for when new domain hasn't appeared yet)
  for (let i = 1; i <= 14; i++) {
    const d = new Date(vnMs - i * 86_400_000);
    domains.push(fmtDomain(d));
  }
  return [...new Set(domains)]; // deduplicate
}

function fmtDomain(d) {
  const dd   = String(d.getUTCDate()).padStart(2, "0");
  const mm   = String(d.getUTCMonth() + 1).padStart(2, "0");
  const yyyy = d.getUTCFullYear();
  return `https://api.tlap${dd}${mm}${yyyy}.com`;
}

// ─── In-memory state (resets on cold start; CF Cache bridges the gap) ───────
let _apiBase        = ""; // set lazily inside request handler (Date.now()=0 at module init in CF Workers)
let _apiDiscoveredAt = 0;
let _lastGoodData   = [];
let _lastGoodTs     = 0;

const API_DISC_MS   = 3_600_000; // re-discover API base every 1h
const DATA_FRESH_MS = 1_800_000; // serve in-memory cache for 30min
const KV_STALE_MS   = 7_200_000; // CF Cache entry: 2h max-age
const PROBE_TIMEOUT = 5_000;     // per-domain probe timeout
const FETCH_TIMEOUT = 12_000;    // main data fetch timeout

// ─── CF Cache API (persistent across instances on same PoP) ─────────────────
const CF_CACHE_KEY = "https://tieulam-relay-internal/matches-v4";

async function cacheGet() {
  try {
    const cached = await caches.default.match(new Request(CF_CACHE_KEY));
    if (!cached) return null;
    const body = await cached.json();
    if (body?.data) return body; // caller checks body.ts for freshness
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

// ─── Discovery: frontend JS scan → parallel probe fallbacks ─────────────────
async function discoverFromFrontend() {
  for (const front of TIEULAM_FRONTS) {
    try {
      const html = await fetch(front, { signal: AbortSignal.timeout(6000) }).then(r => r.text());
      for (const m of html.matchAll(/src="(\/assets\/[^"]+\.js)"/g)) {
        try {
          const js = await fetch(front + m[1], { signal: AbortSignal.timeout(10000) }).then(r => r.text());
          const hit = js.match(/create\(\{baseURL:"(https:\/\/api\.tlap[^"]+)"\}/)
                   || js.match(/"(https:\/\/api\.tlap[\w]+\.com)"/);
          if (hit) return hit[1];
        } catch (_) {}
      }
    } catch (_) {}
  }
  return null;
}

async function probeDomains(domains) {
  // Probe first 6 candidates in parallel; return first that answers
  const candidates = domains.slice(0, 6);
  const controllers = candidates.map(() => new AbortController());

  const probes = candidates.map((base, i) =>
    fetch(base + "/matches/graph", {
      method: "POST",
      headers: TIEULAM_HDR,
      body: JSON.stringify({ queries: [], limit: 1, page: 1 }),
      signal: controllers[i].signal,
    }).then(r => {
      if (r.ok || r.status === 422) {
        controllers.forEach((c, j) => j !== i && c.abort()); // cancel losers
        return base;
      }
      throw new Error(`HTTP ${r.status}`);
    })
  );

  try {
    return await Promise.any(probes);
  } catch (_) {
    return null;
  }
}

async function getApiBase() {
  if (!_apiBase) _apiBase = buildDomains()[0]; // lazy init
  if (Date.now() - _apiDiscoveredAt < API_DISC_MS) return _apiBase;

  // 1. Try frontend JS (most authoritative)
  const fromFront = await discoverFromFrontend();
  if (fromFront) {
    _apiBase = fromFront;
    _apiDiscoveredAt = Date.now();
    return _apiBase;
  }

  // 2. Parallel probe: dynamic domains (today-first) + hardcoded fallback
  const domains = buildDomains();
  const found = await probeDomains(domains);
  if (found) {
    _apiBase = found;
    _apiDiscoveredAt = Date.now();
    return _apiBase;
  }

  // Keep existing base; reset timestamp only partially so we retry in 5min
  _apiDiscoveredAt = Date.now() - API_DISC_MS + 300_000;
  return _apiBase;
}

// ─── CORS headers ────────────────────────────────────────────────────────────
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

// ─── Main handler ─────────────────────────────────────────────────────────────
export default {
  async fetch(req, env) {
    if (req.method === "OPTIONS") return new Response(null, { headers: CORS });

    const url  = new URL(req.url);
    const path = url.pathname;

    // ── Auth ──────────────────────────────────────────────────────────────────
    const secret = env.RELAY_SECRET || "";
    const token  = req.headers.get("X-Relay-Token") || url.searchParams.get("token") || "";
    if (secret && token !== secret) return jsonResp({ error: "Unauthorized" }, 401);

    // ── Health / env test endpoint ─────────────────────────────────────────────
    if (path === "/healthz" || path === "/test-env") {
      const domains = buildDomains();
      // Quick probe first 3 domains (parallel)
      const probeResults = await Promise.allSettled(
        domains.slice(0, 3).map(base =>
          fetch(base + "/matches/graph", {
            method: "POST", headers: TIEULAM_HDR,
            body: JSON.stringify({ queries: [], limit: 1, page: 1 }),
            signal: AbortSignal.timeout(5000),
          }).then(r => ({ base, status: r.status, ok: r.ok || r.status === 422 }))
        )
      );
      const cfCache = await cacheGet();
      return jsonResp({
        ok: true,
        env: {
          relay_secret_set: !!secret,
          relay_secret_len: secret.length,
        },
        domains_today_first: domains.slice(0, 3),
        probe_results: probeResults.map(p =>
          p.status === "fulfilled" ? p.value : { error: p.reason?.message }
        ),
        memory_cache_size: _lastGoodData.length,
        cf_cache_ts: cfCache?.ts ?? null,
        cf_cache_size: cfCache?.data?.length ?? 0,
        current_api_base: _apiBase,
      });
    }

    // ── Status endpoint ────────────────────────────────────────────────────────
    if (path === "/status") {
      const cfCache = await cacheGet();
      return jsonResp({
        api_base: _apiBase,
        discovered_at: _apiDiscoveredAt,
        memory_cache: _lastGoodData.length,
        cf_cache_age_ms: cfCache ? Date.now() - cfCache.ts : null,
        cf_cache_size: cfCache?.data?.length ?? 0,
      });
    }

    // ── Main /matches endpoint ─────────────────────────────────────────────────
    // 1. Serve in-memory cache if fresh
    if (_lastGoodData.length && Date.now() - _lastGoodTs < DATA_FRESH_MS) {
      return jsonResp({ data: _lastGoodData, count: _lastGoodData.length, cached: true });
    }

    // 2. Try to fetch fresh data
    try {
      const base = await getApiBase();
      let reqBody = { queries: [], limit: 50, page: 1 };
      try { reqBody = await req.clone().json(); } catch (_) {}

      const r = await fetch(base + "/matches/graph", {
        method: "POST",
        headers: TIEULAM_HDR,
        body: JSON.stringify(reqBody),
        signal: AbortSignal.timeout(FETCH_TIMEOUT),
      });

      if (!r.ok) {
        // Force re-discovery next call
        _apiDiscoveredAt = 0;
        // Serve stale in-memory cache
        if (_lastGoodData.length) {
          return jsonResp({ data: _lastGoodData, count: _lastGoodData.length, cached: true, stale: true, upstream_status: r.status });
        }
        // Try CF Cache
        const cfCache = await cacheGet();
        if (cfCache?.data?.length) {
          return jsonResp({ data: cfCache.data, count: cfCache.data.length, cached: true, stale: true, from_cf_cache: true, upstream_status: r.status });
        }
        // Nothing available — return empty (NOT 502) so caller knows to skip
        return jsonResp({ data: [], count: 0, error: `upstream_${r.status}`, api_base: base });
      }

      const d = await r.json();
      const data = Array.isArray(d) ? d : (d.data || d.matches || []);

      // Update both caches
      _lastGoodData = data;
      _lastGoodTs   = Date.now();
      await cachePut(data, base);

      return jsonResp({ data, count: data.length, api_base: base });

    } catch (err) {
      // Network error / timeout
      _apiDiscoveredAt = 0; // force re-discovery

      // Stale in-memory
      if (_lastGoodData.length) {
        return jsonResp({ data: _lastGoodData, count: _lastGoodData.length, cached: true, stale: true, error: err.message });
      }
      // CF Cache
      const cfCache = await cacheGet();
      if (cfCache?.data?.length) {
        return jsonResp({ data: cfCache.data, count: cfCache.data.length, cached: true, stale: true, from_cf_cache: true, error: err.message });
      }
      // Empty fallback — still 200 so main.py doesn't hard-fail
      return jsonResp({ data: [], count: 0, error: err.message });
    }
  },
};
