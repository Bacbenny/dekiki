// dekki-relay — Cloudflare Worker (Service Worker format)
// Resolves Footy Live sources via sportsembed handshake (protobuf + WASM crypto),
// tries every available embed server, then proxies playlist + segments.
// Also supports /footylive/<id>/raw mode that returns a 302 redirect to the real m3u8.

const FOOTYLIVE_STREAMS_URL = "https://footylive.vercel.app/api/streams/";
const WATCHFOOTY_MATCH_URL = "https://api.watchfooty.st/api/v1/match/";
const EMBED_ORIGIN = "https://sportsembed.su";
const USER_AGENT =
  "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36";

const CORS_HEADERS = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Methods": "GET,HEAD,OPTIONS",
  "Access-Control-Allow-Headers": "Content-Type,Authorization,X-Relay-Token",
};

function jsonResp(body, status) {
  status = status || 200;
  return new Response(JSON.stringify(body), {
    status: status,
    headers: Object.assign({}, CORS_HEADERS, { "Content-Type": "application/json; charset=utf-8" }),
  });
}

function bytesToBase64(bytes) {
  var binary = "";
  for (var i = 0; i < bytes.length; i++) binary += String.fromCharCode(bytes[i]);
  return btoa(binary);
}

function base64ToBytes(value) {
  var binary = atob(value);
  var bytes = new Uint8Array(binary.length);
  for (var i = 0; i < binary.length; i++) bytes[i] = binary.charCodeAt(i);
  return bytes;
}

