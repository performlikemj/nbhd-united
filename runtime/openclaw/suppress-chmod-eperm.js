'use strict';

// OpenClaw 2026.4.5's ensureTaskRegistryPermissions() calls chmodSync(0o700)
// on ~/.openclaw/tasks before every SQLite write. On Azure Container Apps,
// both Azure File Share and EmptyDir volumes are root-owned — non-root
// chmod returns EPERM. The underlying read/write operations work fine (0777).
//
// The EPERM propagates as both an unhandled rejection AND (after OpenClaw's
// own handler re-throws it) an uncaught exception. We must catch BOTH to
// prevent the crash. Only the specific chmod EPERM is suppressed — all other
// errors still crash the process normally.
//
// Remove this handler when OpenClaw wraps the chmodSync in try/catch upstream.

function isChmodEperm(reason) {
  return reason && reason.code === 'EPERM' && reason.syscall === 'chmod';
}

function logSuppressed(reason, eventType) {
  const path = reason.path || '(unknown)';
  console.warn(
    `[nbhd] chmod EPERM suppressed via ${eventType} (expected on Azure Container Apps): ${path}`
  );
}

process.on('unhandledRejection', (reason) => {
  if (isChmodEperm(reason)) {
    logSuppressed(reason, 'unhandledRejection');
    return;
  }
  throw reason;
});

process.on('uncaughtException', (err) => {
  if (isChmodEperm(err)) {
    logSuppressed(err, 'uncaughtException');
    return;
  }
  // For non-chmod errors: pass through to OpenClaw's own handler.
  // We do NOT call process.exit here — OpenClaw handles certain uncaught
  // exceptions (like CIAO announcements) gracefully and calling exit would
  // kill those too. OpenClaw's handler will decide per-error whether to crash.
});
