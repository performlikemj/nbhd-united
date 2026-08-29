import test from "node:test";
import assert from "node:assert/strict";
import { mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

import siteEditorLib from "./lib.js";

const { createSiteEditor, HARD_DENY_PATHS } = siteEditorLib;

const TOKEN = "github_pat_TEST_TOKEN_123";
const BASE_CONFIG = {
  owner: "performlikemj",
  repo: "kihoko",
  branch: "main",
  allowPaths: ["**/*"],
  denyPaths: [],
  maxTextBytes: 262144,
  maxImageBytes: 2097152,
  maxFiles: 20,
  maxTotalBytes: 5242880,
  deployMinutes: 6,
  authorEmail: "site-editor@example.invalid",
};

function response(status, body = {}) {
  return {
    ok: status >= 200 && status < 300,
    status,
    async text() {
      return typeof body === "string" ? body : JSON.stringify(body);
    },
  };
}

function editor(overrides = {}, fetchImpl = async () => {
  throw new Error("unexpected fetch");
}) {
  return createSiteEditor({
    config: { ...BASE_CONFIG, ...overrides },
    env: { NBHD_SITE_GITHUB_TOKEN: TOKEN, NBHD_TENANT_ID: "13fa39df-74b6-4b17-b41e-ea0fc400fb13" },
    fetchImpl,
    now: () => new Date("2026-08-30T01:02:03.000Z"),
  });
}

function text(result) {
  return result.content[0].text;
}

test("path fence accepts intended source and rejects traversal plus every hard-deny class", async () => {
  const { tools } = editor({ denyPaths: ["web/src/styles/private/**"] });
  const rows = [
    ["web/src/pages/AboutPage.js", true],
    ["web/src/components/Header.jsx", true],
    ["web/src/styles/style.css", true],
    ["web/src/styles/private/hidden.css", false],
    ["../web/src/pages/AboutPage.js", false],
    ["/web/src/pages/AboutPage.js", false],
    ["C:/web/src/pages/AboutPage.js", false],
    ["web\\src\\pages\\AboutPage.js", false],
    ["web/src/pages/\0AboutPage.js", false],
    [".git/config", false],
    [".github/workflows/deploy.js", false],
    ["api/server.js", false],
    ["kihokosite/settings.js", false],
    ["djangoapp/views.js", false],
    ["djangoapp_backup/views.js", false],
    ["scripts/deploy.js", false],
    ["README.md", false],
    ["web/.babelrc", false],
    ["web/.env.production", false],
    ["web/package.json", false],
    ["web/package-lock.json", false],
    ["web/webpack.config.js", false],
    ["web/src/App.js", false],
    ["web/src/index.js", false],
    ["web/src/services/api.js", false],
    ["web/src/context/Auth.js", false],
    ["web/src/pages/AdminUploadPage.js", false],
    ["web/src/pages/LoginPage.js", false],
    ["web/src/pages/CartPage.js", false],
    ["web/public/app.js", false],
    ["web/public/data.json", false],
    ["web/public/icons/logo.svg", false],
    ["web/public/app.wasm", false],
    ["web/src/styles/client-secret.css", false],
    ["web/src/styles/client-credential.css", false],
    ["web/src/styles/cert.pem", false],
    ["web/src/styles/cert.key", false],
    ["web/src/styles/cert.p12", false],
    ["web/src/styles/cert.pfx", false],
    ["web/src/styles/cert.crt", false],
  ];
  assert.ok(rows.length >= 12);
  assert.ok(HARD_DENY_PATHS.length >= 12);
  for (const [repoPath, accepted] of rows) {
    const content = repoPath.endsWith(".css") ? "body { color: black; }" : "export const value = 1;";
    const result = await tools.site_stage_file({ path: repoPath, content });
    withAssertionContext(repoPath, () => {
      if (accepted) assert.match(text(result), /^Staged /);
      else assert.doesNotMatch(text(result), /^Staged /);
    });
  }
});

function withAssertionContext(label, assertion) {
  try {
    assertion();
  } catch (error) {
    error.message = `${label}: ${error.message}`;
    throw error;
  }
}

test("allow paths are checked before deny paths and listing denied folders fails closed", async () => {
  const { tools } = editor({
    allowPaths: ["web/src/styles/**/*.css"],
    denyPaths: ["web/src/styles/admin/**"],
  });
  assert.match(text(await tools.site_stage_file({ path: "web/src/styles/site.css", content: "a{}" })), /^Staged/);
  assert.match(
    text(await tools.site_stage_file({ path: "web/src/pages/AboutPage.js", content: "export default 1" })),
    /isn't editable/,
  );
  assert.equal(text(await tools.site_list_files({ path: ".github" })), "That folder isn't editable.");
});

test("listing can descend through nested double-star allow roots and hides non-regular entries", async () => {
  const fetchImpl = async () => response(200, [
    { type: "dir", name: "themes", path: "web/src/styles/themes", size: 0 },
    { type: "symlink", name: "linked.css", path: "web/src/styles/linked.css", size: 12 },
  ]);
  const { tools } = editor({ allowPaths: ["web/src/styles/**/*.css"] }, fetchImpl);
  const result = text(await tools.site_list_files({ path: "web/src/styles" }));
  assert.match(result, /folder\tthemes/);
  assert.doesNotMatch(result, /linked\.css/);
});

test("text, image, file-count, and total-byte caps are enforced", async (t) => {
  const capped = editor({ maxTextBytes: 4, maxImageBytes: 12, maxFiles: 1, maxTotalBytes: 12 }).tools;
  assert.match(
    text(await capped.site_stage_file({ path: "web/src/styles/site.css", content: "12345" })),
    /size limit/,
  );
  assert.match(text(await capped.site_stage_file({ path: "web/src/styles/a.css", content: "a{}" })), /^Staged/);
  assert.match(text(await capped.site_stage_file({ path: "web/src/styles/b.css", content: "b{}" })), /At most 1/);

  const directory = mkdtempSync(join(tmpdir(), "nbhd-site-editor-"));
  t.after(() => rmSync(directory, { recursive: true, force: true }));
  const oversized = join(directory, "large.png");
  writeFileSync(oversized, Buffer.concat([Buffer.from([137, 80, 78, 71, 13, 10, 26, 10]), Buffer.alloc(8)]));
  assert.match(
    text(await capped.site_stage_upload({ path: "web/public/large.png", local_path: oversized })),
    /size limit/,
  );

  const total = editor({ maxTextBytes: 10, maxFiles: 3, maxTotalBytes: 6 }).tools;
  assert.match(text(await total.site_stage_file({ path: "web/src/styles/a.css", content: "a{}" })), /^Staged/);
  assert.match(text(await total.site_stage_file({ path: "web/src/styles/b.css", content: "body{}" })), /total publish/);
});

test("invalid UTF-8 text and mismatched image magic bytes are rejected", async (t) => {
  const { tools } = editor();
  assert.match(
    text(await tools.site_stage_file({ path: "web/src/pages/AboutPage.js", content: "\ud800" })),
    /valid UTF-8/,
  );

  const directory = mkdtempSync(join(tmpdir(), "nbhd-site-editor-magic-"));
  t.after(() => rmSync(directory, { recursive: true, force: true }));
  const fakePng = join(directory, "fake.png");
  writeFileSync(fakePng, Buffer.from("GIF89a"));
  assert.match(
    text(await tools.site_stage_upload({ path: "web/public/fake.png", local_path: fakePng })),
    /don't match/,
  );
});

test("valid JPG, PNG, GIF, and WebP uploads pass magic-byte checks", async (t) => {
  const directory = mkdtempSync(join(tmpdir(), "nbhd-site-editor-images-"));
  t.after(() => rmSync(directory, { recursive: true, force: true }));
  const fixtures = [
    ["jpg", Buffer.from([0xff, 0xd8, 0xff, 0x00])],
    ["png", Buffer.from([137, 80, 78, 71, 13, 10, 26, 10])],
    ["gif", Buffer.from("GIF89a")],
    ["webp", Buffer.concat([Buffer.from("RIFF"), Buffer.alloc(4), Buffer.from("WEBP")])],
  ];
  const { tools } = editor();
  for (const [extension, bytes] of fixtures) {
    const localPath = join(directory, `image.${extension}`);
    writeFileSync(localPath, bytes);
    assert.match(
      text(await tools.site_stage_upload({ path: `web/public/image.${extension}`, local_path: localPath })),
      /^Staged/,
    );
  }
});

test("JSX and CSS parse failures cannot reach publish", async () => {
  const calls = [];
  const { tools, _state } = editor({}, async (...args) => {
    calls.push(args);
    return response(500, { message: "should not publish" });
  });
  assert.match(
    text(await tools.site_stage_file({ path: "web/src/pages/AboutPage.jsx", content: "const = <div>" })),
    /Couldn't stage/,
  );
  assert.match(
    text(await tools.site_stage_file({ path: "web/src/styles/site.css", content: "a { color: red" })),
    /Couldn't stage/,
  );
  assert.equal(_state.pending.size, 0);
  assert.match(text(await tools.site_publish({ message: "Bad syntax", confirm: true })), /No website changes/);
  assert.equal(calls.length, 0);
});

test("index.html blocks new scripts, inline handlers, and remote origins", async () => {
  const original = '<html><link href="https://cdn.example/base.css"></html>';
  const fetchImpl = async () => response(200, {
    type: "file",
    encoding: "base64",
    content: Buffer.from(original).toString("base64"),
  });
  for (const injection of [
    `${original}<script src="/evil.js"></script>`,
    `${original}<button onclick="evil()">x</button>`,
    `${original}<img src="https://evil.example/x.png">`,
  ]) {
    const { tools } = editor({}, fetchImpl);
    assert.match(
      text(await tools.site_stage_file({ path: "web/public/index.html", content: injection })),
      /index\.html cannot/,
    );
  }
});

test("show pending renders a unified LCS line diff", async () => {
  const current = "one\ntwo\nthree";
  const fetchImpl = async () => response(200, {
    type: "file",
    encoding: "base64",
    content: Buffer.from(current).toString("base64"),
  });
  const { tools } = editor({}, fetchImpl);
  await tools.site_stage_file({ path: "web/src/pages/AboutPage.js", content: "one\nchanged\nthree" });
  const shown = text(await tools.site_show_pending());
  assert.match(shown, /--- a\/web\/src\/pages\/AboutPage\.js/);
  assert.match(shown, /-two/);
  assert.match(shown, /\+changed/);
});

function githubFixture({ firstPatchStatus = 200 } = {}) {
  const calls = [];
  let patchCount = 0;
  let commitCount = 0;
  const fetchImpl = async (url, options) => {
    const parsed = new URL(url);
    const call = { url: parsed, options, body: options.body ? JSON.parse(options.body) : undefined };
    calls.push(call);
    const method = options.method;
    const pathname = parsed.pathname;
    if (method === "GET" && pathname.endsWith("/git/ref/heads/main")) {
      return response(200, { object: { sha: patchCount ? "head-2" : "head-1" } });
    }
    if (method === "GET" && pathname.includes("/git/commits/head-")) return response(200, { tree: { sha: "base-tree" } });
    if (method === "POST" && pathname.endsWith("/git/blobs")) return response(201, { sha: `blob-${calls.length}` });
    if (method === "POST" && pathname.endsWith("/git/trees")) return response(201, { sha: "new-tree" });
    if (method === "POST" && pathname.endsWith("/git/commits")) {
      commitCount += 1;
      return response(201, { sha: `commit-${commitCount}` });
    }
    if (method === "PATCH" && pathname.endsWith("/git/refs/heads/main")) {
      patchCount += 1;
      if (patchCount === 1 && firstPatchStatus !== 200) return response(firstPatchStatus, { message: "ref moved" });
      return response(200, { object: { sha: `commit-${commitCount}` } });
    }
    throw new Error(`unexpected ${method} ${pathname}`);
  };
  return { calls, fetchImpl };
}

test("publish uses the exact Git Data API sequence, fixed author, trailer, and non-force ref update", async () => {
  const fake = githubFixture();
  const { tools, _state } = editor({}, fake.fetchImpl);
  await tools.site_stage_file({ path: "web/src/pages/AboutPage.js", content: "export default <main>Hello</main>;" });
  const result = await tools.site_publish({ message: "Update About copy", confirm: true });

  assert.deepEqual(
    fake.calls.map((call) => `${call.options.method} ${call.url.pathname.replace("/repos/performlikemj/kihoko", "")}`),
    [
      "GET /git/ref/heads/main",
      "GET /git/commits/head-1",
      "POST /git/blobs",
      "POST /git/trees",
      "POST /git/commits",
      "PATCH /git/refs/heads/main",
    ],
  );
  for (const call of fake.calls) {
    assert.equal(call.options.headers.Accept, "application/vnd.github+json");
    assert.equal(call.options.headers["X-GitHub-Api-Version"], "2022-11-28");
    assert.equal(call.options.headers["User-Agent"], "nbhd-site-editor");
  }
  const commitCall = fake.calls.find((call) => call.options.method === "POST" && call.url.pathname.endsWith("/git/commits"));
  assert.equal(commitCall.body.message, "Update About copy\n\nNBHD-Tenant: 13fa39df");
  assert.deepEqual(commitCall.body.author, {
    name: "NBHD Site Editor (Pistachio)",
    email: "site-editor@example.invalid",
    date: "2026-08-30T01:02:03.000Z",
  });
  assert.deepEqual(commitCall.body.committer, commitCall.body.author);
  const patchCall = fake.calls.at(-1);
  assert.deepEqual(patchCall.body, { sha: "commit-1", force: false });
  assert.match(text(result), /Published commit commit-/);
  assert.match(text(result), /about 6 minutes/);
  assert.equal(_state.pending.size, 0);
});

test("a moved ref retries exactly once from a fresh ref", async () => {
  const fake = githubFixture({ firstPatchStatus: 422 });
  const { tools } = editor({}, fake.fetchImpl);
  await tools.site_stage_file({ path: "web/src/pages/AboutPage.js", content: "export default 2;" });
  assert.match(text(await tools.site_publish({ message: "Retry safely", confirm: true })), /Published commit/);
  assert.equal(fake.calls.filter((call) => call.options.method === "GET" && call.url.pathname.endsWith("/git/ref/heads/main")).length, 2);
  assert.equal(fake.calls.filter((call) => call.options.method === "PATCH").length, 2);
  assert.match(fake.calls[7].url.pathname, /git\/commits\/head-2$/);
});

test("a second moved ref fails loudly and retains pending changes", async () => {
  const fake = githubFixture({ firstPatchStatus: 422 });
  const originalFetch = fake.fetchImpl;
  let patches = 0;
  const { tools, _state } = editor({}, async (url, options) => {
    if (options.method === "PATCH") {
      patches += 1;
      return response(409, { message: "still moved" });
    }
    return originalFetch(url, options);
  });
  await tools.site_stage_file({ path: "web/src/pages/AboutPage.js", content: "export default 3;" });
  assert.match(text(await tools.site_publish({ message: "Retry twice", confirm: true })), /GitHub API 409/);
  assert.equal(patches, 2);
  assert.equal(_state.pending.size, 1);
});

test("tokens are redacted from every returned error even when GitHub echoes them", async () => {
  for (const echoed of [TOKEN, "ghp_ABC123secret"] ) {
    const { tools } = editor({}, async () => response(500, { message: `bad credential ${echoed}` }));
    const result = await tools.site_list_files({ path: "web" });
    assert.doesNotMatch(JSON.stringify(result), /github_pat_|ghp_/);
    assert.match(text(result), /\[REDACTED\]/);
  }
});

test("every tool fails closed without a token or complete config", async () => {
  const factories = [
    createSiteEditor({ config: BASE_CONFIG, env: {}, fetchImpl: async () => response(200) }),
    createSiteEditor({ config: {}, env: { NBHD_SITE_GITHUB_TOKEN: TOKEN }, fetchImpl: async () => response(200) }),
  ];
  const invocations = {
    site_list_files: { path: "" },
    site_read_file: { path: "web/src/pages/AboutPage.js" },
    site_stage_file: { path: "web/src/pages/AboutPage.js", content: "x" },
    site_stage_upload: { path: "web/public/a.png", local_path: "/missing" },
    site_show_pending: {},
    site_discard: {},
    site_publish: { message: "x", confirm: true },
    site_deploy_status: {},
  };
  for (const instance of factories) {
    for (const [name, args] of Object.entries(invocations)) {
      assert.equal(text(await instance.tools[name](args)), "Site editing isn't configured for this account.");
    }
  }
});

test("site_deploy_status maps queued, in-progress, success, and failure runs", async () => {
  const rows = [
    [{ status: "queued", conclusion: null }, /is queued;/],
    [{ status: "in_progress", conclusion: null }, /is in progress;/],
    [{ status: "completed", conclusion: "success" }, /completed with result success/],
    [{ status: "completed", conclusion: "failure" }, /completed with result failure/],
  ];
  for (const [run, expected] of rows) {
    const fetchImpl = async () => response(200, {
      workflow_runs: [{ ...run, head_sha: "abc123", updated_at: "2026-08-30T01:00:00Z" }],
    });
    const result = await editor({}, fetchImpl).tools.site_deploy_status();
    assert.match(text(result), expected);
    assert.match(text(result), /abc123/);
  }
});
