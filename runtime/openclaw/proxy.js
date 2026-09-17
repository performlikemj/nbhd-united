/**
 * Lightweight reverse proxy for OpenClaw container.
 *
 * Routes a single ingress port to the two internal HTTP servers:
 *   /telegram-webhook  -> localhost:8787  (webhook server)
 *   everything else    -> localhost:18789 (Gateway API)
 *
 * Uses only built-in Node.js modules — no npm install required.
 */

const http = require("http");

const PROXY_PORT = parseInt(process.env.PROXY_PORT || "8080", 10);
const GATEWAY_PORT = 18789;
const WEBHOOK_PORT = 8787;
const GATEWAY_HEALTH_TIMEOUT_MS = 2000;

// OpenClaw 2026.9.4's gateway REJECTS Bearer-authenticated routes (e.g.
// /tools/invoke — every chat turn + cron tool call) with
// "proxy_attribution_required" when a request carries proxy-forwarded client
// headers it can't attribute to a trusted proxy. Azure Container Apps ingress
// stamps X-Forwarded-* / Forwarded on the hop to this proxy; passing them
// through to the loopback gateway makes 9.4 treat us as an untrusted proxy and
// 403 the request (silently breaks all chat on 9.4). gateway.trustedProxies
// alone does NOT fix it — the forwarded client IP resolves to loopback, still
// "unattributable". The fix the gateway itself recommends is to have the proxy
// rebuild the forwarded headers: strip them so the gateway sees a clean
// loopback request (attributed "direct-local") and applies the Bearer token
// check normally. 9.4's hasForwardedRequestHeaders() keys on `forwarded`,
// `x-real-ip`, and any `x-forwarded-*`, so drop exactly those. Only for the
// gateway hop; the Telegram webhook server (:8787) is left untouched.
function stripForwardedHeaders(headers) {
  const cleaned = { ...headers };
  for (const name of Object.keys(cleaned)) {
    const n = name.toLowerCase();
    if (n === "forwarded" || n === "x-real-ip" || n.startsWith("x-forwarded-")) {
      delete cleaned[name];
    }
  }
  return cleaned;
}

function proxyRequest(req, res, targetPort, stripForwarded = false) {
  const options = {
    hostname: "127.0.0.1",
    port: targetPort,
    path: req.url,
    method: req.method,
    headers: stripForwarded ? stripForwardedHeaders(req.headers) : req.headers,
  };

  const upstream = http.request(options, (upstreamRes) => {
    res.writeHead(upstreamRes.statusCode, upstreamRes.headers);
    upstreamRes.pipe(res);
  });

  upstream.on("error", (err) => {
    console.error(
      `[proxy] upstream error port=${targetPort} path=${req.url}: ${err.message}`
    );
    if (!res.headersSent) {
      res.writeHead(502, { "Content-Type": "application/json" });
    }
    res.end(
      JSON.stringify({
        error: "bad_gateway",
        detail: `upstream ${targetPort} unreachable`,
      })
    );
  });

  req.pipe(upstream);
}

function proxyHealth(res, gatewayPort, timeoutMs) {
  let settled = false;
  const respond = (statusCode, payload) => {
    if (settled) return;
    settled = true;
    res.writeHead(statusCode, { "Content-Type": "application/json" });
    res.end(JSON.stringify(payload));
  };

  const upstream = http.get(
    {
      hostname: "127.0.0.1",
      port: gatewayPort,
      path: "/healthz",
    },
    (upstreamRes) => {
      upstreamRes.resume();
      if (upstreamRes.statusCode >= 200 && upstreamRes.statusCode < 400) {
        respond(200, { status: "ok", proxy: true, gateway: true });
      } else {
        respond(503, {
          status: "unavailable",
          proxy: true,
          gateway: false,
        });
      }
    }
  );

  upstream.setTimeout(timeoutMs, () => {
    upstream.destroy(new Error("gateway health probe timed out"));
  });
  upstream.on("error", () => {
    respond(503, {
      status: "unavailable",
      proxy: true,
      gateway: false,
    });
  });
}

function createProxyServer({
  gatewayPort = GATEWAY_PORT,
  webhookPort = WEBHOOK_PORT,
  healthTimeoutMs = GATEWAY_HEALTH_TIMEOUT_MS,
} = {}) {
  return http.createServer((req, res) => {
    if (req.url === "/proxy-health") {
      proxyHealth(res, gatewayPort, healthTimeoutMs);
      return;
    }

    if (req.url.startsWith("/telegram-webhook")) {
      proxyRequest(req, res, webhookPort);
    } else {
      // Strip proxy-forwarded client headers on the gateway hop (see
      // stripForwardedHeaders) so 2026.9.4 doesn't 403 the Bearer-authed route.
      proxyRequest(req, res, gatewayPort, true);
    }
  });
}

if (require.main === module) {
  const server = createProxyServer();
  server.listen(PROXY_PORT, () => {
    console.log(`[proxy] listening on :${PROXY_PORT}`);
    console.log(`[proxy]   /telegram-webhook -> :${WEBHOOK_PORT}`);
    console.log(`[proxy]   /*                -> :${GATEWAY_PORT}`);
  });

  // Graceful shutdown
  function shutdown(signal) {
    console.log(`[proxy] received ${signal}, shutting down`);
    server.close(() => process.exit(0));
    setTimeout(() => process.exit(1), 10000);
  }

  process.on("SIGTERM", () => shutdown("SIGTERM"));
  process.on("SIGINT", () => shutdown("SIGINT"));
}

module.exports = { createProxyServer, stripForwardedHeaders, GATEWAY_HEALTH_TIMEOUT_MS };
