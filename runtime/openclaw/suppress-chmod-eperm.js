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
// Three suppression layers, because the failure mode shape varies by version:
//
//   1. fs.chmodSync / fs.promises.chmod monkey-patch (LAYER 1 below) —
//      handles 2026.5.7+ where ensureTaskRegistryPermissions calls
//      chmodSync directly. The throw is synchronous and propagates through
//      the call stack; swallowing it here is the only way to keep cron
//      task creation working. Must install BEFORE OpenClaw imports, hence
//      at top of file.
//
//   2. FileHandle.prototype.chmod (fchmod) monkey-patch (LAYER 2 below) —
//      handles 2026.5.28+ where appendRegularFile() opens a file and then
//      calls `await handle.chmod(mode)` BEFORE the appendFile write
//      (dist/regular-file-BD2zl6_l.js:184). Because the throw skips the
//      subsequent appendFile, every cron run-log diary line is silently
//      dropped on root-owned mounts — file gets created (open touches it)
//      but stays 0 bytes. The fchmod path uses FileHandle.prototype.chmod,
//      which Layer 1's fs.chmod patch does NOT cover.
//
//   3. registerUnhandledRejectionHandler (LAYER 3 below) — legacy handler
//      from the 2026.4.5 era when chmod was thrown asynchronously via
//      Promise rejection. Kept as a safety net for any future code path
//      that uses an async chmod variant.
//
// The signals that this file ISN'T working (next-time-debugging shortcut):
//   Layer 1 hard failure:
//   - "[tasks/registry] Failed to restore task registry" appears on every
//     container boot (look in the OpenClaw container logs).
//   - Zero `nbhd_send_to_user` log lines from the container — crons never fire.
//   - Container wakes briefly (4-min cycles) but does no user-facing work.
//   Layer 2 soft failure (silent observability gap):
//   - All files under `cron/runs/<jobId>.jsonl` on the file share are size=0.
//     File mtimes update on each cron fire, content stays empty.
//   - `jobs-state.json` per-job `state` still updates (lastRunAtMs etc.) —
//     only the per-fire run-log history is lost.
// If you see either combo, suppression isn't catching the latest OpenClaw call site.
//
// When upgrading OpenClaw:
//   - Pull the npm tarball: `npm pack openclaw@<version>` then
//     `grep -nE "chmodSync|fs.chmod|handle.chmod|chmodPromise" dist/*.js`
//   - Verify both the task-registry source still calls fs.chmodSync (Layer 1)
//     AND that regular-file's appendRegularFile still uses handle.chmod (Layer 2).
//   - If a NEW syscall variant appears (fchmod / lchmod / etc.), extend the
//     appropriate layer to cover it.
//
// References:
//   - 2026.5.7:  dist/task-registry-DxA2A4eM.js:1140-1148 (sync chmodSync)
//   - 2026.5.28: dist/regular-file-BD2zl6_l.js:184 (FileHandle.chmod)
//   - 2026.4.5:  same task-registry function, async path (promise rejection)
//   - Memory: project_openclaw_chmod_eperm_saga.md

// ────────────────────────────────────────────────────────────────────────
// LAYER 1 — sync chmod monkey-patch (covers 2026.5.7+ task registry init)
// ────────────────────────────────────────────────────────────────────────
//
// Patches BOTH fs.chmodSync (the 2026.5.7+ task-registry failing call) and
// fs.promises.chmod (the path-based async variant). The FileHandle.chmod
// variant (which 2026.5.28's appendRegularFile uses) is patched separately
// in Layer 2 below — Node's FileHandle doesn't surface on the fs object.
// Skipped: fs.chmod (callback form, not in use), fs.fchmodSync /
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
// LAYER 2 — FileHandle.prototype.chmod (fchmod) monkey-patch
// ────────────────────────────────────────────────────────────────────────
//
// Covers 2026.5.28+ where appendRegularFile opens a file and then calls
// `await handle.chmod(mode)` on the FileHandle before the appendFile
// write. The throw on EPERM jumps to the `finally { await handle.close(); }`
// — the file is created by O_CREAT but nothing is written. Observable on
// canary as `cron/runs/<jobId>.jsonl` files with mtime updates but 0 bytes.
//
// Node doesn't export FileHandle directly. We wrap fs.promises.open and
// patch the prototype on the FIRST handle returned; the wrapper is then
// a thin pass-through (prototype is now patched, all future handles get
// the suppressed chmod for free).
//
// We don't patch `handle.fchmod` separately because Node's FileHandle
// only exposes `chmod` (which internally is fchmod via the file descriptor).

if (fs.promises && fs.promises.open && !fs.promises.open.__nbhdPatched) {
  const origPromisesOpen = fs.promises.open;
  fs.promises.open = async function nbhdPatchedPromisesOpen(...args) {
    const handle = await origPromisesOpen.apply(fs.promises, args);
    if (handle && typeof handle.chmod === 'function') {
      const proto = Object.getPrototypeOf(handle);
      if (proto && typeof proto.chmod === 'function' && !proto.chmod.__nbhdPatched) {
        const origHandleChmod = proto.chmod;
        proto.chmod = async function nbhdPatchedHandleChmod(mode) {
          try {
            return await origHandleChmod.call(this, mode);
          } catch (err) {
            if (isSuppressibleChmodErr(err)) {
              // FileHandle doesn't expose its path post-open; use the
              // open-time path from the first arg if reachable. Fall back
              // to a generic label so the log entry still uniques.
              logChmodSuppression(String(args[0] || '<file-handle>'), err.code, 'file-handle');
              return;
            }
            throw err;
          }
        };
        proto.chmod.__nbhdPatched = true;
      }
    }
    return handle;
  };
  fs.promises.open.__nbhdPatched = true;
}

// ────────────────────────────────────────────────────────────────────────
// LAYER 3 — legacy unhandledRejection handler via OpenClaw plugin SDK
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
