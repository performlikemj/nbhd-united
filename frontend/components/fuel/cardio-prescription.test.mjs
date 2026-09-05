import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import { fileURLToPath } from "node:url";
import ts from "typescript";
import React from "react";
import { renderToStaticMarkup } from "react-dom/server";

// Exercise the actual TSX renderer and conversion helpers with a fixed profile;
// no browser, network, or additional frontend test-runner dependency is needed.
const require = createRequire(import.meta.url);
function load(file) {
  const filename = fileURLToPath(new URL(file, import.meta.url));
  const js = ts.transpileModule(readFileSync(filename, "utf8"), {
    compilerOptions: { module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX },
  }).outputText;
  const loadedModule = { exports: {} };
  const localRequire = (name) => {
    if (name === "@/lib/queries") return {
      useFuelProfileQuery: () => ({ data: { distance_unit: "km", weight_unit: "kg" } }),
      useUpdateFuelProfileMutation: () => ({ mutate() {}, isPending: false }),
    };
    if (name === "@/components/status-pill") return { StatusPill: ({ status }) => React.createElement("span", null, status) };
    if (name.startsWith("./use-")) return load(`${name}.ts`);
    return require(name);
  };
  new Function("require", "module", "exports", js)(localRequire, loadedModule, loadedModule.exports);
  return loadedModule.exports;
}
const { CardioPrescriptionReadOnly, WorkoutDetailReadOnly } = load("./workout-detail-readonly.tsx");
const render = (props) => renderToStaticMarkup(React.createElement(CardioPrescriptionReadOnly, props));

test("malformed stored segments cannot crash or render objects/nonfinite numbers", () => {
  const detail = { segments: [null, [], { kind: {}, effort: {}, target_pace: 10, duration_s: Infinity },
    { kind: "interval", repeat: NaN, distance_km: 0.8, target_pace: {}, recovery: { effort: {}, duration_s: NaN } }],
    planned: { duration_s: Infinity, distance_km: NaN } };
  const html = render({ detail, completed: true, actualDurationSeconds: Infinity });
  assert.match(html, /Segment/);
  assert.match(html, /0.8 km/);
  assert.doesNotMatch(html, /NaN|Infinity|\[object Object\]|Actual/);
});

test("prescribed distance becomes Actual only on completed workouts", () => {
  const detail = { segments: [{ kind: "steady", duration_s: 600, effort: "easy", target_pace: "6:00" }],
    planned: { duration_s: 600 }, distance_km: 2 };
  assert.doesNotMatch(render({ detail, completed: false, actualDurationSeconds: 500 }), /Actual/);
  assert.match(render({ detail, completed: true, actualDurationSeconds: 500 }), /Actual 8:20 · 2 km/);
});

test("empty stats stay hidden and prescription-only detail is not empty", () => {
  const preview = (detail) => renderToStaticMarkup(React.createElement(WorkoutDetailReadOnly, { detail, category: "cardio" }));
  assert.doesNotMatch(preview({ terrain: "track" }), /STATS|No exercise details/);
  assert.match(preview({}), /No exercise details/i);
  const stats = preview({ distance_km: 2 });
  assert.match(stats, /DISTANCE/);
  assert.doesNotMatch(stats, /HEART RATE|POWER/);
});
