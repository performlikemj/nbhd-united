#!/usr/bin/env node
"use strict";

const fs = require("node:fs/promises");
const path = require("node:path");

const FORMAT_ALIASES = new Map([
  [".aac", "aac"],
  [".flac", "flac"],
  [".m4a", "m4a"],
  [".mp4", "m4a"],
  [".mp4a", "m4a"],
  [".mp3", "mp3"],
  [".mpeg", "mp3"],
  [".mpga", "mp3"],
  [".oga", "ogg"],
  [".ogg", "ogg"],
  [".opus", "ogg"],
  [".wav", "wav"],
  [".wave", "wav"],
  [".weba", "webm"],
  [".webm", "webm"],
]);

function formatForPath(mediaPath) {
  const extension = path.extname(mediaPath).toLowerCase();
  const format = FORMAT_ALIASES.get(extension);
  if (!format) {
    throw new Error(`unsupported audio extension: ${extension || "(none)"}`);
  }
  return format;
}

async function transcribe(mediaPath, env = process.env, fetchImpl = fetch) {
  if (!mediaPath) {
    throw new Error("usage: nbhd-transcribe <MediaPath>");
  }

  const apiBaseUrl = String(env.NBHD_API_BASE_URL || "").replace(/\/+$/, "");
  const internalKey = String(env.NBHD_INTERNAL_API_KEY || "");
  const tenantId = String(env.NBHD_TENANT_ID || "");
  if (!apiBaseUrl || !internalKey || !tenantId) {
    throw new Error("internal transcription credentials are unavailable");
  }

  const audio = await fs.readFile(mediaPath);
  const response = await fetchImpl(`${apiBaseUrl}/api/internal/transcribe/`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      "X-NBHD-Internal-Key": internalKey,
      "X-NBHD-Tenant-Id": tenantId,
    },
    body: JSON.stringify({
      input_audio: {
        data: audio.toString("base64"),
        format: formatForPath(mediaPath),
      },
    }),
    signal: AbortSignal.timeout(55_000),
  });

  if (!response.ok) {
    throw new Error(`internal transcription failed with HTTP ${response.status}`);
  }

  let payload;
  try {
    payload = await response.json();
  } catch {
    throw new Error("internal transcription returned invalid JSON");
  }
  if (!payload || typeof payload.text !== "string") {
    throw new Error("internal transcription response is missing text");
  }
  return payload.text;
}

async function main() {
  try {
    const text = await transcribe(process.argv[2]);
    process.stdout.write(`${text}\n`);
  } catch (error) {
    process.stderr.write(`nbhd-transcribe: ${error.message}\n`);
    process.exitCode = 1;
  }
}

if (require.main === module) {
  void main();
}

module.exports = { formatForPath, transcribe };
