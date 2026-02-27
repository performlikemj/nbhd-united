import test from "node:test";
import assert from "node:assert/strict";

import register from "./index.js";

function buildApi({ pluginConfig = { apiBaseUrl: "https://nbhd.test" } } = {}) {
  const tools = new Map();
  const api = {
    pluginConfig,
    registerTool(tool) {
      tools.set(tool.name, tool);
    },
  };
  return { api, tools };
}

function mockResponse({ status = 200, payload = {} } = {}) {
  return {
    ok: status >= 200 && status < 300,
    status,
    async text() {
      return JSON.stringify(payload);
    },
  };
}

function setupEnv() {
  process.env.NBHD_TENANT_ID = "tenant-test";
  process.env.NBHD_INTERNAL_API_KEY = "secret-key";
}

// ---------------------------------------------------------------------------
// nbhd_reddit_connect
// ---------------------------------------------------------------------------

test("nbhd_reddit_connect — happy path returns connect_url", async () => {
  setupEnv();
  const { api, tools } = buildApi();
  const calls = [];
  global.fetch = async (url, options) => {
    calls.push({ url: String(url), options });
    return mockResponse({ payload: { connect_url: "https://reddit.com/oauth/authorize?..." } });
  };

  register(api);
  const tool = tools.get("nbhd_reddit_connect");
  assert.ok(tool, "tool should be registered");

  const result = await tool.execute("1", {});
  assert.equal(calls.length, 1);
  assert.equal(calls[0].options.method, "POST");
  assert.equal(calls[0].options.headers["X-NBHD-Internal-Key"], "secret-key");
  assert.equal(calls[0].options.headers["X-NBHD-Tenant-Id"], "tenant-test");
  assert.match(calls[0].url, /\/runtime\/tenant-test\/reddit\/tool\/$/);

  const body = JSON.parse(calls[0].options.body);
  assert.equal(body.action, "connect");

  assert.ok(result.content);
  const parsed = JSON.parse(result.content[0].text);
  assert.equal(parsed.connect_url, "https://reddit.com/oauth/authorize?...");
});

test("nbhd_reddit_connect — missing NBHD_API_BASE_URL throws", async () => {
  const savedUrl = process.env.NBHD_API_BASE_URL;
  process.env.NBHD_TENANT_ID = "tenant-test";
  process.env.NBHD_INTERNAL_API_KEY = "secret-key";
  delete process.env.NBHD_API_BASE_URL;

  const { api, tools } = buildApi({ pluginConfig: {} });
  register(api);
  const tool = tools.get("nbhd_reddit_connect");

  await assert.rejects(
    () => tool.execute("1", {}),
    /NBHD_API_BASE_URL is required/,
  );

  process.env.NBHD_API_BASE_URL = savedUrl;
});

// ---------------------------------------------------------------------------
// nbhd_reddit_status
// ---------------------------------------------------------------------------

test("nbhd_reddit_status — connected true", async () => {
  setupEnv();
  const { api, tools } = buildApi();
  const calls = [];
  global.fetch = async (url, options) => {
    calls.push({ url: String(url), options });
    return mockResponse({ payload: { connected: true, provider_email: "user@example.com" } });
  };

  register(api);
  const tool = tools.get("nbhd_reddit_status");
  assert.ok(tool, "tool should be registered");

  const result = await tool.execute("2", {});
  const body = JSON.parse(calls[0].options.body);
  assert.equal(body.action, "status");

  const parsed = JSON.parse(result.content[0].text);
  assert.equal(parsed.connected, true);
  assert.equal(parsed.provider_email, "user@example.com");
});

test("nbhd_reddit_status — connected false", async () => {
  setupEnv();
  const { api, tools } = buildApi();
  global.fetch = async () =>
    mockResponse({ payload: { connected: false, provider_email: null } });

  register(api);
  const tool = tools.get("nbhd_reddit_status");

  const result = await tool.execute("3", {});
  const parsed = JSON.parse(result.content[0].text);
  assert.equal(parsed.connected, false);
});