function base64UrlEncode(value) {
  return bytesToBase64(new TextEncoder().encode(value))
    .replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

function base64UrlDecode(value) {
  var padded = value.replace(/-/g, "+").replace(/_/g, "/") + "=".repeat((4 - (value.length % 4)) % 4);
  return new TextDecoder().decode(base64ToBytes(padded));
}

function hexStr(bytes) {
  return Array.from(bytes, function(b) { return b.toString(16).padStart(2, "0"); }).join("");
}

function concatBytes() {
  var arrays = Array.prototype.slice.call(arguments);
  var size = arrays.reduce(function(t, a) { return t + a.length; }, 0);
  var out = new Uint8Array(size);
  var off = 0;
  for (var i = 0; i < arrays.length; i++) { out.set(arrays[i], off); off += arrays[i].length; }
  return out;
}

function varint(n) {
  var bytes = [], v = n;
  while (v > 0x7f) { bytes.push((v & 0x7f) | 0x80); v >>>= 7; }
  bytes.push(v);
  return new Uint8Array(bytes);
}

function protoField(field, value) {
  var body = new TextEncoder().encode(value);
  return concatBytes(new Uint8Array([(field << 3) | 2]), varint(body.length), body);
}

function encodeRequest(data) {
  return concatBytes(protoField(1, data.category), protoField(2, data.slug),
    protoField(3, data.stream), protoField(4, data.matchId));
}

function pack(op, chunks) {
  var parts = [new Uint8Array([op])];
  for (var i = 0; i < chunks.length; i++) {
    var len = new Uint8Array(4);
    new DataView(len.buffer).setUint32(0, chunks[i].length, true);
    parts.push(len, chunks[i]);
  }
  return concatBytes.apply(null, parts);
}

var wasmInstance = null;
function getWasm(env) {
  if (wasmInstance) return Promise.resolve(wasmInstance);
  return WebAssembly.instantiate(env.STREAM_LOCK).then(function(result) {
    var instance = result && result.instance ? result.instance : result;
    if (!instance || !instance.exports) throw new Error("WASM instance has no exports");
    wasmInstance = instance.exports;
    return wasmInstance;
  });
}

function wasmDispatch(env, input) {
  return getWasm(env).then(function(wasm) {
    var ptr = wasm.zonl3736033c71(input.length, 1);
    var inputMemory = new Uint8Array(wasm.memory.buffer);
    inputMemory.set(input, ptr);
    var retptr = wasm.yojc788d654767(-8);
    wasm.juut545fd2befc(retptr, ptr, input.length);
    var view = new DataView(wasm.memory.buffer);
    var outPtr = view.getUint32(retptr, true);
    var outLen = view.getUint32(retptr + 4, true);
    return new Uint8Array(wasm.memory.buffer).slice(outPtr, outPtr + outLen);
  });
}

function resolveEmbed(env, embedUrl) {
  var url = new URL(embedUrl);
  var m = url.pathname.match(/^\/embed\/(\d+)\/([^/]+)\/([^/]+)\/(\d+)\/?$/);
  if (!m || url.hostname !== "sportsembed.su") return Promise.reject(new Error("Invalid embed URL"));

  var matchId = m[1], slug = m[2], category = m[3], stream = m[4];
  var body = encodeRequest({ matchId: matchId, slug: slug, category: category, stream: stream });
  var nonce = crypto.getRandomValues(new Uint8Array(32));

  return wasmDispatch(env, pack(0x17, [body, nonce])).then(function(factor) {
    if (factor.length !== 16) throw new Error("Invalid client factor");
    return wasmDispatch(env, pack(0x29, [body, nonce, factor])).then(function(proofBytes) {
      var proof = new TextDecoder().decode(proofBytes);
      if (!/^[0-9a-f]{64}$/.test(proof)) throw new Error("Invalid client proof");
      return fetch(EMBED_ORIGIN + "/api/get", {
        method: "POST",
        headers: {
          "Content-Type": "application/octet-stream", Origin: EMBED_ORIGIN,
          Referer: EMBED_ORIGIN + "/embed/" + matchId + "/" + slug + "/" + category + "/" + stream,
          "User-Agent": USER_AGENT, "x-client-nonce": bytesToBase64(nonce),
          "x-client-factor": bytesToBase64(factor), "x-client-proof": proof,
        },
        body: body,
      });
    }).then(function(upstream) {
      if (!upstream.ok) throw new Error("Embed /api/get returned " + upstream.status);
      return upstream.arrayBuffer().then(function(buf) {
        var encrypted = new Uint8Array(buf);
        var live = upstream.headers.get("x-live") || "";
        var edge = base64ToBytes(upstream.headers.get("x-edge") || "");
        var bodyTag = base64ToBytes(upstream.headers.get("x-body-tag") || "");
        var keyHex = live.split("_").pop() || "";
        var key = Uint8Array.from(keyHex.match(/.{2}/g) || [], function(h) { return parseInt(h, 16); });
        if (key.length !== 16 || edge.length !== 16 || bodyTag.length !== 8)
          throw new Error("Invalid embed response headers");
        return wasmDispatch(env, pack(0x3b, [encrypted, concatBytes(key, edge), nonce, factor, bodyTag]));
      }).then(function(streamUrlBytes) {
        var streamUrl = new TextDecoder().decode(streamUrlBytes).trim();
        if (!streamUrl.startsWith("https://")) throw new Error("Embed did not return an HLS URL");
        return { streamUrl: streamUrl, embed: matchId + "/" + slug + "/" + category + "/" + stream };
      });
    });
  });
}

function hmac(secret, value) {
  return crypto.subtle.importKey("raw", new TextEncoder().encode(secret),
    { name: "HMAC", hash: "SHA-256" }, false, ["sign"]).then(function(key) {
      return crypto.subtle.sign("HMAC", key, new TextEncoder().encode(value));
    }).then(function(sig) { return hexStr(new Uint8Array(sig)); });
}

function buildProxyUrl(origin, target, embed, secret, expires) {
  return hmac(secret, expires + "|" + embed + "|" + target).then(function(sig) {
    var url = new URL(origin + "/footylive/" + embed.split("/")[0]);
    url.searchParams.set("u", base64UrlEncode(target));
    url.searchParams.set("e", base64UrlEncode(embed));
    url.searchParams.set("x", String(expires));
    url.searchParams.set("s", sig);
    return url.toString();
  });
}

function verifyProxy(requestUrl, env) {
  var url = new URL(requestUrl);
  var encodedTarget = url.searchParams.get("u"), encodedEmbed = url.searchParams.get("e");
  var expires = url.searchParams.get("x"), provided = url.searchParams.get("s");
  if (!encodedTarget || !encodedEmbed || !expires || !provided)
    return Promise.reject(new Error("Missing proxy parameters"));
  if (Number(expires) < Date.now()) return Promise.reject(new Error("Expired stream URL"));
  var target = base64UrlDecode(encodedTarget), embed = base64UrlDecode(encodedEmbed);
  if (!target.startsWith("https://")) return Promise.reject(new Error("Invalid stream target"));
  return hmac(env.RELAY_SECRET, expires + "|" + embed + "|" + target).then(function(expected) {
    if (provided !== expected) throw new Error("Invalid proxy signature");
    return { target: target, embed: embed };
  });
}

function absoluteUrl(value, base) { return new URL(value, base).toString(); }
function isPlaylist(contentType, body) {
  var head = new TextDecoder().decode(body.slice(0, Math.min(body.length, 256)));
  return head.includes("#EXTM3U") || contentType.includes("mpegurl") ||
    (contentType.includes("text/plain") && head.includes("#EXT"));
}

function fetchHlsPlaylist(streamUrl, embed) {
  var referers = [
    EMBED_ORIGIN + "/embed/" + embed,
    EMBED_ORIGIN + "/",
    "https://footylive.vercel.app/",
  ];
  var index = 0;
  function next() {
    if (index >= referers.length) {
      return Promise.resolve(new Response(null, { status: 403 }));
    }
    var referer = referers[index++];
    return fetch(streamUrl, {
      headers: {
        Accept: "application/vnd.apple.mpegurl, application/x-mpegURL, */*",
        Referer: referer,
        Origin: EMBED_ORIGIN,
        "User-Agent": USER_AGENT,
        "Sec-Fetch-Dest": "empty",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "cross-site",
      },
    }).then(function(response) {
      if (response.ok) return response;
      return next();
    });
  }
  return next();
}

function rewritePlaylistIndexed(text, baseUrl, embed, origin, secret, expires) {
  var lines = text.split(/\r?\n/), results = new Array(lines.length), promises = [];
  for (var i = 0; i < lines.length; i++) {
    var line = lines[i], trimmed = line.trim();
    if (!trimmed) { results[i] = line; continue; }
    if (trimmed.startsWith("#")) {
      if (trimmed.includes('URI="') && !trimmed.includes('URI="data:')) {
        var rebuilt = line, matches = Array.from(line.matchAll(/URI="([^"]+)"/g)), chain = Promise.resolve(rebuilt);
        for (var j = 0; j < matches.length; j++) (function(m) {
          if (!m[1].startsWith("data:")) chain = chain.then(function(current) {
            return buildProxyUrl(origin, absoluteUrl(m[1], baseUrl), embed, secret, expires)
              .then(function(proxied) { return current.replace(m[0], 'URI="' + proxied + '"'); });
          });
        })(matches[j]);
        (function(idx, c) { promises.push(c.then(function(result) { results[idx] = result; })); })(i, chain);
      } else results[i] = line;
      continue;
    }
    (function(idx, segLine) {
      promises.push(buildProxyUrl(origin, absoluteUrl(segLine, baseUrl), embed, secret, expires)
        .then(function(proxied) { results[idx] = proxied; }));
    })(i, trimmed);
  }
  return Promise.all(promises).then(function() { return results.join("\n"); });
}

function serveProxy(requestUrl, env) {
  return verifyProxy(requestUrl, env).then(function(verified) {
    return fetch(verified.target, { headers: { Accept: "*/*", Referer: EMBED_ORIGIN + "/", "User-Agent": USER_AGENT } });
  }).then(function(upstream) {
    if (!upstream.ok) return jsonResp({ error: "Upstream returned " + upstream.status }, 502);
    var contentType = upstream.headers.get("content-type") || "";
    return upstream.arrayBuffer().then(function(body) {
      var bodyBytes = new Uint8Array(body);
      if (!isPlaylist(contentType, bodyBytes)) return new Response(body, {
        status: 200, headers: Object.assign({}, CORS_HEADERS, { "Content-Type": contentType || "video/mp2t", "Cache-Control": "no-store" })
      });
      var expires = Date.now() + 1800000;
      return rewritePlaylistIndexed(new TextDecoder().decode(bodyBytes), verified.target,
        verified.embed, new URL(requestUrl).origin, env.RELAY_SECRET, expires).then(function(rewritten) {
          return new Response(rewritten, { status: 200, headers: Object.assign({}, CORS_HEADERS, {
            "Content-Type": "application/vnd.apple.mpegurl", "Cache-Control": "no-store"
          })});
        });
    });
  });
}

function pickSources(payload, matchId) {
  var streams = Array.isArray(payload.streams) ? payload.streams : [];
  var valid = streams.filter(function(s) { return s && s.url; });
  if (!valid.length) throw new Error("No streams for match " + matchId);
  var watch = valid.filter(function(s) { return String(s.provider || "").toLowerCase().includes("watchfooty"); });
  return (watch.length ? watch : valid).map(function(s) { return s.url; });
}

function resolveFirstWorking(env, embedUrls, requestUrl, matchId) {
  var index = 0, failures = [];
  function next() {
    if (index >= embedUrls.length) {
      throw new Error("All " + embedUrls.length + " Footy sources failed: " + failures.join("; "));
    }
    var embedUrl = embedUrls[index++];
    return resolveEmbed(env, embedUrl).then(function(resolved) {
      return fetchHlsPlaylist(resolved.streamUrl, resolved.embed).then(function(playlist) {
        if (!playlist.ok) {
          failures.push(resolved.embed + " HLS " + playlist.status);
          return next();
        }
        return playlist.text().then(function(text) {
          if (!text.includes("#EXTM3U")) {
            failures.push(resolved.embed + " invalid playlist");
            return next();
          }
          var expires = Date.now() + 1800000;
          return rewritePlaylistIndexed(text, resolved.streamUrl, resolved.embed,
            new URL(requestUrl).origin, env.RELAY_SECRET, expires).then(function(rewritten) {
              return new Response(rewritten, { status: 200, headers: Object.assign({}, CORS_HEADERS, {
                "Content-Type": "application/vnd.apple.mpegurl", "Cache-Control": "no-store"
              })});
            });
        });
      });
    }).catch(function(error) {
      failures.push(embedUrl + " " + String(error && error.message || error));
      return next();
    });
  }
  return next();
}

// Raw mode: resolve embed and return 302 redirect to the real m3u8 URL
function resolveRawRedirect(env, embedUrls, matchId) {
  var index = 0, failures = [];
  function next() {
    if (index >= embedUrls.length) {
      throw new Error("All " + embedUrls.length + " Footy sources failed: " + failures.join("; "));
    }
    var embedUrl = embedUrls[index++];
    return resolveEmbed(env, embedUrl).then(function(resolved) {
      return { streamUrl: resolved.streamUrl, embed: resolved.embed };
    }).catch(function(error) {
      failures.push(embedUrl + " " + String(error && error.message || error));
      return next();
    });
  }
  return next();
}

function fetchMatchPayload(matchId) {
  var headers = { Accept: "application/json", "User-Agent": USER_AGENT };
  return fetch(FOOTYLIVE_STREAMS_URL + encodeURIComponent(matchId), { headers: headers })
    .then(function(api) {
      if (api.ok) {
        return api.json().then(function(payload) {
          if (Array.isArray(payload && payload.streams) && payload.streams.length) return payload;
          throw new Error("Footy Live API returned no streams");
        });
      }
      throw new Error("Footy Live API returned " + api.status);
    })
    .catch(function() {
      return fetch(WATCHFOOTY_MATCH_URL + encodeURIComponent(matchId), { headers: headers })
        .then(function(api) {
          if (!api.ok) throw new Error("WatchFooty API returned " + api.status);
          return api.json().then(function(match) {
            var streams = Array.isArray(match && match.streams) ? match.streams : [];
            if (!streams.length) throw new Error("WatchFooty API returned no streams");
            return { matchTitle: match.title || "", streams: streams };
          });
        });
    });
}

function handleMatch(request, env, matchId) {
  var requestUrl = new URL(request.url);
  if (requestUrl.searchParams.has("u")) return serveProxy(request.url, env);
  return fetchMatchPayload(matchId).then(function(payload) {
    return resolveFirstWorking(env, pickSources(payload, matchId), request.url, matchId);
  });
}

// Raw mode: /footylive/<id>/raw — resolve and 302 redirect to real m3u8
function handleRawRedirect(request, env, matchId) {
  return fetchMatchPayload(matchId).then(function(payload) {
    return resolveRawRedirect(env, pickSources(payload, matchId), matchId);
  }).then(function(resolved) {
    return new Response(null, {
      status: 302,
      headers: Object.assign({}, CORS_HEADERS, {
        "Location": resolved.streamUrl,
        "Cache-Control": "no-store",
      }),
    });
  });
}

function serviceBindings() {
  var bindings = {};
  try { if (typeof STREAM_LOCK !== "undefined") bindings.STREAM_LOCK = STREAM_LOCK; } catch (_) {}
  try { if (typeof RELAY_SECRET !== "undefined") bindings.RELAY_SECRET = RELAY_SECRET; } catch (_) {}
  return bindings;
}



async function handleFilm4k() {
  var loginRes = await fetch("https://film4k.net/api/auth/login", { method: "POST", headers: { "Content-Type": "application/json", "User-Agent": USER_AGENT }, body: JSON.stringify({ email: "dvdvbac@gmail.com", password: "Bac12345" }) });
  if (!loginRes.ok) return jsonResp({ error: "Film4k login failed", status: loginRes.status }, 502);
  var loginData = await loginRes.json();
  var token = loginData.token || loginData.access_token;
  if (!token) return jsonResp({ error: "Film4k login returned no token" }, 502);
  var channelsRes = await fetch("https://film4k.net/api/tv/channels", { headers: { Accept: "application/json", "User-Agent": USER_AGENT, Authorization: "Bearer " + token } });
  if (!channelsRes.ok) return jsonResp({ error: "Film4k channels failed", status: channelsRes.status }, 502);
  return jsonResp({ token: token, channels: await channelsRes.json() });
}

addEventListener("fetch", function(event) {
  var request = event.request, bindings = serviceBindings();
  if (request.method === "OPTIONS") {
    event.respondWith(new Response(null, { status: 204, headers: CORS_HEADERS })); return;
  }
  var url = new URL(request.url);
  if (url.pathname === "/film4k/fetch-all" && request.method === "GET") {
    event.respondWith(handleFilm4k().catch(function(error) { return jsonResp({ error: "Film4k proxy failed", detail: String(error && error.message || error) }, 502); })); return;
  }
  if (url.pathname === "/healthz") {
    event.respondWith(jsonResp({ ok: true, worker: "dekki-relay", resolver: "sportsembed-handshake",
      relay_secret_set: Boolean(bindings.RELAY_SECRET), wasm_loaded: Boolean(bindings.STREAM_LOCK) })); return;
  }
  // Raw redirect mode: /footylive/<id>/raw
  var rawMatch = url.pathname.match(/^\/footylive\/([^/]+)\/raw$/);
  if (rawMatch && (request.method === "GET" || request.method === "HEAD")) {
    event.respondWith(handleRawRedirect(request, bindings, decodeURIComponent(rawMatch[1]))
      .catch(function(error) { return jsonResp({ error: "Footy Live raw redirect failed",
        detail: String(error && error.message || error) }, 502); }));
    return;
  }
  var footyMatch = url.pathname.match(/^\/footylive\/([^/]+)$/);
  if (footyMatch && (request.method === "GET" || request.method === "HEAD")) {
    event.respondWith(handleMatch(request, bindings, decodeURIComponent(footyMatch[1]))
      .catch(function(error) { return jsonResp({ error: "Footy Live resolver failed",
        detail: String(error && error.message || error) }, 502); }));
    return;
  }
  event.respondWith(jsonResp({ error: "Not found. Use /healthz" }, 404));
});
