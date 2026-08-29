"use strict";

const fs = require("fs");
const path = require("path");
const { TextDecoder } = require("util");

const { parse: parseJavaScript } = require("@babel/parser");
const postcss = require("postcss");

const NOT_CONFIGURED = "Site editing isn't configured for this account.";
const TEXT_EXTENSIONS = new Set([".js", ".jsx", ".css", ".html", ".md", ".txt"]);
const IMAGE_EXTENSIONS = new Set([".jpg", ".jpeg", ".png", ".gif", ".webp"]);
const HARD_DENY_PATHS = [
  ".git/**",
  ".github/**",
  "api/**",
  "kihokosite/**",
  "djangoapp/**",
  "djangoapp_backup/**",
  "scripts/**",
  ".gitignore",
  "README.md",
  "STORE_GUIDE.md",
  "env.example",
  "staticwebapp.config.json",
  "web/.babelrc",
  "web/.env*",
  "web/README.md",
  "web/package.json",
  "web/package-lock.json",
  "web/webpack.config.js",
  "web/staticwebapp.config.json",
  "web/src/App.js",
  "web/src/index.js",
  "web/src/services/**",
  "web/src/context/**",
  "web/src/pages/AdminUploadPage.js",
  "web/src/pages/LoginPage.js",
  "web/src/pages/SignupPage.js",
  "web/src/pages/CartPage.js",
  "web/src/pages/ProductDetailPage.js",
  "web/src/pages/CheckoutResultPage.js",
  "web/public/**/*.js",
  "web/public/**/*.jsx",
  "web/public/**/*.json",
  "web/public/**/*.map",
  "web/public/**/*.svg",
  "web/public/**/*.wasm",
  "**/.env*",
  "**/local.settings*",
  "**/*secret*",
  "**/*credential*",
  "**/*.pem",
  "**/*.key",
  "**/*.p12",
  "**/*.pfx",
  "**/*.crt",
  "**/package.json",
  "**/package-lock.json",
];

function redactToken(value) {
  return String(value)
    .replace(/gh[pousr]_[A-Za-z0-9]+/g, "[REDACTED]")
    .replace(/github_pat_[A-Za-z0-9_]+/g, "[REDACTED]");
}

function toolText(value) {
  return { content: [{ type: "text", text: redactToken(value) }] };
}

function errorText(prefix, error) {
  const message = error && error.message ? error.message : String(error);
  return toolText(`${prefix}: ${message.replace(/\s+/g, " ").trim().slice(0, 800)}`);
}

function positiveInt(value, fallback, ceiling) {
  if (typeof value !== "number" || !Number.isInteger(value) || value <= 0) return fallback;
  return Math.min(value, ceiling);
}

function globRegex(pattern, ignoreCase = false) {
  let source = "";
  for (let i = 0; i < pattern.length; i += 1) {
    const char = pattern[i];
    if (char === "*") {
      if (pattern[i + 1] === "*") {
        if (pattern[i + 2] === "/") {
          source += "(?:.*/)?";
          i += 2;
        } else {
          source += ".*";
          i += 1;
        }
      } else {
        source += "[^/]*";
      }
    } else if (char === "?") {
      source += "[^/]";
    } else if (char === "{") {
      const close = pattern.indexOf("}", i + 1);
      if (close !== -1) {
        const choices = pattern
          .slice(i + 1, close)
          .split(",")
          .map((part) => part.replace(/[|\\{}()[\]^$+*?.-]/g, "\\$&"));
        source += `(?:${choices.join("|")})`;
        i = close;
      } else {
        source += "\\{";
      }
    } else {
      source += char.replace(/[|\\{}()[\]^$+*?.-]/g, "\\$&");
    }
  }
  return new RegExp(`^${source}$`, ignoreCase ? "i" : "");
}

