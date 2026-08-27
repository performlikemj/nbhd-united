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

test("send_to_user accepts and forwards an optional app thread UUID", async () => {
  const originalFetch = globalThis.fetch;
  const originalBaseUrl = process.env.NBHD_API_BASE_URL;
  const originalTenantId = process.env.NBHD_TENANT_ID;
  const originalInternalKey = process.env.NBHD_INTERNAL_API_KEY;
  const tools = collectTools();
  process.env.NBHD_API_BASE_URL = "https://nbhd.test";
  process.env.NBHD_TENANT_ID = "tenant-123";
  process.env.NBHD_INTERNAL_API_KEY = "internal-key";
  let request;

  try {
    globalThis.fetch = async (_url, options) => {
      request = options;
      return { ok: true, status: 200, async text() { return "{}"; } };
    };
    const threadId = "7c410ca8-33e7-42ed-b65c-95c42142e621";
    await tools.nbhd_send_to_user.execute("call-thread", {
      message: "Research is ready.",
      thread_id: threadId,
      occurrence_key: "subagent:announce-model-key",
    });

    assert.equal(tools.nbhd_send_to_user.parameters.properties.thread_id.format, "uuid");
    assert.doesNotMatch(
      tools.nbhd_send_to_user.parameters.properties.thread_id.description,
      /sub-agent/i,
    );
    assert.deepEqual(JSON.parse(request.body), {
      message: "Research is ready.",
      thread_id: threadId,
    });
    assert.equal(request.headers["X-NBHD-Occurrence-Key"], "subagent:announce-model-key");
  } finally {
    globalThis.fetch = originalFetch;
    if (originalBaseUrl === undefined) delete process.env.NBHD_API_BASE_URL;
    else process.env.NBHD_API_BASE_URL = originalBaseUrl;
    if (originalTenantId === undefined) delete process.env.NBHD_TENANT_ID;
    else process.env.NBHD_TENANT_ID = originalTenantId;
    if (originalInternalKey === undefined) delete process.env.NBHD_INTERNAL_API_KEY;
    else process.env.NBHD_INTERNAL_API_KEY = originalInternalKey;
  }
});

test("runtime field validation errors are surfaced to the model", async () => {
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
    globalThis.fetch = async () => ({
      ok: false,
      status: 400,
      async text() {
        return JSON.stringify({ week_rating: ["week_rating must be one of: thumbs-up, thumbs-down, meh."] });
      },
    });

    await assert.rejects(
      tools["nbhd_weekly_review_create"].execute("call-1", {
        week_start: "2026-07-20",
        week_end: "2026-07-26",
        week_rating: "great",
        mood_summary: "Mixed",
        raw_text: "Mixed week",
      }),
      /runtime_request_failed \(\{"week_rating":\["week_rating must be one of: thumbs-up, thumbs-down, meh\."\]\}\)/,
    );
  } finally {
    globalThis.fetch = originalFetch;
    if (originalTenantId === undefined) delete process.env.NBHD_TENANT_ID;
    else process.env.NBHD_TENANT_ID = originalTenantId;
    if (originalInternalKey === undefined) delete process.env.NBHD_INTERNAL_API_KEY;
    else process.env.NBHD_INTERNAL_API_KEY = originalInternalKey;
  }
});

test("journal_query.window.kind keeps its TIME-WINDOW enum (must not be clobbered)", () => {
  const tools = collectTools();
  const wkind = tools["nbhd_journal_query"].parameters.properties.window.properties.kind;
  assert.ok(Array.isArray(wkind.enum));
  assert.ok(wkind.enum.includes("today"), "time-window enum intact");
  assert.ok(!wkind.enum.includes("daily"), "must NOT have been replaced by the document enum");
});

