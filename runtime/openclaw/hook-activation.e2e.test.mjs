import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { once } from "node:events";
import { chmod, mkdir, mkdtemp, readFile, realpath, rm, writeFile } from "node:fs/promises";
import http from "node:http";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { test } from "node:test";

import { assertHookOnlyPluginsActivated } from "./plugins/hook-activation-contract.mjs";

async function unusedPort() {
  const server = http.createServer();
  server.listen(0, "127.0.0.1");
  await once(server, "listening");
  const port = server.address().port;
  await new Promise((resolve, reject) => server.close((error) => (error ? reject(error) : resolve())));
  return port;
}

async function stopChild(child) {
  if (child.exitCode !== null) return;
  child.kill("SIGTERM");
  const stopped = await Promise.race([
    once(child, "exit").then(() => true),
    new Promise((resolve) => setTimeout(() => resolve(false), 5_000)),
  ]);
  if (!stopped && child.exitCode === null) {
    child.kill("SIGKILL");
    await once(child, "exit");
  }
}

async function waitForActivatedPluginsLine(child, logs) {
  const deadline = Date.now() + 30_000;
  while (Date.now() < deadline) {
    if (/http server listening \(\d+ plugins?:/u.test(logs())) return;
    if (child.exitCode !== null) {
      throw new Error(`OpenClaw exited before announcing activated plugins (${child.exitCode})\n${logs()}`);
    }
    await new Promise((resolve) => setTimeout(resolve, 200));
  }
  throw new Error(`Timed out waiting for OpenClaw's activated-plugin line\n${logs()}`);
}

test("pinned gateway activates every source-derived hook-only plugin in the maximal config", {
  timeout: 60_000,
}, async (t) => {
  const configuredBinary = process.env.OPENCLAW_REPRO_BIN;
  const maximalConfigPath = process.env.OPENCLAW_MAXIMAL_CONFIG;
  assert.ok(configuredBinary, "OPENCLAW_REPRO_BIN must point to the pinned openclaw.mjs");
  assert.ok(maximalConfigPath, "OPENCLAW_MAXIMAL_CONFIG must point to the generated maximal config");

  const openclawBinary = await realpath(configuredBinary);
  const packageJson = JSON.parse(await readFile(path.join(path.dirname(openclawBinary), "package.json"), "utf8"));
  assert.equal(packageJson.version, "2026.5.28", "the activation regression must run against the Dockerfile pin");

  const temporaryRoot = await mkdtemp(path.join(os.tmpdir(), "nbhd-hook-activation-e2e-"));
  const stateDirectory = path.join(temporaryRoot, "state");
  const workspaceDirectory = path.join(temporaryRoot, "workspace");
  const configPath = path.join(stateDirectory, "openclaw.json");
  await mkdir(stateDirectory, { recursive: true });
  await mkdir(workspaceDirectory, { recursive: true });
  await writeFile(path.join(workspaceDirectory, "AGENTS.md"), "# Hook activation smoke\n");

  const config = JSON.parse(await readFile(maximalConfigPath, "utf8"));
  const gatewayPort = await unusedPort();
  const localPluginRoot = fileURLToPath(new URL("./plugins", import.meta.url));
  config.gateway.port = gatewayPort;
  config.agents.defaults.workspace = workspaceDirectory;
  config.agents.defaults.heartbeat = { every: "0m" };
  for (const channel of Object.values(config.channels ?? {})) {
    if (channel && typeof channel === "object") channel.enabled = false;
  }
  config.plugins.load.paths = config.plugins.load.paths.map((pluginPath) => (
    pluginPath.replace(/^\/opt\/nbhd\/plugins/u, localPluginRoot)
  ));
  await writeFile(configPath, `${JSON.stringify(config, null, 2)}\n`);
  await chmod(configPath, 0o600);

  let gatewayLogs = "";
  const child = spawn(process.execPath, [
    openclawBinary,
    "gateway",
    "--port",
    String(gatewayPort),
    "--bind",
    "loopback",
    "--verbose",
  ], {
    env: {
      ...process.env,
      NODE_OPTIONS: "",
      OPENCLAW_CONFIG_PATH: configPath,
      OPENCLAW_STATE_DIR: stateDirectory,
      OPENCLAW_DISABLE_BONJOUR: "1",
      OPENROUTER_API_KEY: "sk-smoke-test-dummy",
      NBHD_API_BASE_URL: "http://127.0.0.1:9",
      NBHD_TENANT_ID: "00000000-0000-4000-8000-000000000123",
      NBHD_INTERNAL_API_KEY: "smoke-test-internal-key",
    },
    stdio: ["ignore", "pipe", "pipe"],
  });
  const appendLogs = (chunk) => {
    gatewayLogs = `${gatewayLogs}${chunk}`.slice(-80_000);
  };
  child.stdout.on("data", appendLogs);
  child.stderr.on("data", appendLogs);

  t.after(async () => {
    await stopChild(child);
    await rm(temporaryRoot, { recursive: true, force: true });
  });

  await waitForActivatedPluginsLine(child, () => gatewayLogs);
  const result = assertHookOnlyPluginsActivated(gatewayLogs, localPluginRoot);
  console.log(result.line);
  console.log(`Activated hook-only plugins: ${result.expected.join(", ")}`);
});
