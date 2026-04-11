'use strict';

// Use OpenClaw's public plugin SDK API to register a handler for chmod EPERM.
//
// OpenClaw 2026.4.5's ensureTaskRegistryPermissions() calls chmodSync(0o700)
// on ~/.openclaw/tasks before every SQLite write. On Azure Container Apps,
// volumes are root-owned — non-root chmod returns EPERM. The underlying
// read/write operations work fine (0777 default).
//
// OpenClaw's own unhandled rejection handler (installUnhandledRejectionHandler)
// calls process.exit(1) for unclassified errors — which can't be intercepted
// by process.on('unhandledRejection'). The registerUnhandledRejectionHandler
// API is checked BEFORE the process.exit path, so returning true from a
// registered handler actually prevents the crash.
//
// API: registerUnhandledRejectionHandler(handler: (reason) => boolean)
//   - return true  → handled, OpenClaw skips entirely (no log, no exit)
//   - return false → not handled, OpenClaw's normal classification applies
//
// Stable import: openclaw/dist/plugin-sdk/runtime.js (no build hashes)
// TypeScript:    plugin-sdk/src/infra/unhandled-rejections.d.ts
//
// Remove when OpenClaw wraps chmodSync in try/catch upstream.

// OpenClaw is installed globally (npm install --global) so bare 'openclaw'
// isn't resolvable from /opt/nbhd/. Use require.resolve with the global path.
const sdkPath = require.resolve('openclaw/dist/plugin-sdk/runtime.js', {
  paths: ['/usr/local/lib/node_modules'],
});

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
