// film4k-proxy — Cloudflare Worker (Service Worker format)
// Proxy for Film4k API — Cloudflare edge nodes in Vietnam bypass geo-restriction.

const API_BASE = "https://film4k.net/api";
const CORS_HEADERS = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Methods": "GET, POST, PUT, DELETE, OPTIONS",
  "Access-Control-Allow-Headers": "Content-Type, Authorization, X-Client-Info, Apikey",
};
const UA =
  "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36";

function jsonResp(body, status) {
  return new Response(JSON.stringify(body), {
    status: status || 200,
    headers: Object.assign({}, CORS_HEADERS, { "Content-Type": "application/json" }),
  });
}

async function handleRequest(request) {
  if (request.method === "OPTIONS") {
    return new Response(null, { status: 200, headers: CORS_HEADERS });
  }

  try {
    var url = new URL(request.url);
    var path = url.pathname;

    // /fetch-all — login + get channels in one call
    if (path === "/fetch-all") {
      var loginRes = await fetch(API_BASE + "/auth/login", {
        method: "POST",
        headers: { "Content-Type": "application/json", "User-Agent": UA },
        body: JSON.stringify({
          email: "dvdvbac@gmail.com",
          password: "Bac12345",
        }),
      });

      if (!loginRes.ok) {
        var loginErr = await loginRes.text();
        return jsonResp(
          { error: "Login failed: " + loginRes.status, detail: loginErr.substring(0, 500) },
          401
        );
      }

      var loginData = await loginRes.json();
      var token = loginData.token || loginData.access_token;
      if (!token) {
        return jsonResp({ error: "No token in login response", data: loginData }, 500);
      }

      var channelsRes = await fetch(API_BASE + "/tv/channels", {
        method: "GET",
        headers: {
          "Content-Type": "application/json",
          "User-Agent": UA,
          Authorization: "Bearer " + token,
          Accept: "application/json",
        },
      });

      if (!channelsRes.ok) {
        var chErr = await channelsRes.text();
        return jsonResp(
          { error: "Channels failed: " + channelsRes.status, detail: chErr.substring(0, 500) },
          502
        );
      }

      var channelsData = await channelsRes.json();
      return jsonResp({ token: token, channels: channelsData }, 200);
    }

    // /login — POST email/password
    if (path === "/login" && request.method === "POST") {
      var body = await request.json();
      var res = await fetch(API_BASE + "/auth/login", {
        method: "POST",
        headers: { "Content-Type": "application/json", "User-Agent": UA },
        body: JSON.stringify(body),
      });
      return new Response(res.body, {
        status: res.status,
        headers: Object.assign({}, CORS_HEADERS, { "Content-Type": "application/json" }),
      });
    }

    // /channels — GET with ?token=xxx
    if (path === "/channels" && request.method === "GET") {
      var tokenParam = url.searchParams.get("token") || "";
      var hdrs = {
        "Content-Type": "application/json",
        "User-Agent": UA,
        Accept: "application/json",
      };
      if (tokenParam) hdrs["Authorization"] = "Bearer " + tokenParam;

      var chRes = await fetch(API_BASE + "/tv/channels", {
        method: "GET",
        headers: hdrs,
      });
      return new Response(chRes.body, {
        status: chRes.status,
        headers: Object.assign({}, CORS_HEADERS, { "Content-Type": "application/json" }),
      });
    }

    return jsonResp({ error: "Not found", path: path }, 404);
  } catch (err) {
    return jsonResp({ error: String(err) }, 500);
  }
}

addEventListener("fetch", function (event) {
  event.respondWith(handleRequest(event.request));
});
