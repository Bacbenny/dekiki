// hoiquan-relay — Cloudflare Worker v1
// Relay proxy cho Hội Quán TV API — bypass 403 từ GitHub Actions IP
// POST / { api_base? }  →  GET {api_base}/fixtures/unfinished
// Auth: X-Relay-Token header == RELAY_SECRET env binding

const HOIQUAN_FRONTS = [
  "https://sv2.hoiquan4.live",
  "https://hoiquan.live",
];

const UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36";

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

// Discover API base từ frontend JS bundle
async function discoverApiBase() {
  for (const front of HOIQUAN_FRONTS) {
    try {
      const html = await fetch(front, {
        headers: { "User-Agent": UA },
        signal: AbortSignal.timeout(8000),
      }).then(r => r.text());

      const scripts = [...html.matchAll(/src="(\/[^"]+\.js)"/g)].map(m => m[1]);
      for (const s of scripts.slice(0, 8)) {
        try {
          const js = await fetch(front + s, {
            headers: { "User-Agent": UA },
            signal: AbortSignal.timeout(12000),
          }).then(r => r.text());

          const hit = js.match(/baseURL\s*:\s*["'](https:\/\/sv[^"']+)["']/)
            || js.match(/["'](https:\/\/sv\.hoiquantv[^"']+)["']/)
            || js.match(/["'](https:\/\/api\.hoiquan[^"']+)["']/);
          if (hit) return hit[1].replace(/\/$/, "");
        } catch (_) {}
      }
    } catch (_) {}
  }
  return "https://sv.hoiquantv.xyz/api/v1/external";
}

let _apiBase = "";
let _apiDiscAt = 0;
const URL_TTL = 3_600_000;

async function getBase(env) {
  const envApi = (env && env.HOIQUAN_API) || "";
  if (envApi) return envApi.replace(/\/$/, "");
  if (_apiBase && Date.now() - _apiDiscAt < URL_TTL) return _apiBase;
  const found = await discoverApiBase();
  _apiBase = found;
  _apiDiscAt = Date.now();
  return _apiBase;
}

export default {
  async fetch(req, env) {
    if (req.method === "OPTIONS") return new Response(null, { headers: CORS });

    const url  = new URL(req.url);
    const path = url.pathname;

    // Auth
    const sec = env.RELAY_SECRET;
    if (!sec) return jsonResp({ error: "RELAY_SECRET not configured" }, 500);
    const tok = req.headers.get("X-Relay-Token") || url.searchParams.get("token") || "";
    if (tok !== sec) return jsonResp({ error: "Unauthorized" }, 401);

    // Health check
    if (path === "/healthz") {
      return jsonResp({
        ok: true,
        worker: "hoiquan-relay",
        env_api: (env && env.HOIQUAN_API) || "(not set)",
        current_base: _apiBase,
      });
    }

    // Parse body
    let body = {};
    try { body = await req.json(); } catch (_) {}

    const apiBase = (body.api_base || await getBase(env)).replace(/\/$/, "");
    const frontendUrl = HOIQUAN_FRONTS[0];

    try {
      const r = await fetch(`${apiBase}/fixtures/unfinished`, {
        method: "GET",
        headers: {
          "User-Agent": UA,
          "Referer": `${frontendUrl}/`,
          "Origin": frontendUrl,
          "Accept": "application/json, text/plain, */*",
        },
        signal: AbortSignal.timeout(15_000),
      });

      if (!r.ok) {
        _apiDiscAt = 0;
        return jsonResp({ data: [], error: `upstream_${r.status}`, api_base: apiBase }, r.status);
      }

      const d = await r.json();
      const data = Array.isArray(d) ? d : (d.data || d.fixtures || []);
      return jsonResp({ data, count: data.length, api_base: apiBase });

    } catch (err) {
      _apiDiscAt = 0;
      return jsonResp({ data: [], error: err.message, api_base: apiBase }, 502);
    }
  },
};
