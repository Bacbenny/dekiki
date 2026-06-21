// ════════════════════════════════════════════════════════════════════════════
// tieulam-relay — Worker v4
// Fixes: proper Referer/UA headers, dynamic domain list (VN tz),
//        parallel probe, NEVER 502, /healthz test endpoint
// ════════════════════════════════════════════════════════════════════════════

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

// Dynamic domain list — VN UTC+7, today first
function buildDomains() {
  const vnMs = Date.now() + 7 * 3_600_000;
  const domains = [];
  for (let i = 0; i <= 7; i++) {
    const d = new Date(vnMs + i * 86_400_000);
    domains.push(fmtDomain(d));
  }
  for (let i = 1; i <= 14; i++) {
    const d = new Date(vnMs - i * 86_400_000);
    domains.push(fmtDomain(d));
  }
  return [...new Set(domains)];
}

function fmtDomain(d) {
  const dd   = String(d.getUTCDate()).padStart(2, "0");
  const mm   = String(d.getUTCMonth() + 1).padStart(2, "0");
  const yyyy = d.getUTCFullYear();
  return `https://api.tlap${dd}${mm}${yyyy}.com`;
}

// In-memory state
let _apiBase  = ""; // set lazily in handler (CF Workers: Date.now()=0 at module init)
let _apiDiscAt = 0;
let _cache    = [];
let _cacheTs  = 0;

const URL_MS   = 3_600_000; // re-discover API URL every 1h
const CACHE_MS = 1_800_000; // serve memory cache for 30min

const CORS = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Methods": "GET,POST,OPTIONS",
  "Access-Control-Allow-Headers": "Content-Type,X-Relay-Token",
};

function jsonResp(body, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { ...CORS, "Content-Type": "application/json" },
  });
}

// ─── Discovery ────────────────────────────────────────────────────────────────
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
  const candidates = domains.slice(0, 6);
  const controllers = candidates.map(() => new AbortController());
  const probes = candidates.map((base, i) =>
    fetch(base + "/matches/graph", {
      method: "POST", headers: TIEULAM_HDR,
      body: JSON.stringify({ queries: [], limit: 1, page: 1 }),
      signal: controllers[i].signal,
    }).then(r => {
      if (r.ok || r.status === 422) {
        controllers.forEach((c, j) => j !== i && c.abort());
        return base;
      }
      throw new Error(`${r.status}`);
    })
  );
  try { return await Promise.any(probes); } catch (_) { return null; }
}

async function getBase() {
  if (!_apiBase) _apiBase = buildDomains()[0]; // lazy init
  if (Date.now() - _apiDiscAt < URL_MS) return _apiBase;

  const fromFront = await discoverFromFrontend();
  if (fromFront) { _apiBase = fromFront; _apiDiscAt = Date.now(); return _apiBase; }

  const found = await probeDomains(buildDomains());
  if (found) { _apiBase = found; _apiDiscAt = Date.now(); return _apiBase; }

  _apiDiscAt = Date.now() - URL_MS + 300_000; // retry in 5min
  return _apiBase;
}

// ─── Handler ──────────────────────────────────────────────────────────────────
export default {
  async fetch(req, env) {
    if (req.method === "OPTIONS") return new Response(null, { headers: CORS });

    const url  = new URL(req.url);
    const path = url.pathname;

    // Auth
    const sec   = env.RELAY_SECRET || "";
    const token = req.headers.get("X-Relay-Token") || url.searchParams.get("token") || "";
    if (sec && token !== sec) return jsonResp({ error: "Unauthorized" }, 401);

    // Health / env test
    if (path === "/healthz" || path === "/test-env") {
      const domains = buildDomains();
      const probeResults = await Promise.allSettled(
        domains.slice(0, 3).map(base =>
          fetch(base + "/matches/graph", {
            method: "POST", headers: TIEULAM_HDR,
            body: JSON.stringify({ queries: [], limit: 1, page: 1 }),
            signal: AbortSignal.timeout(5000),
          }).then(r => ({ base, status: r.status, ok: r.ok || r.status === 422 }))
        )
      );
      return jsonResp({
        ok: true,
        env: { relay_secret_set: !!sec, relay_secret_len: sec.length },
        domains_today_first: domains.slice(0, 3),
        probe_results: probeResults.map(p =>
          p.status === "fulfilled" ? p.value : { error: p.reason?.message }
        ),
        memory_cache_size: _cache.length,
        current_api_base: _apiBase,
      });
    }

    // Serve fresh memory cache
    if (_cache.length && Date.now() - _cacheTs < CACHE_MS)
      return jsonResp({ data: _cache, count: _cache.length, cached: true });

    // Fetch fresh
    try {
      const base = await getBase();
      let reqBody = { queries: [], limit: 50, page: 1 };
      try { reqBody = await req.clone().json(); } catch (_) {}

      const r = await fetch(base + "/matches/graph", {
        method: "POST",
        headers: TIEULAM_HDR,
        body: JSON.stringify(reqBody),
        signal: AbortSignal.timeout(12000),
      });

      if (!r.ok) {
        _apiDiscAt = 0; // force re-discovery
        if (_cache.length)
          return jsonResp({ data: _cache, count: _cache.length, cached: true, stale: true, upstream: r.status });
        return jsonResp({ data: [], count: 0, error: `upstream_${r.status}`, api_base: base });
      }

      const d = await r.json();
      const data = Array.isArray(d) ? d : (d.data || d.matches || []);
      _cache = data; _cacheTs = Date.now();
      return jsonResp({ data, count: data.length, api_base: base });

    } catch (err) {
      _apiDiscAt = 0;
      if (_cache.length)
        return jsonResp({ data: _cache, count: _cache.length, cached: true, stale: true, error: err.message });
      return jsonResp({ data: [], count: 0, error: err.message });
    }
  },
};
