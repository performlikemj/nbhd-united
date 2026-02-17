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

function proxyRequest(req, res, targetPort) {
  const options = {
    hostname: "127.0.0.1",
    port: targetPort,
    path: req.url,
    method: req.method,
    headers: req.headers,
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

const server = http.createServer((req, res) => {
  // Own health check — does not touch upstreams
  if (req.url === "/proxy-health") {
    res.writeHead(200, { "Content-Type": "application/json" });
    res.end(JSON.stringify({ status: "ok", proxy: true }));
    return;
  }

  if (req.url.startsWith("/telegram-webhook")) {
    proxyRequest(req, res, WEBHOOK_PORT);
  } else {
    proxyRequest(req, res, GATEWAY_PORT);
  }
});

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
