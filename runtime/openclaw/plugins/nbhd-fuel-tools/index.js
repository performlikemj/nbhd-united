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
        payload = { raw };
      }
    }

    if (!response.ok) {
      const normalized = asObject(payload);
      const code = asTrimmedString(normalized.error) || "runtime_request_failed";
      const detail = asTrimmedString(normalized.detail);
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
  // ── Fuel Summary ────────────────────────────────────────────────────
  api.registerTool(
    {
      name: "nbhd_fuel_summary",
      description:
        "Get the user's fitness context: recent workouts, planned workouts, latest body weight, and fitness profile (including onboarding status). Call this at the start of fitness conversations to understand what the user has been doing and whether they've completed their fitness profile setup.",
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
    },
    { optional: true },
  );

  // ── Log Workout ─────────────────────────────────────────────────────
  api.registerTool(
    {
      name: "nbhd_fuel_log_workout",
      description:
        'Log a workout from natural language. Infer the category from the activity name (e.g. "deadlift" → strength, "ran" → cardio, "yoga" → mobility). Default to today\'s date and status "done". Do NOT ask follow-up questions — log what the user gave you and confirm briefly.',
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
            enum: ["done", "planned"],
            description: 'Whether the workout is completed or planned. Defaults to "done".',
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
                  "For strength/calisthenics. Each exercise has a name and sets.",
                items: {
                  type: "object",
                  properties: {
                    name: {
                      type: "string",
                      description: "Exercise name, e.g. 'Bench Press', 'Deadlift'.",
                    },
                    sets: {
                      type: "array",
                      items: {
                        type: "object",
                        properties: {
                          reps: {
                            type: "integer",
                            description:
                              "Number of reps performed (must be a number, e.g. 8). If unknown, omit.",
                          },
                          weight: {
                            type: "number",
                            description: "Weight in kg (e.g. 75). Use 0 for bodyweight.",
                          },
                          hold_s: {
                            type: "integer",
                            description:
                              "Hold duration in seconds (for isometric exercises like planks).",
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
          return renderPayload(payload);
        } catch (error) {
          return renderPayload({ error: error.message });
        }
      },
    },
    { optional: true },
  );

  // ── Log Body Weight ─────────────────────────────────────────────────
  api.registerTool(
    {
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
    },
    { optional: true },
  );

  // ── Log Sleep ───────────────────────────────────────────────────────
  api.registerTool(
    {
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
    },
    { optional: true },
  );

  // ── Update Fitness Profile ──────────────────────────────────────────
  api.registerTool(
    {
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
    },
    { optional: true },
  );
}
