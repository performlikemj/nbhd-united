// Also runs during docker build: import the installed helper and its real dependencies.
import assert from "node:assert/strict";
import path from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";
import { BOOTSTRAP } from "./user-bootstrap-cap.mjs";

export function checkBootstrapHelper(buildBootstrapContextFiles) {
  const content = "Profile 東京 🏡\n".repeat(800).slice(0, 9499) + "!";
  assert.equal(content.length, 9500);
  const files = ["USER.md", "SOUL.md"].map(name => ({ name, path: `/workspace/${name}`, content, missing: false }));
  const warnings = [];
  const result = buildBootstrapContextFiles(files, { maxChars: 26000, totalMaxChars: 80000, warn: m => warnings.push(m) });
  assert.equal(result.length, 2);
  for (const file of result) assert.equal(file.content, content);
  assert.deepEqual(warnings, []);
  // Raising the USER ceiling must still honor a smaller configured/aggregate budget.
  for (const limits of [{ maxChars: 2000, totalMaxChars: 80000 }, { maxChars: 26000, totalMaxChars: 2000 }]) {
    const warnings = [];
    const [file] = buildBootstrapContextFiles(files, { ...limits, warn: m => warnings.push(m) });
    assert.ok(file.content.length <= 2000);
    assert.ok(warnings.some(m => m.includes("truncating in injected context")));
  }
}

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  assert.ok(process.argv[2], "Usage: node user-bootstrap-cap.smoke.mjs <openclaw package root>");
  const runtime = await import(pathToFileURL(path.join(process.argv[2], BOOTSTRAP)));
  assert.equal(runtime.t, 26000);
  checkBootstrapHelper(runtime.n);
  console.log("[user-bootstrap-cap] installed runtime loaded; USER.md and SOUL.md inject all 9500 chars without warnings; lower budgets enforced");
}
