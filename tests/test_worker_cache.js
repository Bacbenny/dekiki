const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");

const workerFile = new URL("../workers/dekki.js", `file://${__filename}`).pathname;
const workerSource = fs.readFileSync(workerFile, "utf8");

function createWorkerContext(bindings = {}) {
  const context = vm.createContext({
    URL,
    Date,
    atob,
    Request,
    Response,
    setTimeout,
    addEventListener() {},
    ...bindings,
  });
  vm.runInContext(workerSource, context);
  return context;
}

const context = createWorkerContext({
  URL,
  Date,
  atob,
});

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

  let attempts = 0;
  context.fetch = async () => {
    attempts += 1;
    if (attempts <= 3) return new Response(null, { status: 503 });
    return new Response(
      JSON.stringify({ url: "https://cdn.example/live.m3u8?auth=retry-token" }),
      { status: 200, headers: { "content-type": "application/json" } },
    );
  };
  const transientResult = await context.resolveChannelStream(
    "channel-transient",
    "session=valid",
  );
  assert.equal(
    transientResult.stream,
    "https://cdn.example/live.m3u8?auth=retry-token",
  );
  assert.equal(attempts, 6);

  const failingKv = {
    async get() { throw new Error("simulated KV outage"); },
    async getWithMetadata() { throw new Error("simulated KV outage"); },
    async put() { throw new Error("simulated KV outage"); },
    async delete() { throw new Error("simulated KV outage"); },
  };
  const kvContext = createWorkerContext({
    FILM4K_KV: failingKv,
    console: { warn() {}, error() {}, log() {} },
  });
  kvContext.FILM4K_USERNAME = "test-user";
  kvContext.FILM4K_PASSWORD = "test-password";
  kvContext.fetch = async (input) => {
    if (String(input).endsWith("/api/auth/signin")) {
      return new Response(null, {
        status: 200,
        headers: { "set-cookie": "session=kv-fallback; Path=/" },
      });
    }
    return new Response(
      JSON.stringify({ url: "https://cdn.example/live.m3u8?auth=kv-fallback-token" }),
      { status: 200, headers: { "content-type": "application/json" } },
    );
  };
  const kvResponse = await kvContext.handle(
    new Request("https://worker.example/film4k/stream/channel/channel-kv"),
  );
  assert.equal(kvResponse.status, 302);
  assert.equal(
    kvResponse.headers.get("Location"),
    "https://cdn.example/live.m3u8?auth=kv-fallback-token",
  );
  console.log("Worker expiry, retry, and KV fail-open checks passed.");
})().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
