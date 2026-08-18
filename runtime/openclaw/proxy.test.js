"use strict";

const assert = require("node:assert/strict");
const http = require("node:http");
const { afterEach, test } = require("node:test");

const {
  createProxyServer,
  GATEWAY_HEALTH_TIMEOUT_MS,
} = require("./proxy.js");

const openServers = new Set();

function listen(server) {
  openServers.add(server);
  return new Promise((resolve, reject) => {
    server.once("error", reject);
    server.listen(0, "127.0.0.1", () => {
      server.removeListener("error", reject);
      resolve(server.address().port);
    });
  });
}

function close(server) {
  openServers.delete(server);
  return new Promise((resolve, reject) => {
    server.close((error) => (error ? reject(error) : resolve()));
  });
}

async function requestJson(port) {
  const response = await fetch(`http://127.0.0.1:${port}/proxy-health`);
  return { status: response.status, body: await response.json() };
}

afterEach(async () => {
  await Promise.all([...openServers].map((server) => close(server)));
});

test("proxy health reports a healthy gateway", async () => {
  const gateway = http.createServer((_req, res) => {
    res.writeHead(200);
    res.end("ok");
  });
  const gatewayPort = await listen(gateway);
  const proxy = createProxyServer({ gatewayPort });
  const proxyPort = await listen(proxy);

  const result = await requestJson(proxyPort);

  assert.equal(result.status, 200);
  assert.deepEqual(result.body, {
    status: "ok",
    proxy: true,
    gateway: true,
  });
});

test("proxy health returns 503 when the gateway is unreachable", async () => {
  const unused = http.createServer();
  const gatewayPort = await listen(unused);
  await close(unused);
  const proxy = createProxyServer({ gatewayPort });
  const proxyPort = await listen(proxy);

  const result = await requestJson(proxyPort);

  assert.equal(result.status, 503);
  assert.equal(result.body.gateway, false);
});

test("proxy health times out a stalled gateway", async () => {
  const gateway = http.createServer();
  const gatewayPort = await listen(gateway);
  const proxy = createProxyServer({ gatewayPort, healthTimeoutMs: 25 });
  const proxyPort = await listen(proxy);

  const result = await requestJson(proxyPort);

  assert.equal(result.status, 503);
  assert.equal(result.body.gateway, false);
  assert.equal(GATEWAY_HEALTH_TIMEOUT_MS, 2000);
});
