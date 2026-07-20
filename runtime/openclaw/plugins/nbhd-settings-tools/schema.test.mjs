// Tour-guide registration + response contract tests.
//   node --test runtime/openclaw/plugins/nbhd-settings-tools/schema.test.mjs
import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

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

test("tour-guide and places-search tools share the strict tourGuideEnabled gate", () => {
  for (const pluginConfig of [{}, { tourGuideEnabled: false }, { tourGuideEnabled: "true" }]) {
    assert.equal(collectTools(pluginConfig).nbhd_tour_guide, undefined);
    assert.equal(collectTools(pluginConfig).nbhd_places_search, undefined);
  }
  const tools = collectTools({ tourGuideEnabled: true });
  assert.ok(tools.nbhd_tour_guide);
  assert.ok(tools.nbhd_places_search);
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

test("nbhd_places_search schema bounds every caller-controlled parameter", () => {
  const schema = collectTools({ tourGuideEnabled: true }).nbhd_places_search.parameters;

  assert.deepEqual(schema.required, ["query"]);
  assert.equal(schema.additionalProperties, false);
  assert.equal(schema.properties.query.minLength, 1);
  assert.equal(schema.properties.query.maxLength, 200);
  assert.equal(schema.properties.latitude.minimum, -90);
  assert.equal(schema.properties.latitude.maximum, 90);
  assert.equal(schema.properties.longitude.minimum, -180);
  assert.equal(schema.properties.longitude.maximum, 180);
  assert.equal(schema.properties.language.maxLength, 35);
  assert.equal(schema.properties.categories.maxItems, 10);
  assert.equal(schema.properties.categories.items.maxLength, 64);
  assert.equal(schema.properties.limit.minimum, 1);
  assert.equal(schema.properties.limit.maximum, 20);
});

test("nbhd_places_search sends bounded query and both internal-auth headers", async (t) => {
  const previousTenantId = process.env.NBHD_TENANT_ID;
  const previousInternalKey = process.env.NBHD_INTERNAL_API_KEY;
  process.env.NBHD_TENANT_ID = "tenant-123";
  process.env.NBHD_INTERNAL_API_KEY = "internal-key";
  t.after(() => {
    if (previousTenantId === undefined) delete process.env.NBHD_TENANT_ID;
    else process.env.NBHD_TENANT_ID = previousTenantId;
    if (previousInternalKey === undefined) delete process.env.NBHD_INTERNAL_API_KEY;
    else process.env.NBHD_INTERNAL_API_KEY = previousInternalKey;
  });

  let captured;
  t.mock.method(globalThis, "fetch", async (url, options) => {
    captured = { url: new URL(url), options };
    return {
      ok: true,
      status: 200,
      async text() {
        return JSON.stringify({
          verified: true,
          fresh: true,
          source: "apple_maps",
          results: [
            {
              id: "place-1",
              name: "Kiyomizu-dera",
              latitude: 34.994856,
              longitude: 135.785046,
              formatted_address_lines: ["Kyoto", "Japan"],
              country: "Japan",
              country_code: "JP",
              poi_category: "ReligiousSite",
              bearer: "must-be-stripped",
            },
          ],
          accessToken: "must-be-stripped",
        });
      },
    };
  });
  const tools = collectTools({
    tourGuideEnabled: true,
    apiBaseUrl: "https://nbhd.example",
  });

  const result = await tools.nbhd_places_search.execute("call-2", {
    query: "temple",
    latitude: 35.0,
    longitude: 135.0,
    language: "ja-JP",
    country: "JP",
    categories: ["ReligiousSite", "Landmark"],
    limit: 6,
  });

  assert.equal(
    captured.url.pathname,
    "/api/v1/integrations/runtime/tenant-123/places/search/",
  );
  assert.deepEqual(Object.fromEntries(captured.url.searchParams), {
    q: "temple",
    lat: "35",
    lon: "135",
    lang: "ja-JP",
    country: "JP",
    categories: "ReligiousSite,Landmark",
    limit: "6",
  });
  assert.equal(captured.options.method, "GET");
  assert.equal(captured.options.headers["X-NBHD-Internal-Key"], "internal-key");
  assert.equal(captured.options.headers["X-NBHD-Tenant-Id"], "tenant-123");

  const payload = JSON.parse(result.content[0].text);
  assert.deepEqual(payload, {
    verified: true,
    fresh: true,
    source: "apple_maps",
    results: [
      {
        id: "place-1",
        name: "Kiyomizu-dera",
        latitude: 34.994856,
        longitude: 135.785046,
        formatted_address_lines: ["Kyoto", "Japan"],
        country: "Japan",
        country_code: "JP",
        poi_category: "ReligiousSite",
      },
    ],
  });
  assert.doesNotMatch(JSON.stringify(payload), /accessToken|bearer|internal-key/i);
});

test("nbhd_places_search renders 429/503 degraded bodies as normal token-free results", async (t) => {
  process.env.NBHD_TENANT_ID = "tenant-123";
  process.env.NBHD_INTERNAL_API_KEY = "internal-key";
  let responseStatus = 429;
  t.mock.method(globalThis, "fetch", async () => {
    const status = responseStatus;
    return {
      ok: false,
      status,
      async text() {
        return JSON.stringify({
          verified: false,
          fresh: false,
          source: "apple_maps",
          results: [],
          reason: status === 429 ? "rate_limited" : "upstream_unavailable",
          token: "must-be-stripped",
        });
      },
    };
  });
  const tools = collectTools({ tourGuideEnabled: true, apiBaseUrl: "https://nbhd.example" });

  for (const [status, reason] of [
    [429, "rate_limited"],
    [503, "upstream_unavailable"],
  ]) {
    responseStatus = status;
    const result = await tools.nbhd_places_search.execute(`call-${status}`, {
      query: "coffee",
    });
    const payload = JSON.parse(result.content[0].text);

    assert.deepEqual(payload, {
      verified: false,
      fresh: false,
      source: "apple_maps",
      results: [],
      reason,
    });
    assert.doesNotMatch(JSON.stringify(payload), /token|bearer/i);
  }
});

test("settings-tools manifest declares nbhd_places_search", () => {
  const manifest = JSON.parse(
    readFileSync(new URL("./openclaw.plugin.json", import.meta.url), "utf8"),
  );
  assert.ok(manifest.contracts.tools.includes("nbhd_places_search"));
});
