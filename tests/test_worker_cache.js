const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");

const context = vm.createContext({
  URL,
  Date,
  atob,
  Response,
  addEventListener() {},
});
const workerSource = fs.readFileSync(
  new URL("../workers/dekki.js", `file://${__filename}`).pathname,
  "utf8",
);
vm.runInContext(workerSource, context);

function jwt(exp) {
  const payload = Buffer.from(JSON.stringify({ exp })).toString("base64url");
  return `eyJhbGciOiJub25lIn0.${payload}.signature`;
}

const current = Math.floor(Date.now() / 1000);
const tokenExp = current + 3600;
const authUrl = `https://cdn.example/live.m3u8?auth=${jwt(tokenExp)}`;
const cdnUrl = `https://cdn.example/live.m3u8?cdntoken=${jwt(tokenExp)}`;
const timestampUrl = `https://cdn.example/live.m3u8?timestamp=${tokenExp}`;

assert.equal(context.streamExpiry(authUrl), tokenExp);
assert.equal(context.streamExpiry(cdnUrl), tokenExp);
assert.equal(context.streamExpiry(timestampUrl), tokenExp);
assert.equal(
  context.streamCacheIsFresh({ url: authUrl, ts: Date.now() }),
  true,
);
assert.equal(
  context.streamCacheIsFresh({
    url: `https://cdn.example/live.m3u8?auth=${jwt(current - 10)}`,
    ts: Date.now(),
  }),
  false,
);
assert.equal(
  context.streamCacheIsFresh({
    url: "https://cdn.example/live.m3u8",
    ts: Date.now() - 181_000,
  }),
  false,
);

(async () => {
  context.FILM4K_USERNAME = "test-user";
  context.FILM4K_PASSWORD = "test-password";
  const cookies = [];
  context.fetch = async (input, init = {}) => {
    const url = String(input);
    if (url.endsWith("/api/auth/signin")) {
      return new Response(null, {
        status: 200,
        headers: { "set-cookie": "session=fresh; Path=/" },
      });
    }

    const cookie = init.headers && init.headers.Cookie;
    cookies.push(cookie);
    if (cookie === "session=old") return new Response(null, { status: 401 });
    return new Response(
      JSON.stringify({ url: "https://cdn.example/live.m3u8?auth=fresh-token" }),
      { status: 200, headers: { "content-type": "application/json" } },
    );
  };

  const result = await context.resolveChannelStream("channel-42", "session=old");
  assert.equal(result.stream, "https://cdn.example/live.m3u8?auth=fresh-token");
  assert.ok(cookies.includes("session=old"));
  assert.ok(cookies.includes("session=fresh"));
  console.log("Worker cache expiry and expired-session retry checks passed.");
})().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
