import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

import { DEFAULT_MODEL, modelsForByoFlag } from "./models.ts";

test("flag off hides every BYO model while platform models remain", () => {
  const models = modelsForByoFlag(false);

  assert.ok(models.length > 0, "platform model settings must remain populated");
  assert.ok(models.some((model) => model.model_id === DEFAULT_MODEL));
  assert.ok(models.every((model) => !model.requires?.startsWith("byo-")));
  assert.ok(models.every((model) => !model.model_id.startsWith("anthropic/")));
});

test("flag on preserves the parked BYO model behavior", () => {
  const models = modelsForByoFlag(true);

  assert.ok(models.some((model) => model.requires === "byo-anthropic"));
  assert.ok(models.some((model) => model.model_id.startsWith("anthropic/")));
});

test("BYO modals stay flag-gated while provider page, nav, and task settings remain", () => {
  const page = readFileSync(
    new URL("../app/settings/ai-provider/page.tsx", import.meta.url),
    "utf8",
  );
  const layout = readFileSync(new URL("../app/settings/layout.tsx", import.meta.url), "utf8");
  const connectModal = page.indexOf("<ConnectAnthropicModal");
  const modalGate = page.lastIndexOf("{byoEnabled ? (", connectModal);
  const modalGateEnd = page.indexOf(") : null}", connectModal);

  assert.ok(modalGate >= 0 && modalGate < connectModal);
  assert.ok(modalGateEnd > page.indexOf("<DisconnectModal", connectModal));
  assert.match(page, /<SectionCard title="AI Provider"/);
  assert.match(page, /<SectionCard title="Scheduled Task Models"/);
  assert.match(layout, /href: "\/settings\/ai-provider", label: "AI Provider"/);
});
