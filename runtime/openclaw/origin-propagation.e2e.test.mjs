import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { createHmac } from "node:crypto";
import { once } from "node:events";
import { chmod, mkdir, mkdtemp, readFile, realpath, rm, writeFile } from "node:fs/promises";
import http from "node:http";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { test } from "node:test";

const TOOL_NAME = "nbhd_datebook_add_apple_reminder";
const THREAD_ID = "00000000-0000-4000-8000-000000000001";
const GATEWAY_TOKEN = "origin-repro-gateway-token";
const TENANT_ID = "00000000-0000-4000-8000-000000000002";
const INTERNAL_API_KEY = "internal-origin-repro-key";

function sendJson(response, status, payload) {
  const body = JSON.stringify(payload);
  response.writeHead(status, {
    "content-type": "application/json",
    "content-length": Buffer.byteLength(body),
  });
  response.end(body);
}

function sendStream(response, chunks) {
  response.writeHead(200, {
    "content-type": "text/event-stream",
    "cache-control": "no-cache",
    connection: "keep-alive",
  });
  for (const chunk of chunks) response.write(`data: ${JSON.stringify(chunk)}\n\n`);
  response.end("data: [DONE]\n\n");
}

async function requestJson(request) {
  let raw = "";
  request.setEncoding("utf8");
  for await (const chunk of request) raw += chunk;
  return raw ? JSON.parse(raw) : {};
}

async function listen(server) {
  server.listen(0, "127.0.0.1");
  await once(server, "listening");
  return server.address().port;
}

async function unusedPort() {
  const probe = http.createServer();
  const port = await listen(probe);
  await new Promise((resolve, reject) => probe.close((error) => (error ? reject(error) : resolve())));
  return port;
}

