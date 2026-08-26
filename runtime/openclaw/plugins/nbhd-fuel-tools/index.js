import { wrapTool } from "../../tool-logger.js";
const wrap = (def) => wrapTool(def, { plugin: "nbhd-fuel-tools" });

/**
 * NBHD Fuel Tools Plugin
 *
 * Workout tracking, body weight logging, and fitness profile management:
 * - Log workouts from natural language (infer category, default today)
 * - Get summary context (recent workouts, planned, body weight, profile)
 * - Log body weight
 * - Update fitness profile progressively during onboarding
 */

const DEFAULT_REQUEST_TIMEOUT_MS = 20000;

function asObject(value) {
  return value && typeof value === "object" && !Array.isArray(value) ? value : {};
}

function asTrimmedString(value) {
  return typeof value === "string" ? value.trim() : "";
}

function parseInteger(value, { defaultValue, min, max }) {
  if (value === undefined || value === null || value === "") return defaultValue;
  const parsed = Number.parseInt(String(value), 10);
  if (Number.isNaN(parsed)) return defaultValue;
  return Math.max(min, Math.min(max, parsed));
}

function getRuntimeConfig(api) {
  const pluginConfig = asObject(api.pluginConfig);
  const apiBaseUrl = asTrimmedString(
    pluginConfig.apiBaseUrl || process.env.NBHD_API_BASE_URL,
  ).replace(/\/+$/, "");
  const tenantId = asTrimmedString(process.env.NBHD_TENANT_ID);
  const internalKey = asTrimmedString(process.env.NBHD_INTERNAL_API_KEY);
  const requestTimeoutMs = parseInteger(pluginConfig.requestTimeoutMs, {
    defaultValue: DEFAULT_REQUEST_TIMEOUT_MS,
    min: 1000,
    max: 60000,
  });

  if (!apiBaseUrl) throw new Error("NBHD_API_BASE_URL is required");
  if (!tenantId) throw new Error("NBHD_TENANT_ID is required");
  if (!internalKey) throw new Error("NBHD_INTERNAL_API_KEY is required");

  return { apiBaseUrl, tenantId, internalKey, requestTimeoutMs };
}

function buildUrl(baseUrl, path, query) {
  const url = new URL(`${baseUrl}${path}`);
  for (const [key, value] of Object.entries(query || {})) {
    if (value === undefined || value === null || value === "") continue;
    url.searchParams.set(key, String(value));
  }
  return url;
}

function renderPayload(payload) {
  return {
    content: [{ type: "text", text: JSON.stringify(payload, null, 2) }],
    details: { json: payload },
  };
}

function renderTextPayload(payload, text) {
  return {
    content: [{ type: "text", text }],
    details: { json: payload },
  };
}

function renderWritePayload(payload) {
  const unmatched = Array.isArray(payload?.unmatched_exercises)
    ? payload.unmatched_exercises.map(String).filter(Boolean)
    : [];
  if (unmatched.length === 0) return renderPayload(payload);
  const warning = `No figure for: ${unmatched.join(", ")} — for movements you chose, use exact catalog names (nbhd_fuel_search_exercises); never swap a user-requested movement without asking`;
  return renderTextPayload(payload, `${JSON.stringify(payload, null, 2)}\n${warning}`);
}

const PRESCRIPTION_LEGEND =
  "prescription legend: yes (has_prescription true) = filled · no (false) = needs filling · rest (null) = skip";

function prescriptionLabel(workout) {
  if (workout?.status === "rest" || workout?.has_prescription === null) return "rest";
  return workout?.has_prescription ? "yes" : "no";
}

function renderWorkoutLine(workout) {
  return `${workout?.date || ""} · ${workout?.activity || "Workout"} · ${workout?.status || "unknown"} · prescription ${prescriptionLabel(workout)}`;
}

function renderAudit(payload) {
  const workouts = Array.isArray(payload?.next_14d_workouts) ? payload.next_14d_workouts : [];
  const lines = [PRESCRIPTION_LEGEND, "next_14d_workouts:"];
  if (workouts.length === 0) lines.push("(none)");
  else lines.push(...workouts.map(renderWorkoutLine));
  lines.push("", JSON.stringify(payload, null, 2));
  return renderTextPayload(payload, lines.join("\n"));
}

function renderExerciseSearch(payload) {
  const rows = Array.isArray(payload?.results) ? payload.results : [];
  const lines = rows.map((row) => {
    const stretch = row?.stretch ? " · stretch" : "";
    return `${row?.name || ""} — ${row?.muscle || ""} · ${row?.equipment || ""}${stretch}`;
  });
  lines.push(`total: ${Number.isFinite(payload?.total) ? payload.total : 0}`);
  if (Array.isArray(payload?.muscles)) lines.push(`muscles: ${payload.muscles.join(", ")}`);
  if (Array.isArray(payload?.equipment_types)) {
    lines.push(`equipment: ${payload.equipment_types.join(", ")}`);
  }
  if (payload?.guidance) lines.push(String(payload.guidance));
  return renderTextPayload(payload, lines.join("\n"));
}

function renderPlan(payload) {
  const workouts = Array.isArray(payload?.workouts) ? payload.workouts : [];
  const start = new Date(`${payload?.start_date || ""}T00:00:00Z`);
  const lines = [
    `${payload?.name || "Plan"} · ${payload?.status || "unknown"}`,
    PRESCRIPTION_LEGEND,
  ];
  let currentWeek = null;
  for (const workout of workouts) {
    const workoutDate = new Date(`${workout?.date || ""}T00:00:00Z`);
    const week = Number.isNaN(start.valueOf()) || Number.isNaN(workoutDate.valueOf())
      ? "?"
      : Math.floor((workoutDate - start) / 604800000) + 1;
    if (week !== currentWeek) {
      currentWeek = week;
      lines.push(`Week ${week}`);
    }
    lines.push(renderWorkoutLine(workout));
  }
  return renderTextPayload(payload, lines.join("\n"));
}

