// Schema-contract tests for nbhd-journal-tools.
//   node --test runtime/openclaw/plugins/nbhd-journal-tools/schema.test.mjs
//
// Pins the 2026-06-13 fixes: document `kind` params constrained to the real
// Document.Kind enum (stops the runtime `invalid_kind` 400s) and the
// omission-prone tools leading with REQUIRED in their description.
import { test } from "node:test";
import assert from "node:assert/strict";
import register from "./index.js";

// Mirror of apps/journal/models.py Document.Kind — the server's source of truth.
const DOC_KINDS = ["daily", "weekly", "monthly", "goal", "project", "tasks", "ideas", "memory"];
// document_put excludes goal/tasks (dedicated typed tools own those).
const PUT_KINDS = ["daily", "weekly", "monthly", "project", "ideas", "memory"];
const DOC_KIND_TOOLS = ["nbhd_document_get", "nbhd_document_append", "nbhd_journal_search"];
const ALL_KIND_TOOLS = [...DOC_KIND_TOOLS, "nbhd_document_put"];

function collectTools() {
  const tools = {};
  const api = {
    registerTool(def) { tools[def.name] = def; },
    registerHook() {},
    on() {},
    logger: { info() {}, warn() {}, error() {}, debug() {} },
  };
  register(api);
  return tools;
}

test("read/append/search kind params carry the full Document.Kind enum (exact)", () => {
  const tools = collectTools();
  for (const name of DOC_KIND_TOOLS) {
    assert.ok(tools[name], `${name} should be registered`);
    const kind = tools[name].parameters.properties.kind;
    assert.ok(kind, `${name} has a kind param`);
    assert.deepEqual(
      [...kind.enum].sort(),
      [...DOC_KINDS].sort(),
      `${name}.kind enum must equal Document.Kind`,
    );
  }
});

test("document_put kind enum excludes goal/tasks (matches its description)", () => {
  const tools = collectTools();
  const kind = tools["nbhd_document_put"].parameters.properties.kind;
  assert.deepEqual([...kind.enum].sort(), [...PUT_KINDS].sort(), "put enum is the writable-freeform subset");
  assert.ok(!kind.enum.includes("goal") && !kind.enum.includes("tasks"), "put must not offer goal/tasks");
});

test("no kind enum contains a value the server would reject", () => {
  const tools = collectTools();
  for (const name of ALL_KIND_TOOLS) {
    for (const v of tools[name].parameters.properties.kind.enum) {
      assert.ok(DOC_KINDS.includes(v), `${name} kind enum has out-of-set value: ${v}`);
    }
  }
});

test("journal_query.window.kind keeps its TIME-WINDOW enum (must not be clobbered)", () => {
  const tools = collectTools();
  const wkind = tools["nbhd_journal_query"].parameters.properties.window.properties.kind;
  assert.ok(Array.isArray(wkind.enum));
  assert.ok(wkind.enum.includes("today"), "time-window enum intact");
  assert.ok(!wkind.enum.includes("daily"), "must NOT have been replaced by the document enum");
});

test("omission-prone tools name their required params in the description", () => {
  const tools = collectTools();
  const expect = {
    nbhd_daily_note_set_section: /section_slug/,
    nbhd_daily_note_append: /content/,
    nbhd_lesson_suggest: /text/,
    nbhd_task_update: /task_id/,
  };
  for (const [name, re] of Object.entries(expect)) {
    assert.match(tools[name].description, /REQUIRED/, `${name} flags REQUIRED`);
    assert.match(tools[name].description, re, `${name} names its required param`);
  }
});