async function waitForGateway(url, child, logs) {
  const deadline = Date.now() + 30_000;
  while (Date.now() < deadline) {
    if (child.exitCode !== null) {
      throw new Error(`OpenClaw exited before it was ready (${child.exitCode})\n${logs()}`);
    }
    try {
      const response = await fetch(`${url}/healthz`);
      if (response.ok) return;
    } catch {
      // The gateway has not bound its socket yet.
    }
    await new Promise((resolve) => setTimeout(resolve, 200));
  }
  throw new Error(`Timed out waiting for OpenClaw gateway\n${logs()}`);
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

async function runCommand(command, args, options) {
  const child = spawn(command, args, { ...options, stdio: ["ignore", "pipe", "pipe"] });
  let stdout = "";
  let stderr = "";
  child.stdout.on("data", (chunk) => { stdout += chunk; });
  child.stderr.on("data", (chunk) => { stderr += chunk; });
  const [code] = await once(child, "exit");
  assert.equal(code, 0, `${command} ${args.join(" ")} failed\n${stdout}\n${stderr}`);
  return { stdout, stderr };
}

function fakeRuntime() {
  const observations = {
    datebookBody: undefined,
    datebookResponse: undefined,
    requiredParameters: undefined,
    toolResultText: undefined,
  };
  const server = http.createServer(async (request, response) => {
    try {
      const body = await requestJson(request);
      if (request.url === "/v1/chat/completions") {
        const advertised = Array.isArray(body.tools)
          ? body.tools.find((tool) => tool?.function?.name === TOOL_NAME)
          : undefined;
        if (advertised) observations.requiredParameters = advertised.function.parameters.required;
        const hasToolResult = Array.isArray(body.messages)
          && body.messages.some((message) => message?.role === "tool");
        if (hasToolResult) {
          observations.toolResultText = body.messages.find((message) => message?.role === "tool")?.content;
        }
        const base = {
          id: "chatcmpl-origin-repro",
          object: "chat.completion.chunk",
          created: Math.floor(Date.now() / 1000),
          model: "fake-model",
        };
        const chunks = hasToolResult
          ? [
              {
                ...base,
                choices: [{
                  index: 0,
                  delta: { role: "assistant", content: "done" },
                  finish_reason: null,
                }],
              },
              {
                ...base,
                choices: [{ index: 0, delta: {}, finish_reason: "stop" }],
                usage: { prompt_tokens: 1, completion_tokens: 1, total_tokens: 2 },
              },
            ]
          : [
              {
                ...base,
                choices: [{
                  index: 0,
                  delta: {
                    role: "assistant",
                    tool_calls: [{
                      index: 0,
                      id: "call-origin-repro",
                      type: "function",
                      function: {
                        name: TOOL_NAME,
                        arguments: JSON.stringify({
                          items: [{ title: "Buy milk" }],
                          direct_user_originated: true,
                        }),
                      },
                    }],
                  },
                  finish_reason: null,
                }],
              },
              {
                ...base,
                choices: [{ index: 0, delta: {}, finish_reason: "tool_calls" }],
                usage: { prompt_tokens: 1, completion_tokens: 1, total_tokens: 2 },
              },
            ];
        sendStream(response, chunks);
        return;
      }
      if (request.url?.endsWith("/datebook/request-create")) {
        observations.datebookBody = body;
        observations.datebookResponse = {
          state: "approval_pending",
          command_id: "",
          approval_surface: "app",
          delivery_state: "available",
          guidance: "Waiting for your approval; the approval is in this conversation. Review it within 24 hours.",
        };
        sendJson(response, 202, observations.datebookResponse);
        return;
      }
      sendJson(response, 404, { error: "not_found" });
    } catch (error) {
      sendJson(response, 500, { error: error.message });
    }
  });
  return { observations, server };
}

async function runPinnedFlow(t, { includeEnforcement = false, messageChannel = "ios", trigger = "chat" } = {}) {
  const configuredBinary = process.env.OPENCLAW_REPRO_BIN;
  assert.ok(configuredBinary, "OPENCLAW_REPRO_BIN must point to the pinned openclaw.mjs");
  const openclawBinary = await realpath(configuredBinary);
  const packageJson = JSON.parse(await readFile(path.join(path.dirname(openclawBinary), "package.json"), "utf8"));
  assert.equal(packageJson.version, "2026.5.28", "the regression must run against the Dockerfile pin");

  const temporaryRoot = await mkdtemp(path.join(os.tmpdir(), "nbhd-origin-e2e-"));
  const stateDirectory = path.join(temporaryRoot, "state");
  const workspaceDirectory = path.join(temporaryRoot, "workspace");
  const configPath = path.join(stateDirectory, "openclaw.json");
  await mkdir(stateDirectory, { recursive: true });
  await mkdir(workspaceDirectory, { recursive: true });
  await writeFile(path.join(workspaceDirectory, "AGENTS.md"), "# Local origin propagation smoke\n");

  const { observations, server } = fakeRuntime();
  const runtimePort = await listen(server);
  const gatewayPort = await unusedPort();
  const datebookPluginPath = fileURLToPath(new URL("./plugins/nbhd-datebook-tools", import.meta.url));
  const enforcementPluginPath = fileURLToPath(new URL("./plugins/nbhd-cron-enforcement", import.meta.url));
  const config = {
    agents: {
      defaults: {
        model: { primary: "repro/fake-model" },
        models: { "repro/fake-model": {} },
        workspace: workspaceDirectory,
        memorySearch: { enabled: false },
      },
    },
    models: {
      mode: "replace",
      pricing: { enabled: false },
      providers: {
        repro: {
          baseUrl: `http://127.0.0.1:${runtimePort}/v1`,
          apiKey: "fake-model-key",
          api: "openai-completions",
          models: [{
            id: "fake-model",
            name: "Fake model",
            reasoning: false,
            input: ["text"],
            cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 },
            contextWindow: 32768,
            maxTokens: 4096,
          }],
        },
      },
    },
    gateway: {
      port: gatewayPort,
      mode: "local",
      bind: "loopback",
      auth: { mode: "token", token: GATEWAY_TOKEN },
      http: { endpoints: { chatCompletions: { enabled: true } } },
    },
    tools: { allow: ["group:plugins"] },
    plugins: {
      allow: includeEnforcement
        ? ["nbhd-datebook-tools", "nbhd-cron-enforcement"]
        : ["nbhd-datebook-tools"],
      entries: includeEnforcement
        ? {
            "nbhd-datebook-tools": { enabled: true },
            "nbhd-cron-enforcement": { enabled: true },
          }
        : { "nbhd-datebook-tools": { enabled: true } },
      load: { paths: includeEnforcement ? [datebookPluginPath, enforcementPluginPath] : [datebookPluginPath] },
      bundledDiscovery: "compat",
    },
  };
  await writeFile(configPath, `${JSON.stringify(config, null, 2)}\n`);
  await chmod(configPath, 0o600);

  let gatewayLogs = "";
  const childEnv = {
    ...process.env,
    NODE_OPTIONS: "",
    OPENCLAW_CONFIG_PATH: configPath,
    OPENCLAW_STATE_DIR: stateDirectory,
    OPENCLAW_DISABLE_BONJOUR: "1",
    NBHD_API_BASE_URL: `http://127.0.0.1:${runtimePort}`,
    NBHD_TENANT_ID: TENANT_ID,
    NBHD_INTERNAL_API_KEY: INTERNAL_API_KEY,
  };
  const child = spawn(process.execPath, [
    openclawBinary,
    "gateway",
    "--port",
    String(gatewayPort),
    "--bind",
    "loopback",
    "--verbose",
  ], {
    env: childEnv,
    stdio: ["ignore", "pipe", "pipe"],
  });
  const appendLogs = (chunk) => {
    gatewayLogs = `${gatewayLogs}${chunk}`.slice(-40_000);
  };
  child.stdout.on("data", appendLogs);
  child.stderr.on("data", appendLogs);

  t.after(async () => {
    await stopChild(child);
    await new Promise((resolve) => server.close(resolve));
    await rm(temporaryRoot, { recursive: true, force: true });
  });

  const gatewayUrl = `http://127.0.0.1:${gatewayPort}`;
  await waitForGateway(gatewayUrl, child, () => gatewayLogs);
  if (trigger === "cron") {
    const cliConnection = ["--url", `ws://127.0.0.1:${gatewayPort}`, "--token", GATEWAY_TOKEN];
    const added = await runCommand(process.execPath, [
      openclawBinary,
      "cron",
      "add",
      "--name",
      "Origin Stamp Repro",
      "--at",
      "+1h",
      "--session",
      "isolated",
      "--message",
      "Add an Apple reminder to buy milk",
      "--no-deliver",
      "--json",
      ...cliConnection,
    ], { env: childEnv });
    const job = JSON.parse(added.stdout);
    assert.ok(job.id, added.stdout);
    await runCommand(process.execPath, [
      openclawBinary,
      "cron",
      "run",
      job.id,
      "--wait",
      "--wait-timeout",
      "30s",
      ...cliConnection,
    ], { env: childEnv });
    return { observations, gatewayLogs: () => gatewayLogs, job };
  }
  const response = await fetch(`${gatewayUrl}/v1/chat/completions`, {
    method: "POST",
    headers: {
      Authorization: `Bearer ${GATEWAY_TOKEN}`,
      "Content-Type": "application/json",
      "X-OpenClaw-Message-Channel": messageChannel,
    },
    body: JSON.stringify({
      model: "openclaw",
      user: `thread:${THREAD_ID}`,
      messages: [{ role: "user", content: "Add a reminder to buy milk" }],
    }),
  });
  const responseBody = await response.text();
  assert.equal(response.status, 200, `${responseBody}\n${gatewayLogs}`);
  return { observations, responseBody, gatewayLogs };
}

