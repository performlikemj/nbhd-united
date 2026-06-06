// Behavioral tests for runtime/openclaw/suppress-chmod-eperm.js Layers 1+2.
//
// The suppression file captures fs.chmodSync, fs.promises.chmod, AND
// fs.promises.open at load time. To test the patched behavior against
// synthetic errors, we install a mock for each underlying syscall BEFORE
// require()'ing the suppression module — the patched wrapper then closes
// over our mock and exercises its catch/log/return path on a configurable
// error.
//
// Layer 3 (registerUnhandledRejectionHandler via OpenClaw plugin SDK) is
// not exercised here — it dynamic-imports an OpenClaw runtime that only
// resolves inside the production container. The Layer 1+2 fs-layer
// monkey-patches are the primary defense; Layer 3 is a safety net for
// future async variants.

import { test, before, beforeEach } from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import { createRequire } from 'node:module';

const require = createRequire(import.meta.url);

let pendingError = null;
let pendingHandleChmodError = null;

function makeChmodErr(code, syscall = 'chmod') {
  return Object.assign(new Error(`${code}: ${syscall}`), {
    code,
    syscall,
    path: '/tmp/nbhd-test-chmod',
  });
}

// Mock FileHandle class — gives Layer 2 a real prototype to patch.
// Layer 2 monkey-patches FileHandle.prototype.chmod after the first
// fs.promises.open() call returns one of these instances.
class MockFileHandle {
  async chmod(mode) {
    if (pendingHandleChmodError) throw pendingHandleChmodError;
  }
  async appendFile() {}
  async close() {}
}

before(() => {
  fs.chmodSync = function mockChmodSync() {
    if (pendingError) throw pendingError;
  };
  fs.promises.chmod = async function mockPromisesChmod() {
    if (pendingError) throw pendingError;
  };
  fs.promises.open = async function mockPromisesOpen() {
    return new MockFileHandle();
  };
  require('./suppress-chmod-eperm.js');
});

beforeEach(() => {
  pendingError = null;
  pendingHandleChmodError = null;
});

test('sync: EPERM suppressed', () => {
  pendingError = makeChmodErr('EPERM');
  assert.doesNotThrow(() => fs.chmodSync('/path', 0o700));
});

test('sync: EACCES suppressed', () => {
  pendingError = makeChmodErr('EACCES');
  assert.doesNotThrow(() => fs.chmodSync('/path', 0o700));
});

test('sync: ENOTSUP suppressed', () => {
  pendingError = makeChmodErr('ENOTSUP');
  assert.doesNotThrow(() => fs.chmodSync('/path', 0o700));
});

test('sync: EBUSY re-throws (not in suppression set)', () => {
  pendingError = makeChmodErr('EBUSY');
  assert.throws(() => fs.chmodSync('/path', 0o700), { code: 'EBUSY' });
});

test('sync: EPERM from non-chmod syscall re-throws', () => {
  pendingError = makeChmodErr('EPERM', 'open');
  assert.throws(() => fs.chmodSync('/path', 0o700), { syscall: 'open' });
});

test('sync: success returns undefined', () => {
  pendingError = null;
  assert.equal(fs.chmodSync('/path', 0o700), undefined);
});

test('async-promise: EPERM suppressed', async () => {
  pendingError = makeChmodErr('EPERM');
  await assert.doesNotReject(fs.promises.chmod('/path', 0o700));
});

test('async-promise: EACCES suppressed', async () => {
  pendingError = makeChmodErr('EACCES');
  await assert.doesNotReject(fs.promises.chmod('/path', 0o700));
});

test('async-promise: EBUSY re-throws', async () => {
  pendingError = makeChmodErr('EBUSY');
  await assert.rejects(fs.promises.chmod('/path', 0o700), { code: 'EBUSY' });
});

test('idempotency: marker set, re-require is a no-op', () => {
  assert.equal(fs.chmodSync.__nbhdPatched, true);
  assert.equal(fs.promises.chmod.__nbhdPatched, true);
  assert.equal(fs.promises.open.__nbhdPatched, true);
  const syncBefore = fs.chmodSync;
  const asyncBefore = fs.promises.chmod;
  const openBefore = fs.promises.open;
  const modulePath = require.resolve('./suppress-chmod-eperm.js');
  delete require.cache[modulePath];
  require(modulePath);
  assert.equal(fs.chmodSync, syncBefore, 'sync patch should not double-wrap');
  assert.equal(fs.promises.chmod, asyncBefore, 'async patch should not double-wrap');
  assert.equal(fs.promises.open, openBefore, 'open wrapper should not double-wrap');
});

// ─── Layer 2 — FileHandle.chmod suppression ───────────────────────────

test('file-handle: open() patches FileHandle.prototype.chmod on first call', async () => {
  const handle = await fs.promises.open('/path', 'a');
  // After the first open, MockFileHandle.prototype.chmod is now wrapped.
  assert.equal(handle.chmod.__nbhdPatched, true);
  await handle.close();
});

test('file-handle: EPERM on handle.chmod is suppressed', async () => {
  const handle = await fs.promises.open('/path', 'a');
  pendingHandleChmodError = makeChmodErr('EPERM');
  await assert.doesNotReject(handle.chmod(0o600));
  await handle.close();
});

test('file-handle: EACCES on handle.chmod is suppressed', async () => {
  const handle = await fs.promises.open('/path', 'a');
  pendingHandleChmodError = makeChmodErr('EACCES');
  await assert.doesNotReject(handle.chmod(0o600));
  await handle.close();
});

test('file-handle: ENOTSUP on handle.chmod is suppressed', async () => {
  const handle = await fs.promises.open('/path', 'a');
  pendingHandleChmodError = makeChmodErr('ENOTSUP');
  await assert.doesNotReject(handle.chmod(0o600));
  await handle.close();
});

test('file-handle: EBUSY re-throws (not in suppression set)', async () => {
  const handle = await fs.promises.open('/path', 'a');
  pendingHandleChmodError = makeChmodErr('EBUSY');
  await assert.rejects(handle.chmod(0o600), { code: 'EBUSY' });
  await handle.close();
});

test('file-handle: EPERM from non-chmod syscall re-throws', async () => {
  const handle = await fs.promises.open('/path', 'a');
  pendingHandleChmodError = makeChmodErr('EPERM', 'open');
  await assert.rejects(handle.chmod(0o600), { syscall: 'open' });
  await handle.close();
});

test('file-handle: subsequent appendFile still runs when chmod throws inside try/finally', async () => {
  // This is the load-bearing assertion. OC 5.28's appendRegularFile does:
  //   await handle.chmod(mode);          // ← Layer 2 must suppress EPERM
  //   await handle.appendFile(content);  // ← this MUST still run, otherwise file stays 0 bytes
  const handle = await fs.promises.open('/path', 'a');
  pendingHandleChmodError = makeChmodErr('EPERM');
  let appendCalled = false;
  handle.appendFile = async function () {
    appendCalled = true;
  };
  // Mimic appendRegularFile's flow.
  try {
    await handle.chmod(0o600);
    await handle.appendFile('line\n');
  } finally {
    await handle.close();
  }
  assert.equal(appendCalled, true, 'appendFile must run after suppressed chmod');
});
