/**
 * DEV-ONLY fixture API for screenshotting the logged-in web app without a
 * backend. Loaded exclusively through a dynamic import behind
 * `process.env.NODE_ENV === "development" && NEXT_PUBLIC_WEB_FIXTURES === "1"`
 * in `apiFetch`, so production builds never include this module.
 *
 * Run: `NEXT_PUBLIC_WEB_FIXTURES=1 npm run dev`, then in the browser set
 * `localStorage.nbhd_access_token` to any string (the fixture API ignores it).
 * Append `?fixture=empty` to a page URL to render empty states, or
 * `?fixture=legacy` for a tenant without the web redesign (old shell).
 */

type Json = unknown;

function empty(): boolean {
  if (typeof window === "undefined") return false;
  return new URLSearchParams(window.location.search).get("fixture") === "empty";
}

function isoDay(offset: number): string {
  const d = new Date();
  d.setDate(d.getDate() + offset);
  return d.toISOString().slice(0, 10);
}

function isoAt(offset: number, hour: number, minute = 0): string {
  const d = new Date();
  d.setDate(d.getDate() + offset);
  d.setHours(hour, minute, 0, 0);
  return d.toISOString();
}

const tenant = {
  id: "00000000-0000-4000-8000-000000000001",
  user: { id: 1, email: "yuki@example.com", display_name: "Yuki" },
  status: "active",
  model_tier: "starter",
  has_active_subscription: true,
  is_trial: false,
  trial_ends_at: null,
  trial_days_remaining: null,
  container_id: "oc-fixture",
  container_fqdn: "",
  messages_today: 4,
  messages_this_month: 120,
  tokens_this_month: 0,
  estimated_cost_this_month: "0",
  monthly_token_budget: 0,
  monthly_cost_budget: "0",
  preferred_model: "",
  applied_model: "",
  applied_model_at: null,
  effective_model: "",
  free_model_offer: null,
  task_model_preferences: {},
  last_message_at: null,
  provisioned_at: null,
  config_refreshed_at: null,
  config_version: 1,
  pending_config_version: 1,
  hibernated_at: null,
  created_at: "2025-06-01T00:00:00Z",
  pending_deletion: false,
  deletion_scheduled_at: null,
  platform_budget_exceeded: false,
  constellation_enabled: true,
  finance_enabled: false,
  gravity_available: false,
  fuel_enabled: true,
  web_redesign: true,
  core_enabled: true,
  byo_models_enabled: false,
  neighborhood_enabled: true,
  friends_enabled: true,
};

const me = {
  id: 1,
  email: "yuki@example.com",
  username: "yuki",
  apple_linked: false,
  display_name: "Yuki Tanaka",
  language: "en",
  timezone: "Asia/Tokyo",
  location_city: "Tokyo",
  location_lat: null,
  location_lon: null,
  telegram_chat_id: null,
  telegram_username: "",
  line_user_id: null,
  line_display_name: "",
  preferred_channel: "telegram",
  tenant,
};

const sleepHours = [7.2, 6.4, 7.8, 5.6, 6.9, 7.5, 6.8, 7.1, 8.0, 6.2, 7.4, 6.6, 7.0, 7.3];
let sleep: { id: string; date: string; duration_hours: string; quality: number | null; notes: string; created_at: string }[] =
  sleepHours.map((h, i) => ({
    id: `sleep-${i}`,
    date: isoDay(-i),
    duration_hours: h.toFixed(2),
    quality: [4, 3, 5, 2, 4, 4, 3, 4, 5, 2, 4, 3, 4, 4][i] ?? null,
    notes: i === 3 ? "Woke up twice" : "",
    created_at: isoAt(-i, 7),
  }));

const weights = [71.6, 71.8, 71.7, 72.0, 72.1, 71.9, 72.2, 72.4, 72.3, 72.6, 72.5, 72.7, 72.9, 72.8];
let bodyWeight: { id: string; date: string; weight_kg: string; created_at: string }[] = weights.map((w, i) => ({
  id: `bw-${i}`,
  date: isoDay(-i * 2),
  weight_kg: w.toFixed(1),
  created_at: isoAt(-i * 2, 8),
}));

function workout(id: string, offset: number, activity: string, status: string, category: string, minutes: number) {
  return {
    id,
    date: isoDay(offset),
    scheduled_at: isoAt(offset, 18, 30),
    window_start_at: null,
    window_end_at: null,
    status,
    source: "assistant",
    original_workout: null,
    skip_reason: "",
    category,
    activity,
    duration_minutes: minutes,
    rpe: status === "done" ? 7 : null,
    notes: "",
    notes_thread: [],
    detail_json: {},
    plan_id: null,
    plan_name: null,
    created_at: isoAt(offset - 3, 9),
    updated_at: isoAt(offset, 19),
  };
}

const workouts = [
  workout("w-1", -3, "Zone-2 run", "done", "cardio", 40),
  workout("w-2", -2, "Deadlift 5×5", "done", "strength", 55),
  workout("w-3", 0, "Back squat 5×5", "planned", "strength", 50),
  workout("w-4", 2, "Easy long run", "planned", "cardio", 60),
];

