#!/usr/bin/env node

import { readFileSync } from "node:fs";

import { assertHookOnlyPluginsActivated } from "./hook-activation-contract.mjs";

const logPath = process.argv[2];
if (!logPath) {
  console.error("Usage: node runtime/openclaw/plugins/assert-hook-activation.mjs <gateway-log>");
  process.exit(2);
}

try {
  const result = assertHookOnlyPluginsActivated(readFileSync(logPath, "utf8"));
  console.log(result.line);
  console.log(`Hook activation assertion passed (${result.expected.length} repo-derived hook plugins).`);
  console.log(`Dropped/unknown typed-hook diagnostics: ${result.diagnostics.length}`);
} catch (error) {
  console.error(`Hook activation assertion FAILED: ${error.message}`);
  process.exit(1);
}
