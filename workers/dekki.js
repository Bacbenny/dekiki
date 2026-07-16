// dekki-relay — Cloudflare Worker v2
// Combined relay proxy: Hội Quán TV, Khán Đài A, Vòng Cấm TV
// Auth: X-Relay-Token header == RELAY_SECRET env binding
//
// Routes:
//   POST /hoiquan   → GET {HOIQUAN_API}/fixtures/unfinished
//   POST /khandaia  → GET {KHANDAIA_API}/fixtures/unfinished
//   POST /vongcam   → GET {VONGCAM_API} với Access-Token header
//   GET  /healthz   → health check

const CORS = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Methods": "GET,POST,OPTIONS",
  "Access-Control-Allow-Headers": "Content-Type,X-Relay-Token,Authorization",
};

const UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36";

const HOIQUAN_DEFAULTS  = ["https://sv.hoiquantv.xyz/api/v1/external", "https://sv2.hoiquan4.live"];
const KHANDAIA_DEFAULTS = ["https://sv.khandai-a.xyz/api/v1/external", "https://tructiep.khandaia.link"];
const VONGCAM_DEFAULT   = "https://sv.bugiotv.xyz/internal/api/matches";
const VONGCAM_FRONTEND  = "https://sv2.vongcam3.live";
const VONGCAM_TOKEN_DEF = "AB321C";

function jsonResp(body, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { ...CORS, "Content-Type": "application/json" },
  });
}

function authCheck(req, env, url) {
  const sec = env.RELAY_SECRET;
  if (!sec) return jsonResp({ error: "RELAY_SECRET not configured" }, 500);
  const tok = req.headers.get("X-Relay-Token") || url.searchParams.get("token") || "";
  if (tok !== sec) return jsonResp({ error: "Unauthorized" }, 401);
  return null; // OK
}

// ─── Hội Quán TV ─────────────────────────────────────────────────────────────
async function handleHoiquan(env) {
  const apiBase = (env.HOIQUAN_API || HOIQUAN_DEFAULTS[0]).replace(/\/$/, "");
  const frontend = HOIQUAN_DEFAULTS[1];
  const url = `${apiBase}/fixtures/unfinished`;
  const r = await fetch(url, {
    headers: {
      "User-Agent": UA,
      "Referer": `${frontend}/`,
      "Origin": frontend,
      "Accept": "application/json, text/plain, */*",
    },
    signal: AbortSignal.timeout(15_000),
  });
  if (!r.ok) {
    return jsonResp({ data: [], error: `upstream_${r.status}`, api_base: apiBase }, r.status);
  }
  const d = await r.json();
  const data = d.success ? (d.data || []) : (Array.isArray(d) ? d : []);
  return jsonResp({ data, count: data.length, api_base: apiBase });
}

// ─── Khán Đài A ──────────────────────────────────────────────────────────────
async function handleKhandaia(env) {
  const apiBase = (env.KHANDAIA_API || KHANDAIA_DEFAULTS[0]).replace(/\/$/, "");
  const frontend = "https://tructiep.khandaia.link";
  const url = `${apiBase}/fixtures/unfinished`;
  const r = await fetch(url, {
    headers: {
      "User-Agent": UA,
      "Referer": `${frontend}/`,
      "Origin": frontend,
      "Accept": "application/json, text/plain, */*",
    },
    signal: AbortSignal.timeout(15_000),
  });
  if (!r.ok) {
    return jsonResp({ data: [], error: `upstream_${r.status}`, api_base: apiBase }, r.status);
  }
  const d = await r.json();
  const data = d.success ? (d.data || []) : (Array.isArray(d) ? d : []);
  return jsonResp({ data, count: data.length, api_base: apiBase });
}

// ─── Vòng Cấm TV ─────────────────────────────────────────────────────────────
async function handleVongcam(body, env) {
  const apiUrl      = (body.api_url      || env.VONGCAM_API   || VONGCAM_DEFAULT).trim();
  const accessToken = (body.access_token || env.VONGCAM_ACCESS_TOKEN || VONGCAM_TOKEN_DEF).trim();
  const r = await fetch(apiUrl, {
    headers: {
      "Access-Token": accessToken,
      "Referer":      `${VONGCAM_FRONTEND}/`,
      "Origin":       VONGCAM_FRONTEND,
      "Accept":       "application/json, text/plain, */*",
      "User-Agent":   UA,
    },
    signal: AbortSignal.timeout(15_000),
  });
  if (!r.ok) {
    return jsonResp({ data: [], error: `upstream_${r.status}`, api_url: apiUrl }, r.status);
  }
  const d = await r.json();
  const data = Array.isArray(d) ? d : (d.data || d.matches || d.items || []);
  return jsonResp({ data, count: data.length, api_url: apiUrl });
}

// ─── Main handler ─────────────────────────────────────────────────────────────
export default {
  async fetch(req, env) {
    if (req.method === "OPTIONS") return new Response(null, { headers: CORS });

    const url  = new URL(req.url);
    const path = url.pathname.replace(/\/$/, "") || "/";

    // Auth
    const authErr = authCheck(req, env, url);
    if (path !== "/healthz" && authErr) return authErr;

    // Health check (no auth needed for basic liveness, but show env status)
    if (path === "/healthz") {
      const tok = req.headers.get("X-Relay-Token") || "";
      const sec = env.RELAY_SECRET || "";
      const authed = sec && tok === sec;
      return jsonResp({
        ok: true,
        worker: "dekki-relay-v2",
        routes: ["/hoiquan", "/khandaia", "/vongcam"],
        relay_secret_set: !!sec,
        hoiquan_api:  env.HOIQUAN_API  || "(default)",
        khandaia_api: env.KHANDAIA_API || "(default)",
        vongcam_api:  env.VONGCAM_API  || "(default)",
        authed,
      });
    }

    // Parse body (for vongcam token pass-through)
    let body = {};
    try { body = await req.clone().json(); } catch (_) {}

    try {
      if (path === "/hoiquan")  return await handleHoiquan(env);
      if (path === "/khandaia") return await handleKhandaia(env);
      if (path === "/vongcam")  return await handleVongcam(body, env);
    } catch (err) {
      return jsonResp({ data: [], error: err.message }, 502);
    }

    return jsonResp({ error: "Not found. Use /hoiquan, /khandaia, /vongcam, /healthz" }, 404);
  },
};
