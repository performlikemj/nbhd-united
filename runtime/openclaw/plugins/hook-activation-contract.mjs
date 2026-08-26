import { existsSync, readFileSync, readdirSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const DEFAULT_PLUGIN_ROOT = fileURLToPath(new URL("./", import.meta.url));
const ANSI_ESCAPE = /\u001b\[[0-?]*[ -/]*[@-~]/gu;

function manifestPaths(root) {
  const paths = [];
  for (const entry of readdirSync(root, { withFileTypes: true })) {
    const entryPath = path.join(root, entry.name);
    if (entry.isDirectory()) {
      paths.push(...manifestPaths(entryPath));
    } else if (entry.name === "openclaw.plugin.json") {
      paths.push(entryPath);
    }
  }
  return paths;
}

function manifestOwnsHooks(manifest) {
  // Mirrors openclaw@2026.5.28's activation planner `hasValues(plugin.hooks)`.
  return (manifest.hooks?.length ?? 0) > 0;
}

export function discoverHookOnlyPlugins(pluginRoot = DEFAULT_PLUGIN_ROOT) {
  return manifestPaths(pluginRoot).flatMap((manifestPath) => {
    const pluginDirectory = path.dirname(manifestPath);
    const indexPath = path.join(pluginDirectory, "index.js");
    const manifest = JSON.parse(readFileSync(manifestPath, "utf8"));
    const source = existsSync(indexPath) ? readFileSync(indexPath, "utf8") : "";
    const registersTypedHooks = /\bapi\.on\s*\(/u.test(source);
    const ownsManifestHooks = manifestOwnsHooks(manifest);
    const registersTools = /\b(?:api\.)?registerTool\s*\(/u.test(source);
    if ((!registersTypedHooks && !ownsManifestHooks) || registersTools) return [];

    return [{
      id: manifest.id,
      indexPath,
      manifest,
      manifestPath,
      ownsManifestHooks,
      registersTypedHooks,
    }];
  }).sort((left, right) => left.id.localeCompare(right.id));
}

export function assertHookOnlyManifestsDeclareActivation(pluginRoot = DEFAULT_PLUGIN_ROOT) {
  const plugins = discoverHookOnlyPlugins(pluginRoot);
  const missing = plugins.filter(
    ({ manifest, ownsManifestHooks }) => (
      !ownsManifestHooks && !manifest.activation?.onCapabilities?.includes("hook")
    ),
  );
  if (missing.length > 0) {
    throw new Error(
      `Hook-only plugins missing activation.onCapabilities=["hook"]: ${missing.map(({ id }) => id).join(", ")}`,
    );
  }
  return plugins;
}

export function assertHookPluginDiagnosticsClean(logText, expectedPlugins) {
  const expectedIds = new Set(expectedPlugins.map(({ id }) => id));
  const diagnosticLines = logText.replace(ANSI_ESCAPE, "").split(/\r?\n/u).filter((line) => (
    [...expectedIds].some((id) => line.includes(id))
      && (
        line.includes("blocked because non-bundled plugins must set")
        || line.includes("unknown typed hook")
      )
  ));
  if (diagnosticLines.length > 0) {
    throw new Error(`Hook registrations were dropped by OpenClaw:\n${diagnosticLines.join("\n")}`);
  }
  return diagnosticLines;
}

export function activatedPluginsFromLog(logText) {
  const cleanLog = logText.replace(ANSI_ESCAPE, "");
  const matches = [...cleanLog.matchAll(/^.*http server listening \((\d+) plugins?: ([^;\n)]+)(?:;[^\n)]*)?\).*$/gmu)];
  if (matches.length === 0) {
    throw new Error("OpenClaw log has no 'http server listening (N plugins: ...)' line");
  }

  const match = matches.at(-1);
  const plugins = match[2].split(",").map((value) => value.trim()).filter(Boolean);
  const declaredCount = Number.parseInt(match[1], 10);
  if (plugins.length !== declaredCount) {
    throw new Error(`Activated-plugin line declares ${declaredCount} plugins but names ${plugins.length}: ${match[0]}`);
  }
  return { line: match[0], plugins };
}

export function assertHookOnlyPluginsActivated(logText, pluginRoot = DEFAULT_PLUGIN_ROOT) {
  const expected = assertHookOnlyManifestsDeclareActivation(pluginRoot);
  const activated = activatedPluginsFromLog(logText);
  const activatedIds = new Set(activated.plugins);
  const missing = expected.filter(({ id }) => !activatedIds.has(id));
  if (missing.length > 0) {
    throw new Error(
      `Hook plugins absent from activated-plugin line: ${missing.map(({ id }) => id).join(", ")}\n${activated.line}`,
    );
  }
  const diagnostics = assertHookPluginDiagnosticsClean(logText, expected);
  return { ...activated, diagnostics, expected: expected.map(({ id }) => id) };
}