// ---------------------------------------------------------------------------
// nbhd_reddit_digest
// ---------------------------------------------------------------------------

test("nbhd_reddit_digest — with subreddits and sort", async () => {
  setupEnv();
  const { api, tools } = buildApi();
  const calls = [];
  global.fetch = async (url, options) => {
    calls.push({ url: String(url), options });
    return mockResponse({ payload: { posts: [] } });
  };

  register(api);
  const tool = tools.get("nbhd_reddit_digest");
  assert.ok(tool, "tool should be registered");

  await tool.execute("4", { subreddits: ["javascript", "python"], sort: "new", limit: 10 });

  const body = JSON.parse(calls[0].options.body);
  assert.equal(body.action, "digest");
  assert.deepEqual(body.subreddits, ["javascript", "python"]);
  assert.equal(body.sort, "new");
  assert.equal(body.limit, 10);
});

test("nbhd_reddit_digest — default params (no subreddits, defaults hot/5)", async () => {
  setupEnv();
  const { api, tools } = buildApi();
  const calls = [];
  global.fetch = async (url, options) => {
    calls.push({ url: String(url), options });
    return mockResponse({ payload: { posts: [] } });
  };

  register(api);
  const tool = tools.get("nbhd_reddit_digest");

  await tool.execute("5", {});

  const body = JSON.parse(calls[0].options.body);
  assert.equal(body.action, "digest");
  assert.equal(body.subreddits, undefined);
  assert.equal(body.sort, "hot");
  assert.equal(body.limit, 5);
});

test("nbhd_reddit_digest — rejects array exceeding 10 subreddits", async () => {
  setupEnv();
  const { api, tools } = buildApi();
  global.fetch = async () => mockResponse({ payload: {} });

  register(api);
  const tool = tools.get("nbhd_reddit_digest");

  await assert.rejects(
    () => tool.execute("6", { subreddits: Array.from({ length: 11 }, (_, i) => `sub${i}`) }),
    /Array exceeds max items/,
  );
});

// ---------------------------------------------------------------------------
// nbhd_reddit_post
// ---------------------------------------------------------------------------

test("nbhd_reddit_post — happy path self post", async () => {
  setupEnv();
  const { api, tools } = buildApi();
  const calls = [];
  global.fetch = async (url, options) => {
    calls.push({ url: String(url), options });
    return mockResponse({ payload: { post_url: "https://reddit.com/r/test/comments/abc" } });
  };

  register(api);
  const tool = tools.get("nbhd_reddit_post");
  assert.ok(tool, "tool should be registered");

  await tool.execute("7", { subreddit: "test", title: "Hello World", text: "body text" });

  const body = JSON.parse(calls[0].options.body);
  assert.equal(body.action, "post");
  assert.equal(body.subreddit, "test");
  assert.equal(body.title, "Hello World");
  assert.equal(body.kind, "self");
});

test("nbhd_reddit_post — rejects title over 300 chars", async () => {
  setupEnv();
  const { api, tools } = buildApi();
  global.fetch = async () => mockResponse({ payload: {} });

  register(api);
  const tool = tools.get("nbhd_reddit_post");

  await assert.rejects(
    () => tool.execute("8", { subreddit: "test", title: "x".repeat(301), text: "body" }),
    /title must not exceed 300 characters/,
  );
});

test("nbhd_reddit_post — rejects missing subreddit", async () => {
  setupEnv();
  const { api, tools } = buildApi();
  global.fetch = async () => mockResponse({ payload: {} });

  register(api);
  const tool = tools.get("nbhd_reddit_post");

  await assert.rejects(
    () => tool.execute("9", { title: "Hello", text: "body" }),
    /subreddit is required/,
  );
});

