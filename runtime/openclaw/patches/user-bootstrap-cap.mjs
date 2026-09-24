// Pinned dist patch: re-audit every anchor when upgrading OpenClaw.
import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { globSync, readFileSync, writeFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

export const BOOTSTRAP = "dist/bootstrap-DYYMCrXY.mjs";
export const WORKER = "dist/worker/worker.mjs";
export const ANALYZER = "dist/bootstrap-budget-CKkQABlr.mjs";

function exactlyOnce(source, anchor, file) {
  assert.equal(source.split(anchor).length - 1, 1, `${file}: anchor moved or duplicated: ${anchor}`);
}

function replaceOnce(source, before, after, file) {
  const oldCount = source.split(before).length - 1;
  const newCount = source.split(after).length - 1;
  assert.ok((oldCount === 1 && newCount === 0) || (oldCount === 0 && newCount === 1),
    `${file}: expected exactly one original or patched anchor: ${before}`);
  return source.replace(before, after);
}

export function patchUserBootstrapCap(packageRoot) {
  const pkg = JSON.parse(readFileSync(path.join(packageRoot, "package.json"), "utf8"));
  assert.equal(pkg.name, "openclaw");
  assert.equal(pkg.version, "2026.9.4", "Re-audit USER.md patch for this OpenClaw version");
  const files = [BOOTSTRAP, WORKER, ANALYZER];
  // Detect newly bundled copies, including the worker's binary-looking JS.
  const copies = globSync("dist/**/*.{mjs,cjs,js}", { cwd: packageRoot })
    .filter(file => readFileSync(path.join(packageRoot, file), "utf8").includes("USER_BOOTSTRAP_MAX_CHARS"));
  assert.deepEqual(copies.sort(), [...files].sort(), "USER.md cap bundle inventory changed");
  const plans = files.map(file => {
    const original = readFileSync(path.join(packageRoot, file), "utf8");
    let patched = original;
    if (file === BOOTSTRAP) {
      exactlyOnce(original, "buildBootstrapContextFiles as n", file);
      exactlyOnce(original, "USER_BOOTSTRAP_MAX_CHARS as t", file);
      exactlyOnce(original, "const fileBudget = isUserBootstrapFile(file.name) ? Math.min(maxChars, USER_BOOTSTRAP_MAX_CHARS) : maxChars;", file);
      patched = replaceOnce(original, "const USER_BOOTSTRAP_MAX_CHARS = 4e3;", "const USER_BOOTSTRAP_MAX_CHARS = 26e3;", file);
    } else if (file === WORKER) {
      exactlyOnce(original, "let Fn=isUserBootstrapFile(Pn.name)?Math.min(_n,USER_BOOTSTRAP_MAX_CHARS):_n,Ln=Math.max(1,Math.min(Fn,Dn))", file);
      patched = replaceOnce(original, "USER_BOOTSTRAP_MAX_CHARS=4e3,", "USER_BOOTSTRAP_MAX_CHARS=26e3,", file);
    } else {
      exactlyOnce(original, 'import { i as resolveBootstrapTotalMaxChars, r as resolveBootstrapMaxChars, t as USER_BOOTSTRAP_MAX_CHARS } from "./bootstrap-DYYMCrXY.mjs";', file);
      exactlyOnce(original, 'return name.toLowerCase() === "user.md" ? Math.min(bootstrapMaxChars, USER_BOOTSTRAP_MAX_CHARS) : bootstrapMaxChars;', file);
      for (const op of ["===", "<"]) {
        patched = replaceOnce(patched, `file.effectiveFileLimit ${op} 4e3`, `file.effectiveFileLimit ${op} USER_BOOTSTRAP_MAX_CHARS`, file);
      }
    }
    execFileSync(process.execPath, ["--input-type=module", "--check"], { input: patched, stdio: ["pipe", "pipe", "pipe"] });
    return { file, original, patched };
  });
  // Validate ALL plans before touching any bundle. Re-running is a byte-for-byte no-op.
  const changed = [];
  for (const { file, original, patched } of plans) {
    const target = path.join(packageRoot, file);
    if (original !== patched) {
      writeFileSync(target, patched);
      changed.push(file);
    }
    execFileSync(process.execPath, ["--check", target], { stdio: "pipe" });
  }
  return changed;
}

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  assert.ok(process.argv[2], "Usage: node user-bootstrap-cap.mjs <openclaw package root>");
  console.log(`[user-bootstrap-cap] changed ${patchUserBootstrapCap(process.argv[2]).length} bundles; USER.md cap=26000; node --check passed`);
}