const TOOL_ERROR_DETAIL_MAX_CHARS = 2000;

function clampErrorDetail(text) {
  if (text.length <= TOOL_ERROR_DETAIL_MAX_CHARS) return text;
  return `${text.slice(0, TOOL_ERROR_DETAIL_MAX_CHARS)}… [truncated]`;
}

function compactErrorDetail(payload) {
  const normalized = asObject(payload);
  const entries = Object.entries(normalized).filter(([key]) => key !== "error");
  if (entries.length === 0) return "";

  const detail = normalized.detail;
  const detailIsOnlyKey = entries.length === 1 && detail !== undefined;
  if (detailIsOnlyKey && typeof detail === "string") {
    return detail.trim() ? clampErrorDetail(detail.trim()) : "";
  }

  const value = detailIsOnlyKey ? detail : Object.fromEntries(entries);
  if (value === null || (typeof value === "object" && Object.keys(value).length === 0)) return "";

  try {
    return clampErrorDetail(JSON.stringify(value));
  } catch {
    return clampErrorDetail(String(value));
  }
}

async function callRuntime(api, { path, method = "GET", query, body }) {
  const runtime = getRuntimeConfig(api);
  const url = buildUrl(runtime.apiBaseUrl, path, query);
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), runtime.requestTimeoutMs);

  try {
    const headers = {
      "X-NBHD-Internal-Key": runtime.internalKey,
      "X-NBHD-Tenant-Id": runtime.tenantId,
    };
    let requestBody;
    if (method !== "GET" && body !== undefined) {
      headers["Content-Type"] = "application/json";
      requestBody = JSON.stringify(body);
    }

    const response = await fetch(url, {
      method,
      headers,
      body: requestBody,
      signal: controller.signal,
    });

    const raw = await response.text();
    let payload = {};
    if (raw) {
      try {
        payload = JSON.parse(raw);
      } catch {
        payload = { detail: "upstream returned a non-JSON response body" };
      }
    }

    if (!response.ok) {
      const normalized = asObject(payload);
      const code = asTrimmedString(normalized.error) || "runtime_request_failed";
      // DRF commonly returns field errors at the top level, e.g.
      // {week_rating: ["..."]}, rather than under `detail`. Preserve that
      // compact validation payload so the model can correct and retry.
      const detail = compactErrorDetail(normalized);
      const detailSuffix = detail ? ` (${detail})` : "";
      throw new Error(`NBHD runtime error ${response.status}: ${code}${detailSuffix}`);
    }

    return asObject(payload);
  } catch (error) {
    if (error && error.name === "AbortError") {
      throw new Error(`NBHD runtime request timed out after ${runtime.requestTimeoutMs}ms`);
    }
    throw error;
  } finally {
    clearTimeout(timeout);
  }
}

function fuelPath(api, suffix) {
  const runtime = getRuntimeConfig(api);
  return `/api/v1/fuel/runtime/${encodeURIComponent(runtime.tenantId)}${suffix}`;
}