test("nbhd_reddit_post — rejects missing text for self post", async () => {
  setupEnv();
  const { api, tools } = buildApi();
  global.fetch = async () => mockResponse({ payload: {} });

  register(api);
  const tool = tools.get("nbhd_reddit_post");

  await assert.rejects(
    () => tool.execute("10", { subreddit: "test", title: "Hello" }),
    /text is required for self posts/,
  );
});

// ---------------------------------------------------------------------------
// nbhd_reddit_reply
// ---------------------------------------------------------------------------

test("nbhd_reddit_reply — happy path with valid t3_ thing_id", async () => {
  setupEnv();
  const { api, tools } = buildApi();
  const calls = [];
  global.fetch = async (url, options) => {
    calls.push({ url: String(url), options });
    return mockResponse({ payload: { comment_url: "https://reddit.com/r/test/comments/abc/_/xyz" } });
  };

  register(api);
  const tool = tools.get("nbhd_reddit_reply");
  assert.ok(tool, "tool should be registered");

  await tool.execute("11", { thing_id: "t3_abc123", text: "Great post!" });

  const body = JSON.parse(calls[0].options.body);
  assert.equal(body.action, "reply");
  assert.equal(body.thing_id, "t3_abc123");
  assert.equal(body.text, "Great post!");
});

test("nbhd_reddit_reply — happy path with valid t1_ thing_id", async () => {
  setupEnv();
  const { api, tools } = buildApi();
  const calls = [];
  global.fetch = async (url, options) => {
    calls.push({ url: String(url), options });
    return mockResponse({ payload: { comment_url: "https://reddit.com/..." } });
  };

  register(api);
  const tool = tools.get("nbhd_reddit_reply");

  await tool.execute("12", { thing_id: "t1_xyz789", text: "Nice comment!" });

  const body = JSON.parse(calls[0].options.body);
  assert.equal(body.thing_id, "t1_xyz789");
});

test("nbhd_reddit_reply — rejects invalid thing_id format", async () => {
  setupEnv();
  const { api, tools } = buildApi();
  global.fetch = async () => mockResponse({ payload: {} });

  register(api);
  const tool = tools.get("nbhd_reddit_reply");

  await assert.rejects(
    () => tool.execute("13", { thing_id: "not_a_valid_id", text: "reply" }),
    /thing_id must be a valid Reddit fullname/,
  );
});

test("nbhd_reddit_reply — rejects missing text", async () => {
  setupEnv();
  const { api, tools } = buildApi();
  global.fetch = async () => mockResponse({ payload: {} });

  register(api);
  const tool = tools.get("nbhd_reddit_reply");

  await assert.rejects(
    () => tool.execute("14", { thing_id: "t3_abc123", text: "" }),
    /text is required/,
  );
});

// ---------------------------------------------------------------------------
// Tool registration count
// ---------------------------------------------------------------------------

test("registers exactly 6 Reddit tools", () => {
  setupEnv();
  const { api, tools } = buildApi();
  register(api);

  const expected = [
    "nbhd_reddit_connect",
    "nbhd_reddit_status",
    "nbhd_reddit_digest",
    "nbhd_reddit_my_activity",
    "nbhd_reddit_post",
    "nbhd_reddit_reply",
  ];

  assert.equal(tools.size, expected.length, `expected ${expected.length} tools, got ${tools.size}`);
  for (const name of expected) {
    assert.ok(tools.has(name), `missing tool: ${name}`);
  }
});

// ---------------------------------------------------------------------------
// Runtime error surfacing
// ---------------------------------------------------------------------------

test("runtime error payloads are surfaced with error code/detail", async () => {
  setupEnv();
  const { api, tools } = buildApi();
  global.fetch = async () =>
    mockResponse({
      status: 403,
      payload: { error: "reddit_not_connected", detail: "OAuth token missing" },
    });

  register(api);
  const tool = tools.get("nbhd_reddit_status");

  await assert.rejects(
    () => tool.execute("15", {}),
    /reddit_not_connected \(OAuth token missing\)/,
  );
});