test("purpose_update matches the runtime update statuses", () => {
  const tool = collectTools()["nbhd_purpose_update"];
  assert.deepEqual(tool.parameters.properties.status.enum, ["evolving", "confirmed"]);
  assert.doesNotMatch(tool.description, /status=['\"]retired|set status.*retired/i);
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

test("lesson and journal descriptions carry the rules-delivery contracts", () => {
  const tools = collectTools();
  const lesson = tools.nbhd_lesson_suggest;
  assert.match(lesson.description, /approved lesson immediately/);
  assert.match(lesson.description, /no approval queue/);
  assert.match(lesson.description, /First call `nbhd_lesson_search` and skip near-duplicates/);
  assert.match(lesson.description, /specific, actionable personal insight over trivia/);
  assert.match(lesson.description, /useful in six months/);
  assert.match(lesson.description, /trivial facts, temporary logistics, and vague or untestable statements/);
  assert.match(lesson.description, /Never write lessons into daily notes/);
  assert.match(lesson.description, /Supply 2–4 `tags`/);
  assert.match(lesson.parameters.properties.tags.description, /2–4 concise tags/);

  const setSection = tools.nbhd_daily_note_set_section.description;
  assert.match(setSection, /OVERWRITES the whole section/);
  assert.match(setSection, /first call `nbhd_daily_note_get`/);
  assert.match(setSection, /merge the existing section with the new content/);
  assert.match(setSection, /earlier content is preserved/);

  const constellation = tools.nbhd_constellation_notes.description;
  assert.match(constellation, /evidence about how the user learns/);
  assert.match(constellation, /use them to teach to this user/);
  assert.match(constellation, /never quote a raw signal back to them/);
});

test("memory and journal descriptions carry the W3 rules-delivery contracts", () => {
  const tools = collectTools();

  assert.ok(tools.nbhd_document_put.description.includes("Durable behavioural directives go to `kind=memory, slug=intents`, one directive per line"));

  const memoryGet = tools.nbhd_memory_get.description;
  assert.ok(memoryGet.includes("assistant-held patterns"));
  assert.ok(memoryGet.includes("for shared records, call nbhd_journal_search first"));

  const memoryUpdate = tools.nbhd_memory_update.description;
  assert.ok(memoryUpdate.includes("durable names, locations, and facts"));
  assert.ok(memoryUpdate.includes("stable preferences"));
  assert.ok(memoryUpdate.includes("month-worthy patterns"));
  assert.ok(memoryUpdate.includes("changed ongoing situations"));
  assert.ok(memoryUpdate.includes("Do not store routine details, journal duplicates, or unrequested sensitive emotions"));
  assert.ok(memoryUpdate.includes("When unsure, write less"));

  const context = tools.nbhd_journal_context.description;
  assert.ok(context.includes("when a cron preamble asks for it"));
  assert.ok(context.includes("when the user's request needs recent journal/backbone context"));
  assert.ok(context.includes("not at the start of ordinary chat turns"));

  assert.ok(tools.nbhd_journal_search.description.includes("Search shared journal records first"));

  const sessions = tools.nbhd_sessions_pending.description;
  assert.ok(sessions.includes("`nbhd_task_*` for tasks, `nbhd_goal_*` for goals"));
  assert.ok(!sessions.includes("for tasks (kind='tasks'), goals (kind='goal')"));
});

test("platform issue sender omits absent optional strings and preserves supplied context", async () => {
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
    let request;
    globalThis.fetch = async (url, options) => {
      request = { url: String(url), options };
      return {
        ok: true,
        status: 201,
        async text() { return JSON.stringify({ id: "issue-1", status: "logged" }); },
      };
    };

    await tools["nbhd_platform_issue_report"].execute("call-1", {
      category: "tool_error",
      summary: "journal write timed out",
    });

    assert.equal(
      request.url,
      "https://nbhd.test/api/v1/integrations/runtime/tenant-123/platform-issue/report/",
    );
    assert.deepEqual(JSON.parse(request.options.body), {
      category: "tool_error",
      severity: "low",
      summary: "journal write timed out",
    });

    await tools["nbhd_platform_issue_report"].execute("call-2", {
      category: "tool_error",
      severity: "high",
      tool_name: "  nbhd_daily_note_set_section  ",
      summary: "  journal write timed out  ",
      detail: "  request exceeded 20 seconds  ",
    });
    assert.deepEqual(JSON.parse(request.options.body), {
      category: "tool_error",
      severity: "high",
      summary: "journal write timed out",
      tool_name: "nbhd_daily_note_set_section",
      detail: "request exceeded 20 seconds",
    });
  } finally {
    globalThis.fetch = originalFetch;
    if (originalTenantId === undefined) delete process.env.NBHD_TENANT_ID;
    else process.env.NBHD_TENANT_ID = originalTenantId;
    if (originalInternalKey === undefined) delete process.env.NBHD_INTERNAL_API_KEY;
    else process.env.NBHD_INTERNAL_API_KEY = originalInternalKey;
  }
});

test("situation tool is registered with the city-label policy and manifest contract", () => {
  const tools = collectTools();
  const tool = tools["nbhd_update_situation"];
  assert.ok(tool, "nbhd_update_situation should be registered");
  assert.deepEqual(tool.parameters.required, ["place_label"]);
  assert.equal(tool.parameters.properties.place_label.type, "string");
  assert.equal(tool.parameters.additionalProperties, false);
  assert.match(tool.description, /Call this THAT TURN/);
  assert.match(tool.description, /CURRENT city/);
  assert.match(tool.description, /never guesses, sensors, documents, third parties/);
  assert.match(tool.description, /re-record when the user says they are still away/);
  assert.match(tool.description, /auto-expires after about 48 hours/);
  assert.match(tool.description, /NEVER use this for permanent home\/base changes/);
  assert.match(tool.description, /nbhd_update_profile/);
  assert.match(tool.description, /future travel, wait until the user is actually there or on the way now/);

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
      ok: true,
      status: 200,
      async text() {
        return JSON.stringify({
          ok: false,
          reason: "situational_context_disabled",
          message: "Current-location capture is disabled for this workspace, so it was not recorded.",
        });
      },
    });
    const disabled = await tool.execute("call-3", { place_label: "Osaka" });
    assert.deepEqual(disabled.details.json, {
      ok: false,
      reason: "situational_context_disabled",
      message: "Current-location capture is disabled for this workspace, so it was not recorded.",
    });

    globalThis.fetch = async () => ({
      ok: false,
      status: 503,
      async text() { return JSON.stringify({ error: "unavailable" }); },
    });
    const failed = await tool.execute("call-4", { place_label: "Osaka" });
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