function matchesGlob(value, pattern, ignoreCase = false) {
  if (globRegex(pattern, ignoreCase).test(value)) return true;
  if (pattern.endsWith("/**")) {
    const base = pattern.slice(0, -3).replace(/\/$/, "");
    return ignoreCase ? value.toLowerCase() === base.toLowerCase() : value === base;
  }
  return false;
}

function validatePath(rawPath, { allowEmpty = false } = {}) {
  if (typeof rawPath !== "string") throw new Error("A repository-relative path is required.");
  if (rawPath === "" && allowEmpty) return "";
  if (
    !rawPath ||
    rawPath.includes("\0") ||
    rawPath.includes("\\") ||
    rawPath.startsWith("/") ||
    /^[A-Za-z]:\//.test(rawPath)
  ) {
    throw new Error("Use a safe repository-relative path.");
  }
  const segments = rawPath.split("/");
  if (segments.some((segment) => !segment || segment === "." || segment === "..")) {
    throw new Error("Use a safe repository-relative path.");
  }
  return rawPath;
}

function literalPrefix(pattern) {
  const wildcard = pattern.search(/[?*{]/);
  const prefix = wildcard === -1 ? pattern : pattern.slice(0, wildcard);
  return prefix.replace(/\/+$/, "");
}

function createSiteEditor({ config, env = {}, fetchImpl = globalThis.fetch, now = () => new Date() }) {
  const cfg = config && typeof config === "object" && !Array.isArray(config) ? config : {};
  const allowPaths = Array.isArray(cfg.allowPaths) ? cfg.allowPaths.filter((item) => typeof item === "string") : [];
  const configured = Boolean(
    typeof cfg.owner === "string" && cfg.owner.trim() &&
      typeof cfg.repo === "string" && cfg.repo.trim() &&
      typeof cfg.branch === "string" && cfg.branch.trim() &&
      allowPaths.length,
  );
  const denyPaths = [
    ...(Array.isArray(cfg.denyPaths) ? cfg.denyPaths.filter((item) => typeof item === "string") : []),
    ...HARD_DENY_PATHS,
  ];
  const limits = {
    maxTextBytes: positiveInt(cfg.maxTextBytes, 256 * 1024, 256 * 1024),
    maxImageBytes: positiveInt(cfg.maxImageBytes, 2 * 1024 * 1024, 2 * 1024 * 1024),
    maxFiles: positiveInt(cfg.maxFiles, 20, 20),
    maxTotalBytes: positiveInt(cfg.maxTotalBytes, 5 * 1024 * 1024, 5 * 1024 * 1024),
    deployMinutes: positiveInt(cfg.deployMinutes, 5, 120),
  };
  const pending = new Map();

  function hasToken() {
    return typeof env.NBHD_SITE_GITHUB_TOKEN === "string" && env.NBHD_SITE_GITHUB_TOKEN.trim();
  }

  function ready() {
    return configured && hasToken() && typeof fetchImpl === "function";
  }

  function denied(repoPath) {
    return denyPaths.some((pattern) => matchesGlob(repoPath, pattern, true));
  }

  function allowedFile(repoPath) {
    return allowPaths.some((pattern) => matchesGlob(repoPath, pattern)) && !denied(repoPath);
  }

  function allowedDirectory(repoPath) {
    if (denied(repoPath)) return false;
    if (!repoPath) return allowPaths.length > 0;
    return allowPaths.some((pattern) => {
      if (matchesGlob(repoPath, pattern)) return true;
      const prefix = literalPrefix(pattern);
      return (
        prefix === repoPath ||
        prefix.startsWith(`${repoPath}/`) ||
        (pattern.includes("**") && repoPath.startsWith(`${prefix}/`))
      );
    });
  }

  function assertEditableFile(repoPath) {
    const checked = validatePath(repoPath);
    if (!allowedFile(checked)) throw new Error("That file isn't editable.");
    return checked;
  }

  function assertTextPath(repoPath) {
    const checked = assertEditableFile(repoPath);
    if (!TEXT_EXTENSIONS.has(path.posix.extname(checked).toLowerCase())) {
      throw new Error("Only approved text file types can be edited.");
    }
    return checked;
  }

  function utf8Buffer(content) {
    if (typeof content !== "string") throw new Error("Content must be text.");
    const buffer = Buffer.from(content, "utf8");
    if (buffer.toString("utf8") !== content) throw new Error("Content must be valid UTF-8.");
    return buffer;
  }

  function decodeUtf8(buffer) {
    try {
      return new TextDecoder("utf-8", { fatal: true }).decode(buffer);
    } catch {
      throw new Error("The file isn't valid UTF-8.");
    }
  }

  function checkPendingCapacity(repoPath, byteLength) {
    const existing = pending.get(repoPath);
    const nextCount = pending.size + (existing ? 0 : 1);
    let total = byteLength;
    for (const [candidate, item] of pending) {
      if (candidate !== repoPath) total += item.buffer.length;
    }
    if (nextCount > limits.maxFiles) throw new Error(`At most ${limits.maxFiles} files can be staged.`);
    if (total > limits.maxTotalBytes) throw new Error("The staged changes exceed the total publish size limit.");
  }

  function imageKind(buffer) {
    if (buffer.length >= 3 && buffer[0] === 0xff && buffer[1] === 0xd8 && buffer[2] === 0xff) return "jpg";
    if (buffer.length >= 8 && buffer.subarray(0, 8).equals(Buffer.from([137, 80, 78, 71, 13, 10, 26, 10]))) {
      return "png";
    }
    const firstSix = buffer.subarray(0, 6).toString("ascii");
    if (firstSix === "GIF87a" || firstSix === "GIF89a") return "gif";
    if (
      buffer.length >= 12 &&
      buffer.subarray(0, 4).toString("ascii") === "RIFF" &&
      buffer.subarray(8, 12).toString("ascii") === "WEBP"
    ) {
      return "webp";
    }
    return "";
  }

  function validateImage(repoPath, buffer) {
    const ext = path.posix.extname(repoPath).toLowerCase();
    if (!IMAGE_EXTENSIONS.has(ext)) throw new Error("Only JPG, PNG, GIF, or WebP images can be uploaded.");
    const expected = ext === ".jpeg" ? "jpg" : ext.slice(1);
    if (imageKind(buffer) !== expected) throw new Error("The image contents don't match the file extension.");
  }

  function validateSyntax(repoPath, content) {
    const ext = path.posix.extname(repoPath).toLowerCase();
    if (ext === ".js" || ext === ".jsx") {
      parseJavaScript(content, { sourceType: "unambiguous", plugins: ["jsx"] });
    } else if (ext === ".css") {
      postcss.parse(content, { from: repoPath });
    }
  }

  function remoteOrigins(content) {
    const origins = new Set();
    for (const match of content.matchAll(/https?:\/\/[^\s"'<>`)]+/gi)) {
      try {
        origins.add(new URL(match[0]).origin);
      } catch {
        // An incomplete URL is left to the site's own parser/build checks.
      }
    }
    return origins;
  }

  function validateIndexHtml(oldContent, newContent) {
    const count = (text, regex) => (text.match(regex) || []).length;
    if (count(newContent, /<script\b/gi) > count(oldContent, /<script\b/gi)) {
      throw new Error("index.html cannot add script tags.");
    }
    if (count(newContent, /\son[a-z]+\s*=/gi) > count(oldContent, /\son[a-z]+\s*=/gi)) {
      throw new Error("index.html cannot add inline event handlers.");
    }
    const oldOrigins = remoteOrigins(oldContent);
    for (const origin of remoteOrigins(newContent)) {
      if (!oldOrigins.has(origin)) throw new Error("index.html cannot add a new remote origin.");
    }
  }

  function encodeRepoPath(repoPath) {
    return repoPath.split("/").map(encodeURIComponent).join("/");
  }

  function apiUrl(resource) {
    return `https://api.github.com/repos/${encodeURIComponent(cfg.owner.trim())}/${encodeURIComponent(cfg.repo.trim())}${resource}`;
  }

  async function github(resource, { method = "GET", body, allowNotFound = false } = {}) {
    const response = await fetchImpl(apiUrl(resource), {
      method,
      headers: {
        Authorization: `Bearer ${env.NBHD_SITE_GITHUB_TOKEN.trim()}`,
        Accept: "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "nbhd-site-editor",
        ...(body === undefined ? {} : { "Content-Type": "application/json" }),
      },
      ...(body === undefined ? {} : { body: JSON.stringify(body) }),
    });
    const raw = await response.text();
    let data = {};
    if (raw) {
      try {
        data = JSON.parse(raw);
      } catch {
        data = { message: raw };
      }
    }
    if (allowNotFound && response.status === 404) return null;
    if (!response.ok) {
      const detail = data && data.message ? data.message : raw || `HTTP ${response.status}`;
      const error = new Error(`GitHub API ${response.status}: ${detail}`);
      error.status = response.status;
      throw error;
    }
    return data;
  }

  async function getContents(repoPath, { allowNotFound = false } = {}) {
    const suffix = repoPath ? `/${encodeRepoPath(repoPath)}` : "";
    return github(`/contents${suffix}?ref=${encodeURIComponent(cfg.branch.trim())}`, { allowNotFound });
  }

  async function currentText(repoPath, { allowNotFound = false } = {}) {
    const data = await getContents(repoPath, { allowNotFound });
    if (data === null) return "";
    if (!data || data.type !== "file" || data.encoding !== "base64" || typeof data.content !== "string") {
      throw new Error("The repository entry isn't a regular file.");
    }
    const buffer = Buffer.from(data.content.replace(/\s+/g, ""), "base64");
    if (buffer.length > limits.maxTextBytes) throw new Error("The text file exceeds the configured size limit.");
    return decodeUtf8(buffer);
  }

  function unifiedDiff(repoPath, oldText, newText) {
    const oldLines = oldText.split("\n");
    const newLines = newText.split("\n");
    const operations = [];
    if (oldLines.length * newLines.length > 1_000_000) {
      operations.push(...oldLines.map((line) => `-${line}`), ...newLines.map((line) => `+${line}`));
    } else {
      const table = Array.from({ length: oldLines.length + 1 }, () => new Uint32Array(newLines.length + 1));
      for (let i = oldLines.length - 1; i >= 0; i -= 1) {
        for (let j = newLines.length - 1; j >= 0; j -= 1) {
          table[i][j] = oldLines[i] === newLines[j] ? table[i + 1][j + 1] + 1 : Math.max(table[i + 1][j], table[i][j + 1]);
        }
      }
      let i = 0;
      let j = 0;
      while (i < oldLines.length || j < newLines.length) {
        if (i < oldLines.length && j < newLines.length && oldLines[i] === newLines[j]) {
          operations.push(` ${oldLines[i]}`);
          i += 1;
          j += 1;
        } else if (j < newLines.length && (i === oldLines.length || table[i][j + 1] >= table[i + 1][j])) {
          operations.push(`+${newLines[j]}`);
          j += 1;
        } else {
          operations.push(`-${oldLines[i]}`);
          i += 1;
        }
      }
    }
    return [
      `--- a/${repoPath}`,
      `+++ b/${repoPath}`,
      `@@ -1,${oldLines.length} +1,${newLines.length} @@`,
      ...operations,
    ].join("\n");
  }

  function validateAllPending() {
    for (const [repoPath, item] of pending) {
      if (item.kind === "text") validateSyntax(repoPath, item.content);
      else validateImage(repoPath, item.buffer);
    }
  }

  async function publishAttempt(commitMessage) {
    const branchRef = `heads/${cfg.branch.trim()}`;
    const ref = await github(`/git/ref/${encodeRepoPath(branchRef)}`);
    const headSha = ref && ref.object && ref.object.sha;
    if (!headSha) throw new Error("GitHub returned a ref without a commit SHA.");
    const headCommit = await github(`/git/commits/${encodeURIComponent(headSha)}`);
    const baseTree = headCommit && headCommit.tree && headCommit.tree.sha;
    if (!baseTree) throw new Error("GitHub returned a commit without a tree SHA.");

    const tree = [];
    for (const [repoPath, item] of pending) {
      const blob = await github("/git/blobs", {
        method: "POST",
        body: { content: item.buffer.toString("base64"), encoding: "base64" },
      });
      tree.push({ path: repoPath, mode: "100644", type: "blob", sha: blob.sha });
    }
    const newTree = await github("/git/trees", { method: "POST", body: { base_tree: baseTree, tree } });
    const timestamp = now();
    const date = (timestamp instanceof Date ? timestamp : new Date(timestamp)).toISOString();
    const signature = {
      name: "NBHD Site Editor (Pistachio)",
      email:
        typeof cfg.authorEmail === "string" && cfg.authorEmail.trim()
          ? cfg.authorEmail.trim()
          : "nbhd-site-editor@users.noreply.github.com",
      date,
    };
    const commit = await github("/git/commits", {
      method: "POST",
      body: { message: commitMessage, tree: newTree.sha, parents: [headSha], author: signature, committer: signature },
    });
    try {
      await github(`/git/refs/${encodeRepoPath(branchRef)}`, {
        method: "PATCH",
        body: { sha: commit.sha, force: false },
      });
    } catch (error) {
      if (error.status === 409 || error.status === 422) error.refMoved = true;
      throw error;
    }
    return commit.sha;
  }

  async function guarded(work, prefix = "Couldn't complete the site edit") {
    if (!ready()) return toolText(NOT_CONFIGURED);
    try {
      return await work();
    } catch (error) {
      return errorText(prefix, error);
    }
  }

  const tools = {
    async site_list_files({ path: directory = "" } = {}) {
      return guarded(async () => {
        const repoPath = validatePath(directory, { allowEmpty: true });
        if (!allowedDirectory(repoPath)) return toolText("That folder isn't editable.");
        const data = await getContents(repoPath);
        if (!Array.isArray(data)) throw new Error("The repository entry isn't a folder.");
        const visible = data
          .filter(
            (item) =>
              item &&
              (item.type === "file" || item.type === "dir") &&
              typeof item.path === "string" &&
              allowedDirectory(item.path),
          )
          .map((item) => `${item.type === "dir" ? "folder" : "file"}\t${item.name}\t${item.size || 0} bytes`);
        return toolText(visible.length ? visible.join("\n") : "No editable files in that folder.");
      }, "Couldn't list website files");
    },

    async site_read_file({ path: repoPath } = {}) {
      return guarded(async () => {
        const checked = assertTextPath(repoPath);
        return toolText(await currentText(checked));
      }, "Couldn't read the website file");
    },

    async site_stage_file({ path: repoPath, content } = {}) {
      return guarded(async () => {
        const checked = assertTextPath(repoPath);
        const buffer = utf8Buffer(content);
        if (buffer.length > limits.maxTextBytes) throw new Error("The text file exceeds the configured size limit.");
        validateSyntax(checked, content);
        if (checked === "web/public/index.html") {
          const oldContent = await currentText(checked, { allowNotFound: true });
          validateIndexHtml(oldContent, content);
        }
        checkPendingCapacity(checked, buffer.length);
        pending.set(checked, { kind: "text", content, buffer });
        return toolText(`Staged ${checked} (${buffer.length} bytes).`);
      }, "Couldn't stage the website file");
    },

    async site_stage_upload({ path: repoPath, local_path: localPath } = {}) {
      return guarded(async () => {
        const checked = assertEditableFile(repoPath);
        if (typeof localPath !== "string" || !localPath) throw new Error("A local upload path is required.");
        const stat = fs.statSync(localPath);
        if (!stat.isFile()) throw new Error("The upload source isn't a regular file.");
        if (stat.size > limits.maxImageBytes) throw new Error("The image exceeds the configured size limit.");
        const buffer = fs.readFileSync(localPath);
        validateImage(checked, buffer);
        checkPendingCapacity(checked, buffer.length);
        pending.set(checked, { kind: "binary", buffer });
        return toolText(`Staged ${checked} (binary, ${buffer.length} bytes).`);
      }, "Couldn't stage the website image");
    },

    async site_show_pending() {
      return guarded(async () => {
        if (!pending.size) return toolText("No website changes are staged.");
        const sections = [];
        for (const [repoPath, item] of pending) {
          if (item.kind === "binary") {
            sections.push(`${repoPath}: binary, ${item.buffer.length} bytes`);
          } else {
            const oldContent = await currentText(repoPath, { allowNotFound: true });
            sections.push(unifiedDiff(repoPath, oldContent, item.content));
          }
        }
        return toolText(sections.join("\n\n"));
      }, "Couldn't show the staged website changes");
    },

    async site_discard({ path: repoPath } = {}) {
      return guarded(async () => {
        if (repoPath === undefined || repoPath === null || repoPath === "") {
          const count = pending.size;
          pending.clear();
          return toolText(`Discarded ${count} staged file${count === 1 ? "" : "s"}.`);
        }
        const checked = validatePath(repoPath);
        const removed = pending.delete(checked);
        return toolText(removed ? `Discarded ${checked}.` : `${checked} wasn't staged.`);
      }, "Couldn't discard the staged website change");
    },

    async site_publish({ message, confirm } = {}) {
      return guarded(async () => {
        if (confirm !== true) throw new Error("Publishing requires explicit confirmation.");
        if (typeof message !== "string" || !message.trim()) throw new Error("A short commit message is required.");
        const cleanMessage = message.trim();
        if (cleanMessage.length > 200 || /[\r\n]/.test(cleanMessage)) {
          throw new Error("The commit message must be one line and at most 200 characters.");
        }
        if (!pending.size) throw new Error("No website changes are staged.");
        validateAllPending();
        const tenantId = typeof env.NBHD_TENANT_ID === "string" ? env.NBHD_TENANT_ID.trim() : "";
        const commitMessage = tenantId ? `${cleanMessage}\n\nNBHD-Tenant: ${tenantId.slice(0, 8)}` : cleanMessage;
        let sha;
        try {
          sha = await publishAttempt(commitMessage);
        } catch (error) {
          if (!error.refMoved) throw error;
          sha = await publishAttempt(commitMessage);
        }
        pending.clear();
        const shortSha = sha.slice(0, 7);
        const url = `https://github.com/${cfg.owner.trim()}/${cfg.repo.trim()}/commit/${sha}`;
        return toolText(`Published commit ${shortSha}: ${url}. Deployment usually takes about ${limits.deployMinutes} minutes.`);
      }, "Couldn't publish the website changes");
    },

    async site_deploy_status() {
      return guarded(async () => {
        const data = await github(
          `/actions/runs?branch=${encodeURIComponent(cfg.branch.trim())}&per_page=1`,
        );
        const run = data && Array.isArray(data.workflow_runs) ? data.workflow_runs[0] : null;
        if (!run) return toolText("No deployment run was found for this branch.");
        const status = run.status === "completed" ? "completed" : run.status === "in_progress" ? "in progress" : "queued";
        const conclusion = run.conclusion ? ` with result ${run.conclusion}` : "";
        return toolText(
          `Latest deployment is ${status}${conclusion}; commit ${run.head_sha || "unknown"}; updated ${run.updated_at || "unknown"}.`,
        );
      }, "Couldn't check the website deployment");
    },
  };

  return { tools, _state: { pending } };
}

module.exports = {
  createSiteEditor,
  redactToken,
  HARD_DENY_PATHS,
};
