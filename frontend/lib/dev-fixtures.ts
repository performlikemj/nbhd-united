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

// Neighborhood: two in "your sky", five others; bonds are buckets only.
let people = [
  ["f-1", "Aiko", "aiko", 12, true, "strong", "2024-03-02"],
  ["f-2", "Ren", "ren", 200, true, "steady", "2025-01-15"],
  ["f-3", "Mika", "mika", 320, false, "light", "2025-06-20"],
  ["f-4", "Daniel", "dan", 140, false, "steady", "2023-11-08"],
  ["f-5", "Sora", "sora", 40, false, "light", "2025-08-30"],
  ["f-6", "Hana", "hana", 280, false, "strong", "2024-09-12"],
  ["f-7", "Kenji", "kenji", 90, false, "light", "2025-02-01"],
].map(([id, name, handle, hue, sky, bond, since]) => ({
  friendship_id: id as string,
  display_name: name as string,
  handle: handle as string,
  avatar_hue: hue as number,
  bio: "",
  spark_count: 0,
  in_my_sky: sky as boolean,
  bond: bond as string,
  friends_since: since as string,
  has_unread_thread: false,
  thread_id: null as string | null,
}));
let wavesIn = [
  { friendship_id: "w-1", direction: "incoming", display_name: "Tomo", handle: "tomo", avatar_hue: 170, note: "We met at the running club!", created_at: "" },
];
const missionAsks = [
  { mission_id: "m-1", title: "Help Aiko move on Saturday", status: "active", target: {}, target_date: null as string | null, version: 1, my_commitment: "", my_status: "invited", my_role: "member" },
  { mission_id: "m-2", title: "Ren's 10k training buddy", status: "active", target: { cadence: "weekly" }, target_date: null as string | null, version: 1, my_commitment: "", my_status: "invited", my_role: "member" },
  { mission_id: "m-3", title: "Morning walks", status: "active", target: { cadence: "daily" }, target_date: null as string | null, version: 1, my_commitment: "Walk 20 min", my_status: "active", my_role: "owner" },
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
  if (p === "/api/v1/fuel/workouts/count/") return json({ count: isEmpty ? 0 : 42 });
  if (p === "/api/v1/fuel/resting-hr/") {
    return json(isEmpty ? [] : [58, 57, 59, 56, 57, 55, 56].map((bpm, i) => ({ id: `rhr-${i}`, date: isoDay(-i * 2), bpm, created_at: isoAt(-i * 2, 7) })));
  }
  if (p === "/api/v1/fuel/weekly-summary/") {
    return json({
      week_start: isoDay(-4),
      week_end: isoDay(2),
      by_category: isEmpty ? [] : [{ category: "strength", count: 1, total_minutes: 55 }, { category: "cardio", count: 1, total_minutes: 40 }],
      totals: isEmpty ? { sessions: 0, minutes: 0 } : { sessions: 2, minutes: 95 },
    });
  }
  if (p === "/api/v1/fuel/profile/") {
    return json({
      id: "fp-1", onboarding_status: "completed", fitness_level: "intermediate",
      goals: ["Run a 10k", "Stay strong"], limitations: [], equipment: ["barbell", "dumbbells"],
      days_per_week: 4, additional_context: "", distance_unit: "km",
      created_at: "2025-06-01T00:00:00Z", updated_at: isoAt(-3, 9),
    });
  }
  if (p === "/api/v1/journal/tree/") {
    return json([
      { kind: "daily", label: "Daily notes", items: isEmpty ? [] : [0, -1, -2].map((d) => ({ slug: isoDay(d), title: isoDay(d), updated_at: isoAt(d, 21) })) },
      { kind: "weekly", label: "Weekly reviews", items: isEmpty ? [] : [{ slug: "2026-w38", title: "Week 38", updated_at: isoAt(-5, 20) }] },
      { kind: "project", label: "Projects", items: isEmpty ? [] : [{ slug: "home-renovation", title: "Home Renovation", updated_at: isoAt(-1, 12) }] },
      { kind: "goal", label: "Goals", items: isEmpty ? [] : [{ slug: "run-a-10k", title: "Run a 10k", updated_at: isoAt(-2, 8) }] },
      { kind: "ideas", label: "Ideas", items: [] },
    ]);
  }
  const doc = p.match(/^\/api\/v1\/journal\/documents\/([^/]+)\/([^/]+)\/$/);
  if (doc && method === "GET") {
    const [, kind, slug] = doc;
    const bodies: Record<string, string> = {
      daily: "## Morning\n\nTook the long way to coffee and noticed the city was unusually quiet. Sent the revised kitchen measurements.\n\n## Evening\n\n- [x] Push day\n- [ ] Book the counter template visit\n",
      project: "Kitchen first, then the back porch.\n\n## Milestones\n\n- [x] Demo and haul-away\n- [x] Cabinets ordered\n- [ ] Counter template, fabricator Friday\n",
      goal: "Base-building block: four days a week, mostly zone 2.\n",
      weekly: "## Wins\n\n- Four sessions\n\n## Lessons\n\n- Reflect weekly, not daily\n",
    };
    return json({
      id: `doc-${kind}-${slug}`, kind, slug,
      title: kind === "daily" ? slug : slug.replace(/-/g, " ").replace(/^\w/, (c) => c.toUpperCase()),
      markdown: isEmpty ? "" : bodies[kind] ?? "",
      created_at: isoAt(-1, 8), updated_at: isoAt(0, 8),
    });
  }
  if (p === "/api/v1/journal/status/") {
    return json({
      as_of: new Date().toISOString(), typed_lifecycle: true, finance_enabled: false,
      open_tasks: isEmpty ? [] : [
        { id: "t1", title: "Book the counter template visit", status: "open", due_date: isoDay(1), pillar: "home" },
        { id: "t2", title: "Try the route with a friend", status: "open", due_date: null, pillar: "fitness" },
      ],
      active_goals: isEmpty ? [] : [{ id: "g1", title: "Run a 10k", status: "active", target_date: isoDay(40), pillar: "fitness" }],
      obligations: [],
    });
  }
  if (p === "/api/v1/dashboard/horizons/") {
    return json({
      north_star: isEmpty ? [] : [{ id: "ns1", source: "purpose", statement: "Build a body and a life that can go the distance. Steady, not frantic.", pillars: ["fuel", "journal"], status: "confirmed", origin: "chat", created_at: "2026-08-01T00:00:00Z" }],
      goals: isEmpty ? [] : [{
        id: "g1", title: "Run a 10k", slug: "run-a-10k", preview: "Base-building block: four days a week, mostly zone 2.", status: "active",
        tasks: [
          { id: "gt1", title: "Choose a local 10k", status: "done", due_date: null },
          { id: "gt2", title: "Find a comfortable running rhythm", status: "done", due_date: null },
          { id: "gt3", title: "Make room for an easy long run", status: "open", due_date: null },
          { id: "gt4", title: "Try the route with a friend", status: "open", due_date: null },
        ],
        created_at: "2026-08-10T00:00:00Z", updated_at: isoAt(-2, 8),
      }],
      pending_extractions: [],
      weekly_pulse: isEmpty ? [] : [{ week_start: isoDay(-11), week_end: isoDay(-5), week_rating: "thumbs-up", top_win: "Four sessions and no skipped mornings" }],
      weekly_documents: [],
      mood_trend: isEmpty ? [] : Array.from({ length: 14 }, (_, i) => ({ date: isoDay(-13 + i), mood: ["steady", "good", "low", "good"][i % 4], energy: String(5 + (i % 4)) })),
      momentum: Array.from({ length: 14 }, (_, i) => ({ date: isoDay(-13 + i), message_count: isEmpty ? 0 : (i * 7) % 11, has_journal: !isEmpty && i % 2 === 0 })),
      current_streak: isEmpty ? 0 : 14,
      assistant_insights: isEmpty ? [] : [
        { id: "ai1", pillar: "journal", topic_slug: "deep-work", topic_display_name: "Deep work", statement: "You write the most on focused mornings.", status: "open", confidence: 0.72, created_at: isoAt(-3, 9), last_confirmed_at: null },
        { id: "ai2", pillar: "fuel", topic_slug: "fitness", topic_display_name: "Fitness", statement: "Cardio days line up with brighter mood entries.", status: "confirmed", confidence: 0.81, created_at: isoAt(-9, 9), last_confirmed_at: isoAt(-2, 9) },
      ],
      topic_signals: [],
    });
  }
  if (p === "/api/v1/lessons/constellation/") {
    const lessons = [
      ["Start before you feel ready.", 1, "Work", 120, 90],
      ["Protect the first hour.", 1, "Work", 180, 150],
      ["Say no to the good to keep the great.", 1, "Work", 90, 170],
      ["Ship small, ship often.", 1, "Work", 160, 60],
      ["Sleep is the first workout.", 2, "Health", 420, 110],
      ["Easy days make hard days possible.", 2, "Health", 470, 190],
      ["Walk after dinner.", 2, "Health", 520, 120],
      ["Reflect weekly, not daily.", 3, "Growth", 300, 300],
      ["Ask for the feedback you fear.", 3, "Growth", 350, 260],
      ["Notice what drains you.", 3, "Growth", 330, 340],
      ["Finish the rough draft first.", 4, "Craft", 150, 360],
      ["Cut the first paragraph.", 4, "Craft", 200, 330],
      ["Steal like an artist.", 4, "Craft", 110, 400],
      ["Make it work, then make it good.", 4, "Craft", 190, 420],
      ["Rest before you are tired.", 4, "Craft", 230, 390],
    ] as const;
    return json({
      nodes: isEmpty ? [] : lessons.map(([text, cid, label, x, y], i) => ({ id: i + 1, text, context: "", tags: [label.toLowerCase()], cluster_id: cid, cluster_label: label, x, y, created_at: isoAt(-30 + i, 9) })),
      edges: isEmpty ? [] : [{ source: 1, target: 2, similarity: 0.7, connection_type: "similar" }, { source: 3, target: 4, similarity: 0.66, connection_type: "similar" }],
      affinity_edges: [],
      clusters: isEmpty ? [] : [{ id: 1, label: "Work", count: 4, tags: ["work"] }, { id: 2, label: "Health", count: 3, tags: ["health"] }, { id: 3, label: "Growth", count: 3, tags: ["growth"] }, { id: 4, label: "Craft", count: 5, tags: ["craft"] }],
    });
  }
  if (p === "/api/v1/datebook/agenda/") {
    // `?agenda=stale` = last complete sync too old to cover the week; `?agenda=disabled` = not connected.
    const mode = typeof window !== "undefined" ? new URLSearchParams(window.location.search).get("agenda") : null;
    if (mode === "disabled") return json({ state: "datebook_disabled" });
    const days = Array.from({ length: 7 }, (_, i) => isoDay(i));
    const zoned = (id: string, day: number, h: number, m: number, mins: number, title: string, calendar = "Home") => ({
      entity: "event", id, day: isoDay(day), title, location: "", notes: "", calendar_title: calendar, source_title: "iCloud", display_text: title, authorization: "full_access", read_only: false,
      time: { kind: "zoned", start_at: isoAt(day, h, m), end_at: new Date(new Date(isoAt(day, h, m)).getTime() + mins * 60000).toISOString(), tz_id: "Asia/Tokyo" },
    });
    const items = isEmpty || mode === "stale" ? [] : [
      zoned("e1", 0, 9, 30, 30, "Stand-up", "Work"),
      zoned("e2", 0, 18, 0, 60, "Dinner with Aiko"),
      { entity: "reminder", id: "r1", day: isoDay(1), title: "Renew passport", location: "", notes: "", list_title: "Errands", due: { kind: "all_day", date: isoDay(1) } },
      zoned("e3", 1, 7, 0, 45, "Run club"),
      { entity: "event", id: "e4", day: isoDay(3), title: "Kyoto trip", location: "", notes: "", calendar_title: "Home", source_title: "iCloud", display_text: "Kyoto trip", authorization: "full_access", read_only: false, time: { kind: "all_day", start_date: isoDay(3), end_date_exclusive: isoDay(5) } },
      zoned("e5", 6, 10, 0, 60, "Dentist"),
    ];
    return json({
      state: "ok",
      server_now: new Date().toISOString(),
      timezone: Intl.DateTimeFormat().resolvedOptions().timeZone,
      requested: { start_day: days[0], end_day_exclusive: isoDay(7), start_at: isoAt(0, 0), end_at: isoAt(7, 0) },
      covered: mode === "stale" ? null : { start_at: isoAt(0, 0), end_at: isoAt(isEmpty ? 7 : 5, 0) },
      covered_days: mode === "stale" ? [] : days.slice(0, isEmpty ? 7 : 5),
      freshness: {
        events_last_complete_sync_at: mode === "stale" ? new Date(Date.now() - 3 * 86400000).toISOString() : new Date(Date.now() - 12 * 60000).toISOString(),
        reminders_last_complete_sync_at: null,
        events_authorization: "full_access",
        gateway_status: "active",
      },
      includes: { events: true, reminders: true },
      items,
      truncated: false,
    });
  }
  if (p === "/api/v1/friends/home/") {
    missionAsks[0].target_date = isoDay(2);
    wavesIn = wavesIn.map((w) => ({ ...w, created_at: w.created_at || isoAt(-1, 18) }));
    return json({
      profile: { handle: "yuki", display_name: "Yuki", avatar_hue: 260 },
      neighbors: isEmpty ? [] : people,
      pending_in: isEmpty ? [] : wavesIn,
      pending_out: isEmpty ? [] : [{ friendship_id: "w-2", direction: "outgoing", display_name: "Mei", handle: "mei", avatar_hue: 330, note: "", created_at: isoAt(-3, 9) }],
      moments: [],
      cursor: null,
    });
  }
  if (p === "/api/v1/friends/") {
    const legacy = (x: (typeof people)[number]) => ({ friendship_id: x.friendship_id, display_name: x.display_name, handle: x.handle, avatar_hue: x.avatar_hue, status: "accepted", since: x.friends_since });
    return json({ profile: null, neighbors: isEmpty ? [] : people.map(legacy), pending_incoming: isEmpty ? [] : wavesIn, pending_outgoing: [] });
  }
  const skyEdge = p.match(/^\/api\/v1\/friends\/([^/]+)\/sky\/$/);
  if (skyEdge) {
    const inSky = method === "POST";
    if (inSky && people.filter((x) => x.in_my_sky).length >= 12) return json({ error: "sky_full", cap: 12 });
    people = people.map((x) => (x.friendship_id === skyEdge[1] ? { ...x, in_my_sky: inSky } : x));
    return json({ friendship_id: skyEdge[1], in_my_sky: inSky });
  }
  const waveAct = p.match(/^\/api\/v1\/friends\/waves\/([^/]+)\/(accept|decline)\/$/);
  if (waveAct) {
    const w = wavesIn.find((x) => x.friendship_id === waveAct[1]);
    wavesIn = wavesIn.filter((x) => x.friendship_id !== waveAct[1]);
    if (w && waveAct[2] === "accept") {
      people = [...people, { friendship_id: w.friendship_id, display_name: w.display_name, handle: w.handle, avatar_hue: w.avatar_hue, bio: "", spark_count: 0, in_my_sky: false, bond: "light", friends_since: isoDay(0), has_unread_thread: false, thread_id: null }];
    }
    return json({ friendship_id: waveAct[1], status: waveAct[2] === "accept" ? "accepted" : "declined" });
  }
  if (p === "/api/v1/friends/missions/" && method === "GET") {
    const rows = isEmpty ? [] : missionAsks;
    return json(url.searchParams.get("include_invited") ? rows : rows.filter((m) => m.my_status === "active"));
  }
  const joinM = p.match(/^\/api\/v1\/friends\/missions\/([^/]+)\/join\/$/);
  if (joinM) {
    const m = missionAsks.find((x) => x.mission_id === joinM[1]);
    if (m) m.my_status = "active";
    return json({ mission_id: joinM[1], status: "active" });
  }
  if (p === "/api/v1/friends/circles/" && method === "GET") {
    return json(isEmpty ? [] : [
      { circle_id: "c-1", name: "Sunday run club", hue: 150, member_count: 6, my_role: "member", invite_code: null },
      { circle_id: "c-2", name: "Book swap", hue: 30, member_count: 4, my_role: "admin", invite_code: "BOOKS1" },
    ]);
  }
  if (p === "/api/v1/friends/threads/") {
    if (method === "POST") return json({ thread_id: `t-${String(bodyOf(init).friendship_id ?? "x")}`, friendship_id: bodyOf(init).friendship_id });
    return json([]);
  }
  if (/^\/api\/v1\/friends\/threads\/[^/]+\/messages\/$/.test(p)) return json({ messages: [], next_cursor: null });
  if (/^\/api\/v1\/friends\/threads\/[^/]+\/read\/$/.test(p)) return json({ ok: true });
  if (p === "/api/v1/friends/shares/pending/") return json([]);
  if (p === "/api/v1/friends/absorbed/") return json([]);
  if (p === "/api/v1/friends/mission-actions/") return json([]);
  if (p === "/api/v1/lessons/" && url.searchParams.get("status") === "approved") return json([]);
  if (p === "/api/v1/friends/profile/") return json({ handle: "yuki", display_name: "Yuki", bio: "", avatar_hue: 260, discoverable: true });
  if (p === "/api/v1/lessons/pending/") return json([]);
  if (p === "/api/v1/core/sessions/") {
    return json(isEmpty ? [] : [{
      id: "med-1", date: isoDay(0), status: "ready", completed_at: null,
      lesson: { summary: "Begin again, gently." }, phase_arc: null,
      title: "Begin again, gently", theme: "rest", voice: "calm", model: "", guidance_text: "",
      audio_url: "", ogg_url: "", duration_ms: 660000, ambient_bed: "lakeshore",
      error: "", user_feedback: "", feedback_note: "", feedback_at: null,
      created_at: isoAt(0, 6), updated_at: isoAt(0, 6),
    }]);
  }
  if (p === "/api/v1/core/profile/") {
    return json({
      id: "cp-1", onboarding_status: "completed", preferred_voice: "calm", preferred_duration_minutes: 11,
      ambient_bed_enabled: true, daily_cron_enabled: true, preferred_time: "07:00", additional_context: "",
      created_at: "2025-06-01T00:00:00Z", updated_at: isoAt(-3, 9),
    });
  }
  if (p === "/api/v1/tenants/personas/") {
    return json([
      { key: "neighbor", label: "Neighbor", description: "Warm and practical", emoji: "" },
      { key: "coach", label: "Coach", description: "Direct and encouraging", emoji: "" },
    ]);
  }
  if (p === "/api/v1/tenants/preferences/") return json({ agent_persona: "neighbor" });
  if (p === "/api/v1/tenants/refresh-config/") {
    return json({ can_refresh: true, last_refreshed: isoAt(-2, 10), cooldown_seconds: 0, status: "ok", has_pending_update: false, container_image_tag: "v1", latest_image_tag: "v1", image_outdated: false });
  }
  if (p === "/api/v1/fuel/goals/") return json([]);
  return undefined;
}