test("pinned OpenClaw carries an iOS turn through the real loaded datebook plugin", {
  timeout: 60_000,
}, async (t) => {
  const { observations } = await runPinnedFlow(t);
  assert.deepEqual(
    new Set(observations.requiredParameters),
    new Set(["items", "direct_user_originated"]),
    "the real per-turn factory must advertise both required create parameters",
  );
  assert.equal(observations.datebookBody?.originating_channel, "app");
  assert.equal(observations.datebookBody?.command_type, "reminder_create");
  assert.equal(observations.datebookBody?.direct_user_originated, true);
  assert.match(observations.toolResultText, /the approval is in this conversation/);
  assert.match(observations.toolResultText, /within 24 hours/);
  assert.equal(observations.datebookResponse?.approval_surface, "app");
  assert.equal(observations.datebookResponse?.delivery_state, "available");
});

test("pinned OpenClaw executes the enforcement null override without forwarding origin", {
  timeout: 60_000,
}, async (t) => {
  const { observations, gatewayLogs } = await runPinnedFlow(t, {
    includeEnforcement: true,
    // Use a native channel in this second regression so it exercises only the
    // before_tool_call merge seam; the first test retains the iOS fallback pin.
    messageChannel: "telegram",
  });
  assert.equal(observations.datebookBody?.command_type, "reminder_create");
  assert.equal(Object.hasOwn(observations.datebookBody, "origin"), false, "null override must not be forwarded");
  assert.match(gatewayLogs, /nbhd-cron-enforcement: registered/, "the null assertion must exercise the loaded hook");
});

test("pinned OpenClaw signs origin on a real cron-triggered agent run", {
  timeout: 60_000,
}, async (t) => {
  const { observations, gatewayLogs, job } = await runPinnedFlow(t, {
    includeEnforcement: true,
    messageChannel: "telegram",
    trigger: "cron",
  });
  const origin = observations.datebookBody?.origin;
  assert.match(gatewayLogs(), /nbhd-cron-enforcement: registered/);
  assert.equal(
    origin?.v,
    1,
    `captured body=${JSON.stringify(observations.datebookBody)}\n${gatewayLogs()}`,
  );
  assert.equal(origin?.kind, "cron");
  assert.equal(origin?.tenant_id, TENANT_ID);
  assert.equal(origin?.job_id, job.id);
  assert.equal(typeof origin?.run_id, "string");
  assert.ok(origin.run_id);
  assert.equal(typeof origin?.ts, "number");
  assert.ok(Math.abs(Math.floor(Date.now() / 1000) - origin.ts) <= 900);
  const message = `nbhd-origin.v1|${TENANT_ID}|cron|${origin.run_id}|${job.id}|${origin.ts}`;
  const expected = createHmac("sha256", INTERNAL_API_KEY).update(message).digest("hex");
  assert.equal(origin.sig, expected, "stamp must satisfy Django verify_origin_stamp's HMAC contract");
});
