// Behavioral tests for runtime/openclaw/suppress-chmod-eperm.js Layer 1.
//
// The suppression file captures fs.chmodSync and fs.promises.chmod at
// load time. To test the patched behavior against synthetic errors, we
// install a mock for each underlying syscall BEFORE require()'ing the
// suppression module — the patched wrapper then closes over our mock
// and exercises its catch/log/return path on a configurable error.
//
// Layer 2 (registerUnhandledRejectionHandler via OpenClaw plugin SDK) is
// not exercised here — it dynamic-imports an OpenClaw runtime that only
// resolves inside the production container. The Layer 1 monkey-patch is
// the primary defense; Layer 2 is a safety net for future async variants.

import { test, before, beforeEach } from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import { createRequire } from 'node:module';

const require = createRequire(import.meta.url);

let pendingError = null;

function makeChmodErr(code, syscall = 'chmod') {
  return Object.assign(new Error(`${code}: ${syscall}`), {
    code,
    syscall,
    path: '/tmp/nbhd-test-chmod',
  });
}

before(() => {
  fs.chmodSync = function mockChmodSync() {
    if (pendingError) throw pendingError;
  };
  fs.promises.chmod = async function mockPromisesChmod() {
    if (pendingError) throw pendingError;
  };
  require('./suppress-chmod-eperm.js');
});

beforeEach(() => {
  pendingError = null;
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
  const syncBefore = fs.chmodSync;
  const asyncBefore = fs.promises.chmod;
  const modulePath = require.resolve('./suppress-chmod-eperm.js');
  delete require.cache[modulePath];
  require(modulePath);
  assert.equal(fs.chmodSync, syncBefore, 'sync patch should not double-wrap');
  assert.equal(fs.promises.chmod, asyncBefore, 'async patch should not double-wrap');
});
