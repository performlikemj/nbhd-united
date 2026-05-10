'use strict';

// nbhd's chmod EPERM/EACCES suppression for OpenClaw on Azure Container Apps.
//
// Why this file exists:
// OpenClaw insists on chmodding its data directories to 0o700 ("owner only")
// at gateway startup AND on every task-registry write. On Azure Container
// Apps, mounted volumes (Azure File Share AND EmptyDir) are root-owned,
// while OpenClaw runs as the `node` user — so chmod returns EPERM. The
// underlying read/write operations work fine because the default mount mode
// (0o755) already permits node-user access; the chmod is just paranoid and
// doesn't matter for security in this environment.
//
// Two suppression layers, because the failure mode shape varies by version:
//
//   1. fs.chmodSync / fs.promises.chmod monkey-patch (LAYER 1 below) —
//      handles 2026.5.7+ where ensureTaskRegistryPermissions calls
//      chmodSync directly. The throw is synchronous and propagates through
//      the call stack; swallowing it here is the only way to keep cron
//      task creation working. Must install BEFORE OpenClaw imports, hence
//      at top of file.
//
//   2. registerUnhandledRejectionHandler (LAYER 2 below) — legacy handler
//      from the 2026.4.5 era when chmod was thrown asynchronously via
//      Promise rejection. Kept as a safety net for any future code path
//      that uses an async chmod variant.
//
// The signal that this file ISN'T working (next-time-debugging shortcut):
//   - "[tasks/registry] Failed to restore task registry" appears on every
//     container boot (look in the OpenClaw container logs).
//   - Zero `nbhd_send_to_user` log lines from the container — crons never fire.
//   - Container wakes briefly (4-min cycles) but does no user-facing work.
// If you see this combo, suppression isn't catching the latest OpenClaw call site.
//
// When upgrading OpenClaw:
//   - Pull the npm tarball: `npm pack openclaw@<version>` then
//     `grep -nE "chmodSync|fs.chmod" dist/task-registry-*.js`
//   - Verify the new task-registry source still calls fs.chmodSync (not some
//     other syscall like fchmod / lchmod that we'd need to patch separately).
//   - If the call site moved, extend Layer 1 to cover the new variant.
//
// References:
//   - 2026.5.7: dist/task-registry-DxA2A4eM.js:1140-1148 (sync chmodSync)
//   - 2026.4.5: same function, async path (promise rejection)
//   - Memory: project_openclaw_chmod_eperm_saga.md

// ────────────────────────────────────────────────────────────────────────
// LAYER 1 — sync chmod monkey-patch (covers 2026.5.7+ task registry init)
// ────────────────────────────────────────────────────────────────────────
//
// Patches BOTH fs.chmodSync (currently the failing call) and
// fs.promises.chmod (defensive — likely next call site if OpenClaw refactors
// async). Skipped: fs.chmod (callback form, not in use), fs.fchmodSync /
// fs.lchmodSync (file-descriptor / symlink variants, edge cases).
//
// Idempotent — `--require` runs this for every node command, including the
// entrypoint's `node -e "JSON.parse(...)"` config validation.

const fs = require('fs');
const seenChmodPaths = new Set();

function isSuppressibleChmodErr(err) {
  if (!err) return false;
  if (err.syscall && err.syscall !== 'chmod') return false;
  return err.code === 'EPERM' || err.code === 'EACCES' || err.code === 'ENOTSUP';
}

function logChmodSuppression(targetPath, code, kind) {
  const key = `${kind}:${targetPath}`;
  if (seenChmodPaths.has(key)) return;
  seenChmodPaths.add(key);
  console.warn(
    `[nbhd] chmod ${code} suppressed for ${targetPath} (${kind}; root-owned mount, ` +
      `default mode permits node user)`
  );
}

if (!fs.chmodSync.__nbhdPatched) {
  const origChmodSync = fs.chmodSync;
  fs.chmodSync = function nbhdPatchedChmodSync(p, mode) {
    try {
      return origChmodSync.call(fs, p, mode);
    } catch (err) {
      if (isSuppressibleChmodErr(err)) {
        logChmodSuppression(String(p), err.code, 'sync');
        return;
      }
      throw err;
    }
  };
  fs.chmodSync.__nbhdPatched = true;
}

if (fs.promises && fs.promises.chmod && !fs.promises.chmod.__nbhdPatched) {
  const origPromisesChmod = fs.promises.chmod;
  fs.promises.chmod = async function nbhdPatchedPromisesChmod(p, mode) {
    try {
      return await origPromisesChmod.call(fs.promises, p, mode);
    } catch (err) {
      if (isSuppressibleChmodErr(err)) {
        logChmodSuppression(String(p), err.code, 'async-promise');
        return;
      }
      throw err;
    }
  };
  fs.promises.chmod.__nbhdPatched = true;
}

// ────────────────────────────────────────────────────────────────────────
// LAYER 2 — legacy unhandledRejection handler via OpenClaw plugin SDK
// ────────────────────────────────────────────────────────────────────────
//
// Installed below for safety-net coverage. Layer 1 above handles the
// currently-known sync call site. Layer 2 catches any chmod EPERM that
// reaches OpenClaw's process.exit-on-unclassified-error path via Promise
// rejection — relevant for future async chmod variants that haven't been
// patched at the fs layer.
//
// API: registerUnhandledRejectionHandler(handler: (reason) => boolean)
//   - return true  → handled, OpenClaw skips (no log, no exit)
//   - return false → not handled, OpenClaw's normal classification applies
//
// Stable import: openclaw/dist/plugin-sdk/runtime.js (no build hashes)
// TypeScript:    plugin-sdk/src/infra/unhandled-rejections.d.ts

// OpenClaw is installed globally (npm install --global) so bare 'openclaw'
// isn't resolvable from /opt/nbhd/. Use require.resolve with the global path.
// Wrapped in try/catch because this file loads via --require for EVERY node
// command — including the entrypoint's `node -e "JSON.parse(...)"` config
// validation. An uncaught throw here would crash that validation and prevent
// the container from starting.
let sdkPath;
try {
  sdkPath = require.resolve('openclaw/dist/plugin-sdk/runtime.js', {
    paths: ['/usr/local/lib/node_modules'],
  });
} catch (err) {
  // Not found — probably running outside the OpenClaw container (e.g., tests,
  // entrypoint validation). Silently skip.
}

if (!sdkPath) return;

import(sdkPath).then(({ registerUnhandledRejectionHandler }) => {
  registerUnhandledRejectionHandler((reason) => {
    if (reason && reason.code === 'EPERM' && reason.syscall === 'chmod') {
      const p = reason.path || '(unknown)';
      console.warn(`[nbhd] chmod EPERM suppressed (OpenClaw API): ${p}`);
      return true;
    }
    return false;
  });
  console.log('[nbhd] chmod EPERM handler registered via OpenClaw plugin SDK');
}).catch(err => {
  console.warn('[nbhd] Failed to register chmod handler:', err.message);
});
