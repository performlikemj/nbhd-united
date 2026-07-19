// Tour-guide registration + response contract tests.
//   node --test runtime/openclaw/plugins/nbhd-settings-tools/schema.test.mjs
import test from "node:test";
import assert from "node:assert/strict";

import register from "./index.js";

function collectTools(pluginConfig) {
  const tools = {};
  const api = {
    pluginConfig,
    registerTool(def) {
      tools[def.name] = def;
    },
  };
  register(api);
  return tools;
}

test("nbhd_tour_guide registers only when tourGuideEnabled is strictly true", () => {
  for (const pluginConfig of [{}, { tourGuideEnabled: false }, { tourGuideEnabled: "true" }]) {
    assert.equal(collectTools(pluginConfig).nbhd_tour_guide, undefined);
  }
  assert.ok(collectTools({ tourGuideEnabled: true }).nbhd_tour_guide);
});

test("nbhd_tour_guide returns the injected contract first and mode", async () => {
  const contract = "Injected cards contract: NEVER draw a card in text/ASCII.";
  const tools = collectTools({
    tourGuideEnabled: true,
    tourGuideMode: "cards",
    tourGuideContract: contract,
  });

  const result = await tools.nbhd_tour_guide.execute("call-1", {});
  const payload = JSON.parse(result.content[0].text);

  assert.deepEqual(Object.keys(payload), ["tour_guide_contract", "mode"]);
  assert.equal(payload.tour_guide_contract, contract);
  assert.equal(payload.mode, "cards");
  assert.deepEqual(result.details.json, payload);
});
