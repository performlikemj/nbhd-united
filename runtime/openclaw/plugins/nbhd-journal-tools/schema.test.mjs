// Schema-contract tests for nbhd-journal-tools.
//   node --test runtime/openclaw/plugins/nbhd-journal-tools/schema.test.mjs
//
// Pins the 2026-06-13 fixes: document `kind` params constrained to the real
// Document.Kind enum (stops the runtime `invalid_kind` 400s) and the
// omission-prone tools leading with REQUIRED in their description.
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
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

test("situation tool is registered with the city-label policy and manifest contract", () => {
  const tools = collectTools();
  const tool = tools["nbhd_update_situation"];
  assert.ok(tool, "nbhd_update_situation should be registered");
  assert.deepEqual(tool.parameters.required, ["place_label"]);
  assert.equal(tool.parameters.properties.place_label.type, "string");
  assert.equal(tool.parameters.additionalProperties, false);
  assert.match(tool.description, /user STATES where they currently are/);
  assert.match(tool.description, /CURRENT city/);
  assert.match(tool.description, /auto-expires after about 48 hours/);
  assert.match(tool.description, /NEVER use this for permanent home\/base changes/);
  assert.match(tool.description, /nbhd_update_profile/);
  assert.match(tool.description, /future plan, ask before calling/);

  const manifest = JSON.parse(
    readFileSync(new URL("./openclaw.plugin.json", import.meta.url), "utf8"),
  );
  assert.ok(manifest.contracts.tools.includes("nbhd_update_situation"));
});

test("situation tool posts only place_label and reports rejected labels gracefully", async () => {
  const originalFetch = globalThis.fetch;
  const originalTenantId = process.env.NBHD_TENANT_ID;
  const originalInternalKey = process.env.NBHD_INTERNAL_API_KEY;
  const tools = {};
  const api = {
    pluginConfig: { apiBaseUrl: "https://nbhd.test" },
    registerTool(def) { tools[def.name] = def; },
    registerHook() {},
    on() {},
    logger: { info() {}, warn() {}, error() {}, debug() {} },
  };
  process.env.NBHD_TENANT_ID = "tenant-123";
  process.env.NBHD_INTERNAL_API_KEY = "internal-key";

  try {
    register(api);
    const tool = tools["nbhd_update_situation"];
    let request;
    globalThis.fetch = async (url, options) => {
      request = { url: String(url), options };
      return {
        ok: true,
        status: 200,
        async text() { return JSON.stringify({ ok: true, changed: true }); },
      };
    };

    const accepted = await tool.execute("call-1", { place_label: "  Osaka  ", latitude: 34.7 });
    assert.equal(request.url, "https://nbhd.test/api/v1/integrations/runtime/tenant-123/situation/");
    assert.equal(request.options.method, "POST");
    assert.equal(request.options.headers["X-NBHD-Internal-Key"], "internal-key");
    assert.equal(request.options.headers["X-NBHD-Tenant-Id"], "tenant-123");
    assert.deepEqual(JSON.parse(request.options.body), { place_label: "Osaka" });
    assert.deepEqual(accepted.details.json, { ok: true, changed: true });

    globalThis.fetch = async () => ({
      ok: true,
      status: 200,
      async text() { return JSON.stringify({ ok: false, reason: "invalid_label" }); },
    });
    const rejected = await tool.execute("call-2", { place_label: "# Osaka" });
    assert.equal(rejected.details.json.ok, false);
    assert.match(rejected.details.json.message, /not accepted/);

    globalThis.fetch = async () => ({
      ok: false,
      status: 503,
      async text() { return JSON.stringify({ error: "unavailable" }); },
    });
    const failed = await tool.execute("call-3", { place_label: "Osaka" });
    assert.equal(failed.details.json.ok, false);
    assert.equal(failed.details.json.reason, "runtime_request_failed");
    assert.match(failed.details.json.message, /not accepted/);
  } finally {
    globalThis.fetch = originalFetch;
    if (originalTenantId === undefined) delete process.env.NBHD_TENANT_ID;
    else process.env.NBHD_TENANT_ID = originalTenantId;
    if (originalInternalKey === undefined) delete process.env.NBHD_INTERNAL_API_KEY;
    else process.env.NBHD_INTERNAL_API_KEY = originalInternalKey;
  }
});
