'use strict';

// OpenClaw 2026.4.5's ensureTaskRegistryPermissions() calls chmodSync(0o700)
// on ~/.openclaw/tasks before every SQLite write. On Azure Container Apps,
// both Azure File Share and EmptyDir volumes are root-owned — non-root
// chmod returns EPERM. The underlying read/write operations work fine (0777).
//
// Without this handler, the EPERM becomes an unhandled promise rejection that
// crashes the Node process. This handler catches ONLY that specific error and
// logs it as a warning. All other unhandled rejections still crash normally.
//
// Remove this handler when OpenClaw wraps the chmodSync in try/catch upstream.

process.on('unhandledRejection', (reason) => {
  if (
    reason &&
    reason.code === 'EPERM' &&
    reason.syscall === 'chmod'
  ) {
    const path = reason.path || '(unknown)';
    console.warn(
      `[nbhd] chmod EPERM suppressed (expected on Azure Container Apps): ${path}`
    );
    return; // Suppress — don't crash
  }

  // Everything else: re-throw as uncaughtException to preserve fail-fast behavior
  throw reason;
});
