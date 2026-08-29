"use strict";

const { wrapTool } = require("../../tool-logger.js");
const { createSiteEditor } = require("./lib.js");

const wrap = (definition) => wrapTool(definition, { plugin: "nbhd-site-editor" });

module.exports = function register(api) {
  const editor = createSiteEditor({
    config: api.pluginConfig,
    env: process.env,
    fetchImpl: globalThis.fetch,
    now: () => new Date(),
  });

  api.registerTool(wrap({
    name: "site_list_files",
    description:
      "List editable website files before choosing what to read. Use this only to navigate the configured site repository; then read the current file before staging an edit.",
    parameters: {
      type: "object",
      additionalProperties: false,
      properties: {
        path: { type: "string", description: "Editable repository-relative folder; omit for the repository root." },
      },
    },
    async execute(_toolCallId, params = {}) {
      return editor.tools.site_list_files(params);
    },
  }));

  api.registerTool(wrap({
    name: "site_read_file",
    description:
      "Read the current editable website file before changing it. Always call this before site_stage_file so edits are based on the current branch.",
    parameters: {
      type: "object",
      additionalProperties: false,
      required: ["path"],
      properties: {
        path: { type: "string", description: "Editable repository-relative text file." },
      },
    },
    async execute(_toolCallId, params = {}) {
      return editor.tools.site_read_file(params);
    },
  }));

  api.registerTool(wrap({
    name: "site_stage_file",
    description:
      "Stage a complete replacement for a text file only after reading its current content. After staging all edits, call site_show_pending and wait for the user's explicit go before publishing.",
    parameters: {
      type: "object",
      additionalProperties: false,
      required: ["path", "content"],
      properties: {
        path: { type: "string", description: "Editable repository-relative text file." },
        content: { type: "string", description: "Complete new UTF-8 file content." },
      },
    },
    async execute(_toolCallId, params = {}) {
      return editor.tools.site_stage_file(params);
    },
  }));

  api.registerTool(wrap({
    name: "site_stage_upload",
    description:
      "Stage the image file the user just supplied at an editable website path. Then call site_show_pending and wait for the user's explicit go before publishing.",
    parameters: {
      type: "object",
      additionalProperties: false,
      required: ["path", "local_path"],
      properties: {
        path: { type: "string", description: "Editable repository-relative destination image path." },
        local_path: { type: "string", description: "Local path to the image file the user supplied." },
      },
    },
    async execute(_toolCallId, params = {}) {
      return editor.tools.site_stage_upload(params);
    },
  }));

  api.registerTool(wrap({
    name: "site_show_pending",
    description:
      "Show every staged website change to the user as text diffs or binary sizes. Call this before asking for an explicit go; do not publish yet.",
    parameters: { type: "object", additionalProperties: false, properties: {} },
    async execute(_toolCallId, params = {}) {
      return editor.tools.site_show_pending(params);
    },
  }));

  api.registerTool(wrap({
    name: "site_discard",
    description:
      "Discard one staged website change, or all staged changes when path is omitted. Use this when the user rejects or revises the shown changes.",
    parameters: {
      type: "object",
      additionalProperties: false,
      properties: {
        path: { type: "string", description: "Optional staged repository-relative path; omit to discard all." },
      },
    },
    async execute(_toolCallId, params = {}) {
      return editor.tools.site_discard(params);
    },
  }));

  api.registerTool(wrap({
    name: "site_publish",
    description:
      "Publish all shown, staged website changes in one commit only after the user explicitly says go. Pass confirm=true and a short message. Never claim the site is live unless this tool returns a commit this turn; say deployment takes a few minutes.",
    parameters: {
      type: "object",
      additionalProperties: false,
      required: ["message", "confirm"],
      properties: {
        message: { type: "string", maxLength: 200, description: "Single-line commit message." },
        confirm: { type: "boolean", description: "Must be true only after the user's explicit go." },
      },
    },
    async execute(_toolCallId, params = {}) {
      return editor.tools.site_publish(params);
    },
  }));

  api.registerTool(wrap({
    name: "site_deploy_status",
    description:
      "Check the latest deployment run after site_publish returned a commit. Report its actual status; never guess that a change is live.",
    parameters: { type: "object", additionalProperties: false, properties: {} },
    async execute(_toolCallId, params = {}) {
      return editor.tools.site_deploy_status(params);
    },
  }));
};