export default function register(api) {
  // ── Fuel Audit ──────────────────────────────────────────────────────
  // Single tool for any "what should I do for a workout" / "deliver today's
  // plan" / "schedule a workout" question. Cross-references three sources:
  //   1. Today's daily-note Fuel section (the locked plan, if any).
  //   2. Postgres Workout rows for the next 14 days.
  //   3. The container's cron registry (_fuel:* + user-named workout crons).
  // Returns conflicts (duplicate fires, orphan crons, orphan workouts) plus
  // a one-line `guidance` string that tells the agent what to do.
  api.registerTool(wrap({
      name: "nbhd_fuel_audit",
      description:
        "PREFER this tool over nbhd_fuel_summary when the user asks for a workout, asks what's planned, wants to schedule one, or signals they're training right now (e.g. \"I'm at the gym\", \"about to lift\", \"between sets\") and the always-loaded Fuel state in USER.md doesn't already answer it. Returns: (a) today_plan for today's session — today_plan.raw_section is the daily-note Fuel section if a prep cron wrote one (today_plan.exists), and today_plan.workouts always lists today's scheduled rows from Postgres. Deliver today's session (raw_section if present, else workouts) rather than inventing a new one; only propose a fresh workout if BOTH are empty; (b) next_14d_workouts from Postgres; (c) fuel_crons currently in the container; (d) conflicts.duplicate_fires / orphan_crons / orphan_workouts; (e) a one-line `guidance` string. If conflicts.duplicate_fires is non-empty, surface them and STOP — do not add another cron on top.",
      parameters: {
        type: "object",
        additionalProperties: false,
        properties: {},
      },
      async execute() {
        try {
          const payload = await callRuntime(api, {
            path: fuelPath(api, "/audit/"),
            method: "GET",
          });
          return renderAudit(payload);
        } catch (error) {
          return renderPayload({ error: error.message });
        }
      },
    }),
    { optional: true },
  );

  // ── Fuel Summary ────────────────────────────────────────────────────
  api.registerTool(wrap({
      name: "nbhd_fuel_summary",
      description:
        "Get the user's fitness context: recent/planned workouts, body weight, profile, trends, and all-time PRs. PR rows include metric and a server-authored display. est_1rm is an estimate derived from a rep set, never weight actually lifted: use display, say estimated 1RM, and congratulate the actual weight × reps source set. Call this at the start of fitness conversations. NOTE: for any question about *today's* workout or scheduling, prefer `nbhd_fuel_audit` — it includes cron state and conflict detection.",
      parameters: {
        type: "object",
        additionalProperties: false,
        properties: {},
      },
      async execute() {
        try {
          const payload = await callRuntime(api, {
            path: fuelPath(api, "/summary/"),
            method: "GET",
          });
          return renderPayload(payload);
        } catch (error) {
          return renderPayload({ error: error.message });
        }
      },
    }),
    { optional: true },
  );

  // ── Illustrated Exercise Catalog ───────────────────────────────────
  api.registerTool(wrap({
      name: "nbhd_fuel_search_exercises",
      description:
        "Search the catalog of 302 illustrated exercises; use the returned name verbatim so the app shows the figure. Call nbhd_fuel_search_exercises when choosing accessories or mobility movements, or when the user asks what else could I do for <muscle>? Filters are exact. Muscles: Adductors, Back, Biceps, Calves, Chest, Core, Forearms, Glutes, Hamstrings, Hips, Lats, Legs, Lower Back, Mobility, Posterior Chain, Quads, Rear Delts, Shoulders, Triceps, Upper Back. Equipment: Barbell, Bench, Bodyweight, Box, Cable, Cardio, Chair, Doorway, Dumbbell, Kettlebell, Machine, Plate, Pull-up Bar, Resistance Band, Stability Ball, Towel, Wall.",
      parameters: {
        type: "object",
        additionalProperties: false,
        properties: {
          query: { type: "string", description: "Name, alias, or muscle text to search." },
          muscle: { type: "string", description: "Exact muscle filter from this tool's description." },
          equipment: { type: "string", description: "Exact equipment filter from this tool's description." },
          limit: { type: "integer", minimum: 1, maximum: 100, description: "Maximum rows (1-100)." },
        },
      },
      async execute(_id, params) {
        try {
          const input = asObject(params);
          const query = {};
          if (input.query) query.q = asTrimmedString(input.query);
          if (input.muscle) query.muscle = asTrimmedString(input.muscle);
          if (input.equipment) query.equipment = asTrimmedString(input.equipment);
          if (input.limit !== undefined) {
            query.limit = parseInteger(input.limit, { defaultValue: 50, min: 1, max: 100 });
          }
          const payload = await callRuntime(api, {
            path: fuelPath(api, "/exercises/"),
            method: "GET",
            query,
          });
          return renderExerciseSearch(payload);
        } catch (error) {
          return renderPayload({ error: error.message });
        }
      },
    }),
    { optional: true },
  );

  // ── Full Workout Plan ──────────────────────────────────────────────
  api.registerTool(wrap({
      name: "nbhd_fuel_get_plan",
      description:
        "Get one full plan with every workout row and has_prescription. Use it to fill in every empty session of a plan.",
      parameters: {
        type: "object",
        additionalProperties: false,
        properties: {
          plan_id: { type: "string", description: "UUID of the plan from nbhd_fuel_summary." },
        },
        required: ["plan_id"],
      },
      async execute(_id, params) {
        try {
          const planId = asTrimmedString(asObject(params).plan_id);
          if (!planId) throw new Error("plan_id is required");
          const payload = await callRuntime(api, {
            path: fuelPath(api, `/plans/${encodeURIComponent(planId)}/`),
            method: "GET",
          });
          return renderPlan(payload);
        } catch (error) {
          return renderPayload({ error: error.message });
        }
      },
    }),
    { optional: true },
  );

  // ── Get Workout ────────────────────────────────────────────────────
  api.registerTool(wrap({
      name: "nbhd_fuel_get_workout",
      description:
        "Retrieve the full details of a single workout, including exercises, sets, reps, and metrics. Use a workout_id returned by nbhd_fuel_summary or nbhd_fuel_audit when the user asks what they did in that specific workout.",
      parameters: {
        type: "object",
        additionalProperties: false,
        properties: {
          workout_id: {
            type: "string",
            description: "UUID of the workout to retrieve (from nbhd_fuel_summary or nbhd_fuel_audit).",
          },
        },
        required: ["workout_id"],
      },
      async execute(_id, params) {
        try {
          const input = asObject(params);
          const workoutId = asTrimmedString(input.workout_id);
          if (!workoutId) throw new Error("workout_id is required");

          const payload = await callRuntime(api, {
            path: fuelPath(api, `/workouts/${encodeURIComponent(workoutId)}/`),
            method: "GET",
          });
          return renderPayload(payload);
        } catch (error) {
          return renderPayload({ error: error.message });
        }
      },
    }),
    { optional: true },
  );

  // ── Log Workout ─────────────────────────────────────────────────────
  api.registerTool(wrap({
      name: "nbhd_fuel_log_workout",
      description:
        'Log a workout from natural language. Infer the category from the activity name (e.g. "deadlift" → strength, "ran" → cardio, "yoga" → mobility). Default to today\'s date and status "done". Confirm briefly and do not interrogate the user. TWO hard server rules: (1) a strength or calisthenics workout MUST carry at least one exercise with sets in detail_json.exercises — an empty list is a 400, because a workout with no exercises is invisible in the app; if the user gave you no detail, ask once for the lifts, or log it under the category that matches what they actually described. (2) numeric detail_json fields are numbers only — distance_km is kilometres and work_s/rest_s are seconds, so convert first and send 8.05, never "5 miles".',
      parameters: {
        type: "object",
        additionalProperties: false,
        properties: {
          activity: {
            type: "string",
            description:
              'The exercise or workout name, e.g. "Deadlift", "5K run", "Yoga flow", "Push — Chest & Shoulders".',
          },
          category: {
            type: "string",
            enum: ["strength", "cardio", "hiit", "calisthenics", "mobility", "sport", "other"],
            description: "Workout category. Infer from the activity name when possible.",
          },
          date: {
            type: "string",
            description: "Date in YYYY-MM-DD format. Defaults to today.",
          },
          status: {
            type: "string",
            enum: ["done", "planned", "skipped", "rescheduled", "in_progress", "rest"],
            description:
              'Whether the workout is completed or planned. Defaults to "done". Use "skipped" when the user says they MISSED or skipped a session — it keeps the session in their adherence history. Never log a missed session as "done": a status outside this list is now a 400 rather than being quietly rewritten to "done".',
          },
          duration_minutes: {
            type: "integer",
            description: "Duration in minutes.",
          },
          rpe: {
            type: "integer",
            minimum: 1,
            maximum: 10,
            description: "Rate of perceived exertion (1-10). Only include if the user mentions it.",
          },
          notes: {
            type: "string",
            description: "Optional notes about the workout.",
          },
          detail_json: {
            type: "object",
            description:
              "Category-specific structured data. Shape depends on category.",
            properties: {
              exercises: {
                type: "array",
                description:
                  "For strength AND calisthenics — both use this array (there is no separate 'skills' array). Each exercise has a name and sets; every set MUST declare its `type`.",
                items: {
                  type: "object",
                  properties: {
                    name: {
                      type: "string",
                      description: "Exercise name, e.g. 'Bench Press', 'Pull-up', 'Plank'.",
                    },
                    sets: {
                      type: "array",
                      items: {
                        type: "object",
                        required: ["type"],
                        properties: {
                          type: {
                            type: "string",
                            enum: ["weighted_reps", "bodyweight_reps", "hold_time"],
                            description:
                              "REQUIRED. The set's metric kind. 'weighted_reps' → also send reps + weight (barbell/dumbbell/machine lifts). 'bodyweight_reps' → also send reps (push-ups, pull-ups, air squats). 'hold_time' → also send hold_s (planks, L-sits, dead hangs). Choose by the movement itself, not by whatever numbers the user happened to mention.",
                          },
                          reps: {
                            type: "integer",
                            description:
                              "Reps performed (integer, e.g. 8). Send for weighted_reps and bodyweight_reps; omit for hold_time.",
                          },
                          weight: {
                            type: "number",
                            description:
                              "Weight in kg (e.g. 75). Send for weighted_reps only. Bodyweight is NOT weight 0 — use type 'bodyweight_reps'.",
                          },
                          hold_s: {
                            type: "integer",
                            description:
                              "Hold duration in seconds. Send for hold_time only; omit otherwise.",
                          },
                        },
                      },
                    },
                  },
                },
              },
              distance_km: {
                type: "number",
                description: "Distance in km (for cardio).",
              },
              pace: {
                type: "string",
                description: "Pace as min:sec per km, e.g. '5:30' (for cardio).",
              },
              avg_hr: {
                type: "integer",
                description: "Average heart rate in bpm.",
              },
              elevation: {
                type: "integer",
                description: "Elevation gain in meters.",
              },
              rounds: {
                type: "integer",
                description: "Number of rounds (for HIIT).",
              },
              work_s: {
                type: "integer",
                description: "Work interval in seconds (for HIIT).",
              },
              rest_s: {
                type: "integer",
                description: "Rest interval in seconds (for HIIT).",
              },
              peak_hr: {
                type: "integer",
                description: "Peak heart rate in bpm (for HIIT).",
              },
              calories: {
                type: "integer",
                description: "Calories burned.",
              },
              blocks: {
                type: "array",
                items: { type: "string" },
                description:
                  "Movement blocks for mobility, e.g. ['Hip 90/90', 'Cat-cow'].",
              },
            },
          },
        },
        required: ["activity"],
      },
      async execute(_id, params) {
        try {
          const input = asObject(params);
          const body = {
            activity: asTrimmedString(input.activity),
          };
          if (input.category) body.category = asTrimmedString(input.category);
          if (input.date) body.date = asTrimmedString(input.date);
          if (input.status) body.status = asTrimmedString(input.status);
          if (input.duration_minutes !== undefined)
            body.duration_minutes = parseInteger(input.duration_minutes, { defaultValue: undefined, min: 1, max: 1440 });
          if (input.rpe !== undefined)
            body.rpe = parseInteger(input.rpe, { defaultValue: undefined, min: 1, max: 10 });
          if (input.notes) body.notes = asTrimmedString(input.notes);
          if (input.detail_json) body.detail_json = input.detail_json;

          const payload = await callRuntime(api, {
            path: fuelPath(api, "/log/"),
            method: "POST",
            body,
          });
          return renderWritePayload(payload);
        } catch (error) {
          return renderPayload({ error: error.message });
        }
      },
    }),
    { optional: true },
  );

  // ── Update Workout ───────────────────────────────────────────────────
  api.registerTool(wrap({
      name: "nbhd_fuel_update_workout",
      description:
        'Update an existing workout. Use when the user wants to correct a logged workout — wrong date, wrong exercise, change status from planned to done, adjust rpe, etc. Get the workout_id from nbhd_fuel_summary or from the response when logging a workout. Only send the fields that need changing.',
      parameters: {
        type: "object",
        additionalProperties: false,
        properties: {
          workout_id: {
            type: "string",
            description: "UUID of the workout to update (from summary or log response).",
          },
          activity: { type: "string", description: "New activity name." },
          category: {
            type: "string",
            enum: ["strength", "cardio", "hiit", "calisthenics", "mobility", "sport", "other"],
          },
          status: {
            type: "string",
            enum: ["done", "planned", "skipped", "rescheduled", "in_progress", "rest"],
            description:
              'Change status, e.g. mark a planned workout as "done", or "skipped" when the user says they missed it. A value outside this list is a 400 — it is no longer silently ignored.',
          },
          date: { type: "string", description: "New date in YYYY-MM-DD format." },
          duration_minutes: { type: "integer", description: "Updated duration in minutes." },
          rpe: { type: "integer", minimum: 1, maximum: 10, description: "Updated RPE." },
          notes: { type: "string", description: "Updated notes." },
          detail_json: {
            type: "object",
            description:
              'Updated category-specific structured data. For strength/calisthenics, every set in exercises[] must include its `type` (weighted_reps | bodyweight_reps | hold_time), same contract as nbhd_fuel_log_workout. For cardio, populate at least one of {distance_km, pace ("M:SS"), avg_hr, elevation, avg_power} — e.g. {"distance_km": 5, "pace": "5:30"}. For HIIT, set {rounds, work_s, rest_s} — e.g. {"rounds": 8, "work_s": 30, "rest_s": 30}. Mobility uses catalog-named skills with hold_time sets, e.g. {"skills":[{"name":"Kneeling Hip Flexor Stretch","sets":[{"type":"hold_time","hold_s":45}]}]}; blocks only for non-movement work such as breathing or foam rolling. Use this to fill in target prescriptions on planned workouts — do not leave a planned workout\'s detail_json empty.',
          },
        },
        required: ["workout_id"],
      },
      async execute(_id, params) {
        try {
          const input = asObject(params);
          const workoutId = asTrimmedString(input.workout_id);
          if (!workoutId) throw new Error("workout_id is required");

          const body = {};
          if (input.activity) body.activity = asTrimmedString(input.activity);
          if (input.category) body.category = asTrimmedString(input.category);
          if (input.status) body.status = asTrimmedString(input.status);
          if (input.date) body.date = asTrimmedString(input.date);
          if (input.duration_minutes !== undefined)
            body.duration_minutes = parseInteger(input.duration_minutes, { defaultValue: undefined, min: 1, max: 1440 });
          if (input.rpe !== undefined)
            body.rpe = parseInteger(input.rpe, { defaultValue: undefined, min: 1, max: 10 });
          if (input.notes !== undefined) body.notes = asTrimmedString(input.notes);
          if (input.detail_json) body.detail_json = input.detail_json;

          const payload = await callRuntime(api, {
            path: fuelPath(api, `/workouts/${encodeURIComponent(workoutId)}/`),
            method: "PATCH",
            body,
          });
          return renderWritePayload(payload);
        } catch (error) {
          return renderPayload({ error: error.message });
        }
      },
    }),
    { optional: true },
  );

  // ── Delete Workout ──────────────────────────────────────────────────
  api.registerTool(wrap({
      name: "nbhd_fuel_delete_workout",
      description:
        "Delete a workout. Use when the user wants to remove a logged workout entirely — duplicates, mistakes, or workouts they don't want tracked. Confirm with the user before deleting. Get the workout_id from nbhd_fuel_summary.",
      parameters: {
        type: "object",
        additionalProperties: false,
        properties: {
          workout_id: {
            type: "string",
            description: "UUID of the workout to delete.",
          },
        },
        required: ["workout_id"],
      },
      async execute(_id, params) {
        try {
          const input = asObject(params);
          const workoutId = asTrimmedString(input.workout_id);
          if (!workoutId) throw new Error("workout_id is required");

          const payload = await callRuntime(api, {
            path: fuelPath(api, `/workouts/${encodeURIComponent(workoutId)}/`),
            method: "DELETE",
          });
          return renderPayload(payload);
        } catch (error) {
          return renderPayload({ error: error.message });
        }
      },
    }),
    { optional: true },
  );

  // ── Log Body Weight ─────────────────────────────────────────────────
  api.registerTool(wrap({
      name: "nbhd_fuel_log_body_weight",
      description:
        "Log the user's body weight. Upserts by date — if an entry already exists for that date, it's updated.",
      parameters: {
        type: "object",
        additionalProperties: false,
        properties: {
          weight_kg: {
            type: "number",
            description: "Body weight in kilograms.",
          },
          date: {
            type: "string",
            description: "Date in YYYY-MM-DD format. Defaults to today.",
          },
        },
        required: ["weight_kg"],
      },
      async execute(_id, params) {
        try {
          const input = asObject(params);
          const body = {
            weight_kg: input.weight_kg,
          };
          if (input.date) body.date = asTrimmedString(input.date);

          const payload = await callRuntime(api, {
            path: fuelPath(api, "/body-weight/"),
          method: "POST",
          body,
        });
        return renderPayload(payload);
        } catch (error) {
          return renderPayload({ error: error.message });
        }
      },
    }),
    { optional: true },
  );

  // ── Delete Body Weight ──────────────────────────────────────────────
  api.registerTool(wrap({
      name: "nbhd_fuel_delete_body_weight",
      description:
        "Delete a body weight entry by date. Use when the user wants to remove a logged weight — typos, mistakes, or duplicates. Confirm with the user before deleting.",
      parameters: {
        type: "object",
        additionalProperties: false,
        properties: {
          date: {
            type: "string",
            description: "Date of the weight entry to delete, in YYYY-MM-DD format.",
          },
        },
        required: ["date"],
      },
      async execute(_id, params) {
        try {
          const input = asObject(params);
          const dateStr = asTrimmedString(input.date);
          if (!dateStr) throw new Error("date is required");

          const payload = await callRuntime(api, {
            path: fuelPath(api, "/body-weight/"),
            method: "DELETE",
            query: { date: dateStr },
          });
          return renderPayload(payload);
        } catch (error) {
          return renderPayload({ error: error.message });
        }
      },
    }),
    { optional: true },
  );

  // ── Log Sleep ───────────────────────────────────────────────────────
  api.registerTool(wrap({
      name: "nbhd_fuel_log_sleep",
      description:
        "Log the user's sleep duration. Upserts by date. Include quality (1-5) if the user mentions how they slept.",
      parameters: {
        type: "object",
        additionalProperties: false,
        properties: {
          duration_hours: {
            type: "number",
            description: "Sleep duration in hours, e.g. 7.5 for 7 hours 30 minutes.",
          },
          quality: {
            type: "integer",
            minimum: 1,
            maximum: 5,
            description: "Sleep quality 1-5. Only include if the user mentions it.",
          },
          notes: {
            type: "string",
            description: "Optional notes, e.g. 'woke up twice', 'slept great'.",
          },
          date: {
            type: "string",
            description: "Date in YYYY-MM-DD format. Defaults to today (last night's sleep).",
          },
        },
        required: ["duration_hours"],
      },
      async execute(_id, params) {
        try {
          const input = asObject(params);
          const body = {
            duration_hours: input.duration_hours,
          };
          if (input.quality !== undefined)
            body.quality = parseInteger(input.quality, { defaultValue: undefined, min: 1, max: 5 });
          if (input.notes) body.notes = asTrimmedString(input.notes);
          if (input.date) body.date = asTrimmedString(input.date);

          const payload = await callRuntime(api, {
            path: fuelPath(api, "/sleep/"),
            method: "POST",
            body,
          });
          return renderPayload(payload);
        } catch (error) {
          return renderPayload({ error: error.message });
        }
      },
    }),
    { optional: true },
  );

  // ── Update Fitness Profile ──────────────────────────────────────────
  api.registerTool(wrap({
      name: "nbhd_fuel_update_profile",
      description:
        "Update the user's fitness profile progressively. Call with any subset of fields as you learn them during onboarding conversation. List fields (goals, limitations, equipment) replace the full list each call — send the complete current list, not just additions. Set onboarding_status to 'in_progress' when starting, 'completed' when done, or 'declined' if the user opts out.",
      parameters: {
        type: "object",
        additionalProperties: false,
        properties: {
          onboarding_status: {
            type: "string",
            enum: ["pending", "in_progress", "completed", "declined"],
            description: "Current onboarding state.",
          },
          fitness_level: {
            type: "string",
            enum: ["beginner", "intermediate", "advanced"],
            description: "User's self-assessed fitness level.",
          },
          goals: {
            type: "array",
            items: { type: "string" },
            description:
              "Fitness goals: strength, weight_loss, muscle_gain, endurance, flexibility, general_health, sport_specific.",
          },
          limitations: {
            type: "array",
            items: { type: "string" },
            description:
              'Injuries, conditions, or constraints. Be specific: "right shoulder — rotator cuff tear 2024", not just "shoulder".',
          },
          equipment: {
            type: "array",
            items: { type: "string" },
            description:
              "Available equipment: barbell, dumbbells, kettlebells, pull_up_bar, resistance_bands, machines, bodyweight_only, full_gym.",
          },
          days_per_week: {
            type: "integer",
            minimum: 1,
            maximum: 7,
            description: "How many days per week the user wants to train.",
          },
          preferred_days: {
            type: "array",
            items: {
              type: "string",
              enum: ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"],
            },
            description:
              'Preferred training days as weekday NAMES, e.g. ["monday","wednesday","friday"]. Write the name, never a number: the numbering conventions in play disagree (Python Mon=0, ISO Mon=1, cron Sun=0), and a wrong index silently moves a training day. Legacy integer indices (0=Mon..6=Sun) are still accepted by the server but must not be used in new calls. Sending a value that is neither is a 400 — the list is never partially stored.',
          },
          preferred_time: {
            type: "string",
            enum: ["morning", "afternoon", "evening"],
            description: "Preferred workout time of day.",
          },
          additional_context: {
            type: "string",
            description:
              "Free-form context: sport background, schedule constraints, preferences, anything else relevant.",
          },
        },
      },
      async execute(_id, params) {
        try {
          const input = asObject(params);
          const body = {};
          if (input.onboarding_status) body.onboarding_status = asTrimmedString(input.onboarding_status);
          if (input.fitness_level) body.fitness_level = asTrimmedString(input.fitness_level);
          if (Array.isArray(input.goals)) body.goals = input.goals.map(String);
          if (Array.isArray(input.limitations)) body.limitations = input.limitations.map(String);
          if (Array.isArray(input.equipment)) body.equipment = input.equipment.map(String);
          if (input.days_per_week !== undefined)
            body.days_per_week = parseInteger(input.days_per_week, { defaultValue: undefined, min: 1, max: 7 });
          // Pass the list through verbatim (names or legacy indices) and let the
          // server arbitrate. The old client-side number filter dropped every
          // string silently, so a list of weekday names arrived as [] and wiped
          // the stored preference with a 200.
          if (Array.isArray(input.preferred_days)) body.preferred_days = input.preferred_days;
          if (input.preferred_time) body.preferred_time = asTrimmedString(input.preferred_time);
          if (input.additional_context) body.additional_context = asTrimmedString(input.additional_context);

          const payload = await callRuntime(api, {
            path: fuelPath(api, "/profile/"),
            method: "PATCH",
            body,
          });
          return renderPayload(payload);
        } catch (error) {
          return renderPayload({ error: error.message });
        }
      },
    }),
    { optional: true },
  );

  // ── Create Workout Plan ─────────────────────────────────────────────
  api.registerTool(wrap({
      name: "nbhd_fuel_create_plan",
      description:
        "Create a structured, multi-week workout plan. USE THIS whenever the user asks to make / build / design / lay out / fill out / map out / write up a plan, program, routine, or schedule — including phrasings like 'fill out my workout plan for the rest of the month'. NEVER present a dated plan as a chat message: provide the WEEKLY CADENCE and let the backend assign calendar dates in the user's timezone. ALWAYS pass the user's tenant-local start anchor as start_date. For 'today' / 'I am at the gym now', start_date is today and schedule_json MUST include today's weekday — rotate the split so today is day 1; the server hard-rejects a plan that starts today with no session on today's weekday (400). Never design a cadence that excludes the requested first training day. The response's first_workout_date is the date to use when describing the first session; honor start_date_note and never assume start_date has a session. Design from the user's profile, journal context, sleep trends, lessons, and goals. schedule_json is keyed by weekday NAME — \"monday\", \"tuesday\", \"wednesday\", \"thursday\", \"friday\", \"saturday\", \"sunday\" — mapping each training day to a workout definition. Write the name, never a number: numeric indices are legacy-only and the numbering conventions disagree (Python Mon=0, ISO Mon=1, cron Sun=0), which is how a Wednesday session gets scheduled on Thursday. Set target_rpe per day, objective for the plan's through-line, and week_overrides for progression/deload. Check nbhd_fuel_summary for an existing active plan first.",
      parameters: {
        type: "object",
        additionalProperties: false,
        properties: {
          name: {
            type: "string",
            description: "Plan name, e.g. '4-Week Strength Builder'.",
          },
          start_date: {
            type: "string",
            description: "User's tenant-local start anchor, YYYY-MM-DD. ALWAYS pass the requested anchor; 'today' / 'at the gym now' means today. Omission falls back to next Monday only as backend fallback behavior, not a recommendation.",
          },
          weeks: {
            type: "integer",
            minimum: 1,
            maximum: 12,
            description: "Duration in weeks (1-12).",
          },
          days_per_week: {
            type: "integer",
            minimum: 1,
            maximum: 7,
            description: "Training days per week.",
          },
          schedule_json: {
            type: "object",
            description:
              'Weekly template. Keys are weekday NAMES: "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday" (case-insensitive; "mon".."sun" also accepted). Example: {"monday": {...}, "wednesday": {...}, "friday": {...}}. Legacy integer keys ("0"=Mon..."6"=Sun) are still accepted for back-compat but MUST NOT be used in new calls — three weekday-numbering conventions exist and picking the wrong one silently schedules the session on the wrong day. Values are workout definitions with activity, category, optional duration_minutes and detail_json. Cross-field rule: for a "today" / "at the gym now" start, schedule_json MUST include today\'s weekday by name; rotate the split so today is day 1. The server hard-rejects a plan whose start_date is today when that weekday is missing (400 naming the day to add). Never exclude the requested start day from the cadence. Only include training days — rest days are implied by absence. Send each weekday at most once; two keys resolving to the same day (e.g. "2" and "wednesday") are rejected. On strength and calisthenics days detail_json.exercises is REQUIRED: the server rejects an empty prescription (400 with the offending weekday) so an empty strength day never reaches the calendar.',
            additionalProperties: {
              type: "object",
              properties: {
                activity: { type: "string", description: "Workout name, e.g. 'Push — Chest & Shoulders'." },
                category: {
                  type: "string",
                  enum: ["strength", "cardio", "hiit", "calisthenics", "mobility", "sport", "other"],
                },
                duration_minutes: { type: "integer", description: "Estimated duration in minutes." },
                detail_json: {
                  type: "object",
                  description:
                    'Category-specific prescription. Populate every training day, not just strength. Strength/calisthenics: REQUIRED — {"exercises": [{"name": "...", "sets": [{"type": "weighted_reps", "reps": 5, "weight": 80}, ...]}]} (set type is one of weighted_reps | bodyweight_reps | hold_time). The backend validates every planned category and rejects malformed sets or an empty prescription with a 400 carrying the offending weekday, so design the real programming and self-correct. Cardio: {"distance_km": 5, "pace": "5:30"}. HIIT: {"rounds": 8, "work_s": 30, "rest_s": 30}. Mobility uses skills with hold times, e.g. {"skills":[{"name":"Hip flexor stretch","sets":[{"type":"hold_time","hold_s":45}]}]}. Every planned day needs a prescription.',
                },
                target_rpe: {
                  type: "integer",
                  minimum: 1,
                  maximum: 10,
                  description:
                    "Prescribed intensity for this session as a target RPE (1=very easy, 10=max effort). Optional.",
                },
              },
              required: ["activity", "category"],
            },
          },
          objective: {
            type: "string",
            description:
              "One-line through-line for the plan, e.g. 'Run a sub-25 5K' or 'Build pull strength'. The plan's structured objective, kept out of free-form notes.",
          },
          week_overrides: {
            type: "object",
            description:
              'Optional per-week progression/deload. Keys are 0-indexed week offsets ("0"=first week). Each overridden weekday must be a complete day object with full detail_json because it replaces the base day wholesale. Example: {"3":{"monday":{"category":"strength","activity":"Deload","target_rpe":5,"detail_json":{"exercises":[{"name":"Bench Press","sets":[{"type":"weighted_reps","reps":5,"weight":50}]}]}}}}.',
          },
          notes: {
            type: "string",
            description:
              "Programming notes and progression strategy. Tie back to the user's context — explain why you chose this structure.",
          },
        },
        required: ["name", "weeks", "days_per_week", "schedule_json"],
      },
      async execute(_id, params) {
        try {
          const input = asObject(params);
          const body = {
            name: asTrimmedString(input.name),
            weeks: parseInteger(input.weeks, { defaultValue: 4, min: 1, max: 12 }),
            days_per_week: parseInteger(input.days_per_week, { defaultValue: 3, min: 1, max: 7 }),
            schedule_json: asObject(input.schedule_json),
          };
          if (input.start_date) body.start_date = asTrimmedString(input.start_date);
          if (input.notes) body.notes = asTrimmedString(input.notes);
          if (input.objective) body.objective = asTrimmedString(input.objective);
          if (input.week_overrides) body.week_overrides = asObject(input.week_overrides);

          const payload = await callRuntime(api, {
            path: fuelPath(api, "/plans/"),
            method: "POST",
            body,
          });
          return renderWritePayload(payload);
        } catch (error) {
          return renderPayload({ error: error.message });
        }
      },
    }),
    { optional: true },
  );

  // ── Update Workout Plan ─────────────────────────────────────────────
  api.registerTool(wrap({
      name: "nbhd_fuel_update_plan",
      description:
        'Update an existing workout plan. schedule_json MERGES by default: send only the days you want to add or change, and days you omit stay untouched. Example: add weekend mobility without touching weekdays by sending schedule_json: {"saturday":{"category":"mobility","activity":"Mobility"},"sunday":{"category":"mobility","activity":"Recovery Flow"}}. Omit detail_json from an updated day to keep that day\'s existing exercises. Remove days only with remove_days; use replace_schedule:true only when intentionally replacing the entire weekly template. Legacy integer weekday keys ("0"=Mon..."6"=Sun) still work but must not be used in new calls because numbering conventions disagree. If you send strength/calisthenics detail_json, it must contain a non-empty exercises list. Schedule or weeks changes reconcile future planned workouts.',
      parameters: {
        type: "object",
        additionalProperties: false,
        properties: {
          plan_id: {
            type: "string",
            description: "UUID of the plan to update (from nbhd_fuel_summary or plan creation response).",
          },
          name: { type: "string", description: "New plan name." },
          status: {
            type: "string",
            enum: ["active", "paused", "completed", "archived"],
            description: "New plan status.",
          },
          notes: { type: "string", description: "Updated programming notes." },
          weeks: {
            type: "integer",
            minimum: 1,
            maximum: 12,
            description: "New duration in weeks. Triggers workout regeneration.",
          },
          days_per_week: {
            type: "integer",
            minimum: 1,
            maximum: 7,
            description: "New training days per week.",
          },
          schedule_json: {
            type: "object",
            description:
              'Partial weekly schedule MERGE, keyed by weekday NAME ("monday".."sunday"; "mon".."sun" also accepted). Send only days to add/change; omitted days remain. Omit detail_json on a changed day to keep its exercises. Example adding weekends without touching weekdays: {"saturday":{"category":"mobility","activity":"Mobility"},"sunday":{"category":"mobility","activity":"Recovery Flow"}}. Legacy integer keys ("0"=Mon..."6"=Sun) still work but must not be used in new calls — a wrong convention silently moves the session.',
          },
          remove_days: {
            type: "array",
            items: {
              oneOf: [
                { type: "string" },
                { type: "integer", minimum: 0, maximum: 6 },
              ],
            },
            uniqueItems: true,
            description:
              'Weekday names (preferred) or legacy integer keys to remove explicitly. Removes only these days; all others remain.',
          },
          replace_schedule: {
            type: "boolean",
            description:
              "Set true only to make schedule_json replace the entire weekly template. Days omitted from schedule_json are removed.",
          },
          week_overrides: {
            type: "object",
            description:
              'Replace the plan\'s whole per-week progression/deload map. Keys are 0-indexed ABSOLUTE plan weeks ("0" is always the plan\'s FIRST week) and must be within the plan\'s length. Each overridden weekday must be a complete day object with full detail_json because the override replaces that base day wholesale; null makes it a rest day. Sent as a whole map: it REPLACES the stored one, so include every week you want to keep. Triggers workout regeneration.',
          },
        },
        required: ["plan_id"],
      },
      async execute(_id, params) {
        try {
          const input = asObject(params);
          const planId = asTrimmedString(input.plan_id);
          if (!planId) throw new Error("plan_id is required");

          const body = {};
          if (input.name) body.name = asTrimmedString(input.name);
          if (input.status) body.status = asTrimmedString(input.status);
          if (input.notes !== undefined) body.notes = asTrimmedString(input.notes);
          if (input.weeks !== undefined)
            body.weeks = parseInteger(input.weeks, { defaultValue: undefined, min: 1, max: 12 });
          if (input.days_per_week !== undefined)
            body.days_per_week = parseInteger(input.days_per_week, { defaultValue: undefined, min: 1, max: 7 });
          if (input.schedule_json !== undefined) body.schedule_json = asObject(input.schedule_json);
          if (Array.isArray(input.remove_days)) body.remove_days = input.remove_days;
          if (input.replace_schedule !== undefined) body.replace_schedule = input.replace_schedule === true;
          if (input.week_overrides) body.week_overrides = asObject(input.week_overrides);

          const payload = await callRuntime(api, {
            path: fuelPath(api, `/plans/${encodeURIComponent(planId)}/`),
            method: "PATCH",
            body,
          });
          return renderWritePayload(payload);
        } catch (error) {
          return renderPayload({ error: error.message });
        }
      },
    }),
    { optional: true },
  );

  // ── Delete Workout Plan ─────────────────────────────────────────────
  api.registerTool(wrap({
      name: "nbhd_fuel_delete_plan",
      description:
        "Delete a workout plan. Removes all future planned workouts. Completed workouts are preserved but unlinked from the plan. Always confirm with the user before calling this.",
      parameters: {
        type: "object",
        additionalProperties: false,
        properties: {
          plan_id: {
            type: "string",
            description: "UUID of the plan to delete.",
          },
        },
        required: ["plan_id"],
      },
      async execute(_id, params) {
        try {
          const input = asObject(params);
          const planId = asTrimmedString(input.plan_id);
          if (!planId) throw new Error("plan_id is required");

          await callRuntime(api, {
            path: fuelPath(api, `/plans/${encodeURIComponent(planId)}/`),
            method: "DELETE",
          });
          return renderPayload({ deleted: true, plan_id: planId });
        } catch (error) {
          return renderPayload({ error: error.message });
        }
      },
    }),
    { optional: true },
  );
}
