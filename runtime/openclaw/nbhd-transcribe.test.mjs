import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import fs from "node:fs/promises";
import http from "node:http";
import os from "node:os";
import path from "node:path";
import { after, afterEach, test } from "node:test";
import { createRequire } from "node:module";
import { fileURLToPath } from "node:url";

const require = createRequire(import.meta.url);
const { formatForPath } = require("./nbhd-transcribe.js");
const runtimeDir = path.dirname(fileURLToPath(import.meta.url));
const projectRoot = path.resolve(runtimeDir, "..", "..");
const scriptPath = path.join(runtimeDir, "nbhd-transcribe.js");
const entrypointPath = path.join(runtimeDir, "entrypoint.sh");
const dockerfilePath = path.join(projectRoot, "Dockerfile.openclaw");
const tempDir = await fs.mkdtemp(path.join(os.tmpdir(), "nbhd-transcribe-test-"));
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

function runShim(mediaPath, port) {
  return new Promise((resolve, reject) => {
    const child = spawn(process.execPath, [scriptPath, mediaPath], {
      env: {
        ...process.env,
        NBHD_API_BASE_URL: `http://127.0.0.1:${port}/`,
        NBHD_INTERNAL_API_KEY: "test-internal-key",
        NBHD_TENANT_ID: "tenant-test-id",
      },
      stdio: ["ignore", "pipe", "pipe"],
    });
    let stdout = "";
    let stderr = "";
    child.stdout.setEncoding("utf8").on("data", (chunk) => {
      stdout += chunk;
    });
    child.stderr.setEncoding("utf8").on("data", (chunk) => {
      stderr += chunk;
    });
    child.once("error", reject);
    child.once("close", (code) => resolve({ code, stdout, stderr }));
  });
}

afterEach(async () => {
  await Promise.all([...openServers].map((server) => close(server)));
});

after(async () => {
  await fs.rm(tempDir, { recursive: true, force: true });
});

test("posts audio to Django and prints only the transcription", async () => {
  const mediaPath = path.join(tempDir, "voice.WEBM");
  const audio = Buffer.from("mock audio bytes");
  await fs.writeFile(mediaPath, audio);

  let received;
  const server = http.createServer((request, response) => {
    const chunks = [];
    request.on("data", (chunk) => chunks.push(chunk));
    request.on("end", () => {
      received = {
        method: request.method,
        url: request.url,
        headers: request.headers,
        body: JSON.parse(Buffer.concat(chunks).toString("utf8")),
      };
      response.writeHead(200, { "Content-Type": "application/json" });
      response.end(JSON.stringify({ text: "hello from Django" }));
    });
  });
  const port = await listen(server);

  const result = await runShim(mediaPath, port);

  assert.equal(result.code, 0);
  assert.equal(result.stdout, "hello from Django\n");
  assert.equal(result.stderr, "");
  assert.equal(received.method, "POST");
  assert.equal(received.url, "/api/internal/transcribe/");
  assert.equal(received.headers["x-nbhd-internal-key"], "test-internal-key");
  assert.equal(received.headers["x-nbhd-tenant-id"], "tenant-test-id");
  assert.deepEqual(received.body, {
    input_audio: {
      data: audio.toString("base64"),
      format: "webm",
    },
  });
});

test("fails closed on a non-200 response without printing transcription text", async () => {
  const mediaPath = path.join(tempDir, "failure.mp3");
  await fs.writeFile(mediaPath, "mock audio bytes");
  const server = http.createServer((_request, response) => {
    response.writeHead(503, { "Content-Type": "application/json" });
    response.end(JSON.stringify({ text: "must not reach stdout" }));
  });
  const port = await listen(server);

  const result = await runShim(mediaPath, port);

  assert.notEqual(result.code, 0);
  assert.equal(result.stdout, "");
  assert.match(result.stderr, /HTTP 503/);
  assert.doesNotMatch(result.stderr, /must not reach stdout/);
});

test("fails closed on an invalid JSON response", async () => {
  const mediaPath = path.join(tempDir, "invalid.wav");
  await fs.writeFile(mediaPath, "mock audio bytes");
  const server = http.createServer((_request, response) => {
    response.writeHead(200, { "Content-Type": "text/plain" });
    response.end("not JSON");
  });
  const port = await listen(server);

  const result = await runShim(mediaPath, port);

  assert.notEqual(result.code, 0);
  assert.equal(result.stdout, "");
  assert.match(result.stderr, /invalid JSON/);
});

test("maps supported audio extensions and aliases to Django formats", () => {
  const cases = {
    "clip.aac": "aac",
    "clip.flac": "flac",
    "clip.m4a": "m4a",
    "clip.mp4": "m4a",
    "clip.mp3": "mp3",
    "clip.mpeg": "mp3",
    "clip.oga": "ogg",
    "clip.ogg": "ogg",
    "clip.opus": "ogg",
    "clip.wav": "wav",
    "clip.wave": "wav",
    "clip.weba": "webm",
    "clip.webm": "webm",
  };
  for (const [mediaPath, expected] of Object.entries(cases)) {
    assert.equal(formatForPath(mediaPath), expected, mediaPath);
  }
  assert.throws(() => formatForPath("clip.aiff"), /unsupported audio extension/);
});

test("entrypoint does not read direct provider API keys", async () => {
  const entrypoint = await fs.readFile(entrypointPath, "utf8");
  assert.doesNotMatch(entrypoint, /\bOPENAI_API_KEY\b/);
  assert.doesNotMatch(entrypoint, /\bANTHROPIC_API_KEY\b/);
  assert.match(entrypoint, /\bCLAUDE_CODE_OAUTH_TOKEN\b/);
});

test("Dockerfile installs the shim at the configured CLI command path", async () => {
  const dockerfile = await fs.readFile(dockerfilePath, "utf8");
  assert.match(
    dockerfile,
    /COPY runtime\/openclaw\/nbhd-transcribe\.js \/usr\/local\/bin\/nbhd-transcribe/,
  );
  assert.match(dockerfile, /chmod \+x [^\n]*\/usr\/local\/bin\/nbhd-transcribe/);
});