const feed = [
  {
    id: "cron:morning-1",
    role: "assistant",
    text: "Good morning. Short night, 6h 24m. Push day is on for 6:30pm, and the fabricator cutoff is Friday.",
    created_at: isoAt(0, 7, 40),
    source: "cron",
    thread_id: "main",
    has_image: false,
    has_document: false,
    panels: [
      { kind: "sleep", params: { range: "last_night" }, title: "Last night's sleep" },
      { kind: "workout", params: { day: isoDay(0) }, title: "Today's workout" },
      { kind: "schedule", params: { range: "today" }, title: "Today's calendar" },
      { kind: "log_table", params: { metric: "body_weight", range: "this_month" }, title: "Weight this month" },
    ],
  },
  {
    id: "app:12:1",
    role: "assistant",
    text: "Your week at a glance: a steady wind-down would help the shorter nights.",
    created_at: isoAt(-1, 21, 5),
    source: "app",
    thread_id: "main",
    has_image: false,
    has_document: false,
    panels: [{ kind: "sleep", params: { range: "this_week" }, title: "Sleep this week" }],
  },
];

function json(body: Json): Json {
  return JSON.parse(JSON.stringify(body));
}

function bodyOf(init?: RequestInit): Record<string, unknown> {
  try {
    return init?.body ? (JSON.parse(String(init.body)) as Record<string, unknown>) : {};
  } catch {
    return {};
  }
}

/** Returns a fixture response for `path`, or undefined to fall through to the network. */
export function fixtureResponse(path: string, init?: RequestInit): Json | undefined {
  const method = (init?.method ?? "GET").toUpperCase();
  const url = new URL(path, "http://fixture.local");
  const p = url.pathname;
  const isEmpty = empty();

  if (p === "/api/v1/auth/me/") {
    // `?fixture=legacy` = a tenant without the web redesign (old shell).
    const legacy = typeof window !== "undefined" && new URLSearchParams(window.location.search).get("fixture") === "legacy";
    return json(legacy ? { ...me, tenant: { ...tenant, web_redesign: false } } : me);
  }
  if (p === "/api/v1/chat/messages/") {
    return json({ messages: isEmpty ? [] : feed, cursor: null });
  }
  if (p === "/api/v1/fuel/sleep/") {
    if (method === "POST") {
      const b = bodyOf(init);
      const row = {
        id: `sleep-new-${Date.now()}`,
        date: String(b.date ?? isoDay(0)),
        duration_hours: Number(b.duration_hours ?? 0).toFixed(2),
        quality: (b.quality as number | null) ?? null,
        notes: String(b.notes ?? ""),
        created_at: new Date().toISOString(),
      };
      sleep = [row, ...sleep];
      return json(row);
    }
    return json(isEmpty ? [] : sleep);
  }
  const sleepDetail = p.match(/^\/api\/v1\/fuel\/sleep\/([^/]+)\/$/);
  if (sleepDetail) {
    const id = sleepDetail[1];
    if (method === "PATCH") {
      const b = bodyOf(init);
      if (String(b.notes ?? "").includes("fail")) {
        throw Object.assign(new Error("Couldn't save (fixture failure)."), { status: 400 });
      }
      sleep = sleep.map((r) =>
        r.id === id
          ? {
              ...r,
              ...(b.duration_hours !== undefined ? { duration_hours: Number(b.duration_hours).toFixed(2) } : {}),
              ...(b.quality !== undefined ? { quality: b.quality as number | null } : {}),
              ...(b.notes !== undefined ? { notes: String(b.notes) } : {}),
              ...(b.date !== undefined ? { date: String(b.date) } : {}),
            }
          : r,
      );
      return json(sleep.find((r) => r.id === id));
    }
    if (method === "DELETE") {
      sleep = sleep.filter((r) => r.id !== id);
      return json({});
    }
  }
  if (p === "/api/v1/fuel/body-weight/") {
    if (method === "POST") {
      const b = bodyOf(init);
      const row = {
        id: `bw-new-${Date.now()}`,
        date: String(b.date ?? isoDay(0)),
        weight_kg: Number(b.weight_kg ?? 0).toFixed(1),
        created_at: new Date().toISOString(),
      };
      bodyWeight = [row, ...bodyWeight];
      return json(row);
    }
    return json(isEmpty ? [] : bodyWeight);
  }
  const bwDetail = p.match(/^\/api\/v1\/fuel\/body-weight\/([^/]+)\/$/);
  if (bwDetail) {
    const id = bwDetail[1];
    if (method === "PATCH") {
      const b = bodyOf(init);
      if (Number(b.weight_kg) > 400) {
        throw Object.assign(new Error("Weight looks wrong (fixture failure)."), { status: 400 });
      }
      bodyWeight = bodyWeight.map((r) =>
        r.id === id
          ? {
              ...r,
              ...(b.weight_kg !== undefined ? { weight_kg: Number(b.weight_kg).toFixed(1) } : {}),
              ...(b.date !== undefined ? { date: String(b.date) } : {}),
            }
          : r,
      );
      return json(bodyWeight.find((r) => r.id === id));
    }
    if (method === "DELETE") {
      bodyWeight = bodyWeight.filter((r) => r.id !== id);
      return json({});
    }
  }
  if (p === "/api/v1/fuel/workouts/") return json(isEmpty ? [] : workouts);
  if (p === "/api/v1/fuel/goals/") return json([]);
  return undefined;
}
