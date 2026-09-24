// Run against the real pinned npm package, never a reimplementation of trimming.
// OPENCLAW_PACKAGE_ROOT=/path/to/openclaw node --test .../user-bootstrap-cap.test.mjs
import assert from "node:assert/strict";
import { mkdtempSync, mkdirSync, readFileSync, writeFileSync, rmSync } from "node:fs";
import os from "node:os";
import path from "node:path";
import { after, test } from "node:test";
import { pathToFileURL } from "node:url";
import vm from "node:vm";
import { ANALYZER, BOOTSTRAP, WORKER, patchUserBootstrapCap } from "./user-bootstrap-cap.mjs";
import { checkBootstrapHelper } from "./user-bootstrap-cap.smoke.mjs";

const source = process.env.OPENCLAW_PACKAGE_ROOT || "/tmp/openclaw-src-9.4/package";
const root = mkdtempSync(path.join(os.tmpdir(), "nbhd-bootstrap-test-"));
after(() => rmSync(root, { recursive: true, force: true }));
const originals = Object.fromEntries(["package.json", BOOTSTRAP, WORKER, ANALYZER].map(file =>
  [file, readFileSync(path.join(source, file), "utf8")]));

function fixture(name) {
  const dir = path.join(root, name);
  for (const [file, content] of Object.entries(originals)) {
    mkdirSync(path.dirname(path.join(dir, file)), { recursive: true });
    writeFileSync(path.join(dir, file), content);
  }
  return dir;
}

// The unpacked npm tarball has no dependencies. Evaluate the actual bootstrap
// region, supplying upstream's real UTF-16/string helpers. No trimming stubs.
const strings = await import(pathToFileURL(path.join(source, "dist/string-coerce-CIXf7egm.mjs")));
const utf16 = await import(pathToFileURL(path.join(source, "dist/utf16-slice-D_ngcYKd.mjs")));
function helper(text, worker = false) {
  const context = vm.createContext({
    normalizeOptionalString: strings.l, sliceUtf16Safe: utf16.n, truncateUtf16Safe: utf16.r,
    __esmMin: init => init,
    init_string_coerce() {}, init_google_turn_ordering() {}, init_utils$13() {}, init_agent_scope() {},
  });
  let region;
  if (worker) {
    const start = text.indexOf("function resolveBootstrapMaxChars(");
    const init = text.indexOf("var DEFAULT_BOOTSTRAP_MAX_CHARS", start);
    const end = text.indexOf("}));", init) + 4;
    assert.ok(start > 0 && init > start && end > init);
    region = text.slice(start, end) + ";init_bootstrap$2();";
  } else {
    region = text.slice(text.indexOf("//#region src/agents/embedded-agent-helpers/bootstrap.ts"), text.lastIndexOf("export {"));
  }
  return vm.runInContext(region + "\nbuildBootstrapContextFiles", context);
}

test("real 9.4 bundles: full USER.md, unchanged non-USER files, budgets, idempotence", () => {
  const dir = fixture("success");
  assert.equal(patchUserBootstrapCap(dir).length, 3);
  const patched = Object.fromEntries([BOOTSTRAP, WORKER, ANALYZER].map(file => [file, readFileSync(path.join(dir, file), "utf8")]));
  assert.deepEqual(patchUserBootstrapCap(dir), []);
  for (const file of [BOOTSTRAP, WORKER]) {
    const original = helper(originals[file], file === WORKER);
    const updated = helper(patched[file], file === WORKER);
    checkBootstrapHelper(updated);
    const warnings = [];
    const input = [{ name: "USER.md", path: "/USER.md", content: "x".repeat(9500) }];
    assert.ok(original(input, { maxChars: 26000, totalMaxChars: 80000, warn: m => warnings.push(m) })[0].content.length <= 4000);
    assert.equal(warnings.length, 1);
    for (const name of ["SOUL.md", "AGENTS.md"]) {
      for (const length of [9500, 30000]) {
        const input = [{ name, path: `/${name}`, content: "x".repeat(length) }];
        const opts = { maxChars: 26000, totalMaxChars: 80000 };
        assert.equal(updated(input, opts)[0].content, original(input, opts)[0].content);
      }
    }
    assert.equal(readFileSync(path.join(dir, file), "utf8"), patched[file]);
  }
  // Run actual analyzer predicates: no stale 4K warning/advice after the raise.
  const context = vm.createContext({ USER_BOOTSTRAP_MAX_CHARS: 26000, normalizeOptionalString: strings.l, path });
  const analyzer = patched[ANALYZER];
  vm.runInContext(analyzer.slice(analyzer.indexOf("//#region"), analyzer.lastIndexOf("export {")), context);
  const format = vm.runInContext("formatBootstrapTruncationWarningLines", context);
  const analysis = { hasTruncation: true, truncatedFiles: [{ name: "USER.md", path: "/USER.md", rawChars: 30000, injectedChars: 25999, effectiveFileLimit: 26000, causes: ["per-file-limit"] }] };
  const lines = format({ analysis });
  assert.ok(lines.some(line => line.includes("26000-character bootstrap cap")));
  assert.ok(!lines.some(line => line.includes("If unintentional")));
  analysis.truncatedFiles[0].effectiveFileLimit = 10000;
  assert.ok(format({ analysis }).some(line => line.includes("If unintentional")));
});

for (const [name, file, change] of [
  ["version", "package.json", text => text.replace('"2026.9.4"', '"2026.9.5"')],
  ["export alias", BOOTSTRAP, text => text.replace("buildBootstrapContextFiles as n", "buildBootstrapContextFiles as z")],
  ["worker clamp alias", WORKER, text => text.replace("isUserBootstrapFile(Pn.name)", "isUserBootstrapFile(changed.name)")],
  ["diagnostic predicate", ANALYZER, text => text.replace("effectiveFileLimit === 4e3", "effectiveFileLimit === 5000")],
  ["duplicate constant", BOOTSTRAP, text => text + "\nconst USER_BOOTSTRAP_MAX_CHARS = 4e3;"],
]) {
  test(`fails before any write on changed ${name}`, () => {
    const dir = fixture(name);
    writeFileSync(path.join(dir, file), change(originals[file]));
    const before = [BOOTSTRAP, WORKER, ANALYZER].map(file => readFileSync(path.join(dir, file), "utf8"));
    assert.throws(() => patchUserBootstrapCap(dir));
    assert.deepEqual([BOOTSTRAP, WORKER, ANALYZER].map(file => readFileSync(path.join(dir, file), "utf8")), before);
  });
}

test("refuses an unexpected bundled copy", () => {
  const dir = fixture("extra-copy");
  writeFileSync(path.join(dir, "dist/extra.mjs"), originals[BOOTSTRAP]);
  assert.throws(() => patchUserBootstrapCap(dir), /bundle inventory changed/);
});
