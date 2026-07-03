// vongcam-relay — Cloudflare Worker v1
// Relay proxy cho Vòng Cấm TV (bugiotv) API — bypass 403 từ GitHub Actions IP
// POST / { access_token?, api_url? }  →  GET {api_url} với Access-Token header
// Auth: X-Relay-Token header == RELAY_SECRET env binding

const DEFAULT_API_URL = "https://sv.bugiotv.xyz/internal/api/matches";
const FRONTEND_URL    = "https://sv2.vongcam3.live";
const DEFAULT_TOKEN   = "AB321C";

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

export default {
  async fetch(req, env) {
    if (req.method === "OPTIONS") return new Response(null, { headers: CORS });

    const url  = new URL(req.url);
    const path = url.pathname;

    const sec = env.RELAY_SECRET;
    if (!sec) return jsonResp({ error: "RELAY_SECRET not configured" }, 500);
    const tok = req.headers.get("X-Relay-Token") || url.searchParams.get("token") || "";
    if (tok !== sec) return jsonResp({ error: "Unauthorized" }, 401);

    if (path === "/healthz") {
      return jsonResp({
        ok: true,
        worker: "vongcam-relay",
        default_api: DEFAULT_API_URL,
      });
    }

    let body = {};
    try { body = await req.json(); } catch (_) {}

    const accessToken = (body.access_token || (env && env.VONGCAM_ACCESS_TOKEN) || DEFAULT_TOKEN).trim();
    const apiUrl      = (body.api_url      || DEFAULT_API_URL).trim();

    try {
      const r = await fetch(apiUrl, {
        method: "GET",
        headers: {
          "Access-Token": accessToken,
          "Referer":      `${FRONTEND_URL}/`,
          "Origin":       FRONTEND_URL,
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

    } catch (err) {
      return jsonResp({ data: [], error: err.message, api_url: apiUrl }, 502);
    }
  },
};
