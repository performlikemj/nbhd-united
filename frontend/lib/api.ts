import { clearTokens, getAccessToken, getRefreshToken, setTokens } from "@/lib/auth";
import {
  AuthUser,
  Automation,
  AutomationRun,
  CronJob,
  CronJobDelivery,
  CronJobPayload,
  CronJobSchedule,
  PendingReminder,
  PendingRemindersResponse,
  DashboardData,
  DocumentListItem,
  DocumentResponse,
  Integration,
  JournalEntry,
  JournalEntryEnergy,
  NoteTemplate,
  NoteTemplateSection,
  SidebarSection,
  Tenant,
  TransparencyData,
  UsageRecord,
  UsageSummary,
  RefreshConfigStatus,
  ProvisioningStatus,
  WeeklyReview,
  Lesson,
  ConstellationData,
} from "@/lib/types";

const API_BASE = process.env.NEXT_PUBLIC_API_BASE_URL ?? "http://localhost:8000";

let refreshPromise: Promise<string> | null = null;

async function refreshAccessToken(): Promise<string> {
  const refresh = getRefreshToken();
  if (!refresh) {
    clearTokens();
    throw new Error("No refresh token available.");
  }

  const response = await fetch(`${API_BASE}/api/v1/auth/refresh/`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ refresh }),
  });

  if (!response.ok) {
    clearTokens();
    throw new Error("Session expired. Please sign in again.");
  }

  const data = await response.json();
  setTokens(data.access, refresh);
  return data.access;
}

async function apiFetch<T>(path: string, init?: RequestInit): Promise<T> {
  const accessToken = getAccessToken();

  const headers: Record<string, string> = {
    "Content-Type": "application/json",
    ...(init?.headers as Record<string, string> ?? {}),
  };

  if (accessToken) {
    headers["Authorization"] = `Bearer ${accessToken}`;
  }

  let response = await fetch(`${API_BASE}${path}`, {
    ...init,
    headers,
  });

  if (response.status === 401 && getRefreshToken()) {
    try {
      if (!refreshPromise) {
        refreshPromise = refreshAccessToken();
      }
      const newToken = await refreshPromise;
      refreshPromise = null;

      headers["Authorization"] = `Bearer ${newToken}`;
      response = await fetch(`${API_BASE}${path}`, {
        ...init,
        headers,
      });
    } catch {
      refreshPromise = null;
      // Fall through — response is still the original 401
    }
  }

  if (response.status === 401) {
    // Distinguish "your session expired" from "those credentials don't work".
    // The first case requires a prior refresh token; the second is the
    // backend rejecting login / signup / password-reset-confirm payloads.
    // Showing "Session expired" on a fresh login attempt was confusing —
    // see PR fixing #696-adjacent UX.
    const hadSession = !!getRefreshToken();
    clearTokens();
    if (hadSession) {
      if (typeof window !== "undefined" && window.location.pathname !== "/login") {
        window.location.href = "/login";
      }
      throw new Error("Session expired. Please sign in again.");
    }
    // No prior session. Surface the server's actual detail so callers can
    // suggest the right next step (e.g., the login page can show a
    // password-reset CTA on credentials failures).
    let detail = "Incorrect email or password.";
    try {
      const body = await response.json();
      if (body && typeof body.detail === "string" && body.detail.trim()) {
        detail = body.detail;
      }
    } catch {
      // Body isn't JSON — keep the default detail.
    }
    const err = new Error(detail);
    (err as Error & { status: number }).status = 401;
    throw err;
  }

  if (!response.ok) {
    const message = await response.text();
    const err = new Error(message || `Request failed: ${response.status}`);
    (err as Error & { status: number }).status = response.status;
    throw err;
  }

  if (response.status === 204) {
    return undefined as T;
  }

  return (await response.json()) as T;
}

// Auth
export async function login(email: string, password: string): Promise<{ access: string; refresh: string }> {
  return apiFetch<{ access: string; refresh: string }>("/api/v1/auth/login/", {
    method: "POST",
    body: JSON.stringify({ email, password }),
  });
}

export async function signup(
  email: string,
  password: string,
  displayName?: string,
): Promise<{ access: string; refresh: string }> {
  return apiFetch<{ access: string; refresh: string }>("/api/v1/auth/signup/", {
    method: "POST",
    body: JSON.stringify({ email, password, display_name: displayName }),
  });
}

export async function logout(): Promise<void> {
  const refresh = getRefreshToken();
  if (!refresh) {
    return;
  }

  await apiFetch<void>("/api/v1/auth/logout/", {
    method: "POST",
    body: JSON.stringify({ refresh }),
  });
}

export async function requestPasswordReset(email: string): Promise<{ detail: string }> {
  return apiFetch<{ detail: string }>("/api/v1/auth/password-reset/request/", {
    method: "POST",
    body: JSON.stringify({ email }),
  });
}

export async function confirmPasswordReset(
  uid: string,
  token: string,
  newPassword: string,
): Promise<{ access: string; refresh: string }> {
  return apiFetch<{ access: string; refresh: string }>(
    "/api/v1/auth/password-reset/confirm/",
    {
      method: "POST",
      body: JSON.stringify({ uid, token, new_password: newPassword }),
    },
  );
}

export function fetchMe(): Promise<AuthUser> {
  return apiFetch<AuthUser>("/api/v1/auth/me/");
}

export function updateProfile(data: {
  display_name?: string;
  language?: string;
  timezone?: string;
  location_city?: string;
  location_lat?: number | null;
  location_lon?: number | null;
}): Promise<AuthUser> {
  return apiFetch<AuthUser>("/api/v1/tenants/profile/", {
    method: "PATCH",
    body: JSON.stringify(data),
  });
}

// Dashboard
export function fetchDashboard(): Promise<DashboardData> {
  return apiFetch<DashboardData>("/api/v1/dashboard/");
}

export function fetchUsageHistory(): Promise<{ results: UsageRecord[] }> {
  return apiFetch<{ results: UsageRecord[] }>("/api/v1/dashboard/usage/");
}

export function fetchUsageSummary(): Promise<UsageSummary> {
  return apiFetch<UsageSummary>("/api/v1/billing/usage/summary/");
}

export function fetchHorizons(): Promise<import("@/lib/types").HorizonsData> {
  return apiFetch<import("@/lib/types").HorizonsData>("/api/v1/dashboard/horizons/");
}

// Journal current-status projection — live state derived from typed models
// + the finance ledger (never a stale baked copy). See status_projection.py.
export function fetchJournalStatus(): Promise<import("@/lib/types").JournalStatus> {
  return apiFetch<import("@/lib/types").JournalStatus>("/api/v1/journal/status/");
}

export function approveExtraction(id: string): Promise<{ id: string; status: string }> {
  return apiFetch<{ id: string; status: string }>(`/api/v1/journal/extractions/${id}/approve/`, { method: "POST" });
}

export function dismissExtraction(id: string): Promise<{ id: string; status: string }> {
  return apiFetch<{ id: string; status: string }>(`/api/v1/journal/extractions/${id}/dismiss/`, { method: "POST" });
}

// Assistant insights — the assistant's memory of patterns it has noticed.
// Confirm and refute mutate AssistantInsight rows on the backend; both
// invalidate the Horizons query so the card flips status in place.
export function confirmInsight(id: string, note?: string): Promise<import("@/lib/types").HorizonsAssistantInsight> {
  return apiFetch<import("@/lib/types").HorizonsAssistantInsight>(
    `/api/v1/insights/insights/${id}/confirm/`,
    {
      method: "POST",
      body: note ? JSON.stringify({ note }) : undefined,
      headers: note ? { "Content-Type": "application/json" } : undefined,
    },
  );
}

export function refuteInsight(id: string, note?: string): Promise<import("@/lib/types").HorizonsAssistantInsight> {
  return apiFetch<import("@/lib/types").HorizonsAssistantInsight>(
    `/api/v1/insights/insights/${id}/refute/`,
    {
      method: "POST",
      body: note ? JSON.stringify({ note }) : undefined,
      headers: note ? { "Content-Type": "application/json" } : undefined,
    },
  );
}

// Tenants
export function fetchTenant(): Promise<Tenant> {
  return apiFetch<Tenant>("/api/v1/tenants/me/");
}

// Entity registry — per-tenant PII placeholders with optional identity metadata.
// Backs the privacy_placeholders envelope identity-context sub-section.
export interface EntityRegistryEntry {
  placeholder: string;
  name: string;
  relationship: string;
  notes: string;
  updated_at: string | null;
}

export function fetchEntityRegistry(): Promise<{ entries: EntityRegistryEntry[] }> {
  return apiFetch<{ entries: EntityRegistryEntry[] }>("/api/v1/tenants/settings/entity-registry/");
}

export function updateEntityRegistryEntry(
  placeholder: string,
  patch: Partial<Pick<EntityRegistryEntry, "name" | "relationship" | "notes">>,
): Promise<EntityRegistryEntry> {
  return apiFetch<EntityRegistryEntry>(
    `/api/v1/tenants/settings/entity-registry/${encodeURIComponent(placeholder)}/`,
    {
      method: "PATCH",
      body: JSON.stringify(patch),
    },
  );
}

export function deleteEntityRegistryEntry(placeholder: string): Promise<void> {
  return apiFetch<void>(
    `/api/v1/tenants/settings/entity-registry/${encodeURIComponent(placeholder)}/`,
    { method: "DELETE" },
  );
}

// PII denylist — per-tenant canonical-keyed words the redactor should
// never treat as PII. Populated manually via the People settings page
// when a user spots an NER false positive ("goal", "calendar", an emoji).
export interface PIIDenylistEntry {
  key: string;
  reason: string;
  decided_at: string | null;
}

export function fetchPIIDenylist(): Promise<{ entries: PIIDenylistEntry[] }> {
  return apiFetch<{ entries: PIIDenylistEntry[] }>("/api/v1/tenants/settings/pii-denylist/");
}

export function addPIIDenylistEntry(name: string): Promise<PIIDenylistEntry> {
  return apiFetch<PIIDenylistEntry>("/api/v1/tenants/settings/pii-denylist/", {
    method: "POST",
    body: JSON.stringify({ name }),
  });
}

export function removePIIDenylistEntry(key: string): Promise<void> {
  return apiFetch<void>(`/api/v1/tenants/settings/pii-denylist/${encodeURIComponent(key)}/`, {
    method: "DELETE",
  });
}

export interface PIIDenylistBulkResult {
  added: string[];
  skipped: Array<{ name: string; reason: string }>;
}

export function bulkAddPIIDenylistEntries(names: string[]): Promise<PIIDenylistBulkResult> {
  return apiFetch<PIIDenylistBulkResult>("/api/v1/tenants/settings/pii-denylist/bulk/", {
    method: "POST",
    body: JSON.stringify({ names }),
  });
}

export function onboardTenant(data: { display_name?: string; language?: string; agent_persona?: string }): Promise<Tenant> {
  return apiFetch<Tenant>("/api/v1/tenants/onboard/", {
    method: "POST",
    body: JSON.stringify(data),
  });
}

// Personas
export interface PersonaOption {
  key: string;
  label: string;
  description: string;
  emoji: string;
}

export function fetchPersonas(): Promise<PersonaOption[]> {
  return apiFetch<PersonaOption[]>("/api/v1/tenants/personas/");
}

export function fetchPreferences(): Promise<{ agent_persona: string }> {
  return apiFetch<{ agent_persona: string }>("/api/v1/tenants/preferences/");
}

export function updatePreferences(data: { agent_persona: string }): Promise<{ agent_persona: string }> {
  return apiFetch<{ agent_persona: string }>("/api/v1/tenants/preferences/", {
    method: "PATCH",
    body: JSON.stringify(data),
  });
}

// Refresh Config
export function fetchRefreshConfigStatus(): Promise<RefreshConfigStatus> {
  return apiFetch<RefreshConfigStatus>("/api/v1/tenants/refresh-config/");
}

export function refreshConfig(): Promise<{ detail: string; last_refreshed: string }> {
  return apiFetch<{ detail: string; last_refreshed: string }>("/api/v1/tenants/refresh-config/", { method: "POST" });
}

export function fetchProvisioningStatus(): Promise<ProvisioningStatus> {
  return apiFetch<ProvisioningStatus>("/api/v1/tenants/provisioning-status/");
}

export function retryProvisioning(): Promise<{ detail: string; tenant_status: string; ready: boolean; retry_after_seconds?: number }> {
  return apiFetch<{ detail: string; tenant_status: string; ready: boolean; retry_after_seconds?: number }>(
    "/api/v1/tenants/retry-provisioning/",
    { method: "POST" },
  );
}

// Telegram linking
export interface TelegramLinkResponse {
  deep_link: string;
  qr_code: string;  // base64 data URL
  expires_at: string;
}

export interface TelegramStatus {
  linked: boolean;
  telegram_username?: string;
  telegram_chat_id?: number;
}

export function generateTelegramLink(): Promise<TelegramLinkResponse> {
  return apiFetch<TelegramLinkResponse>("/api/v1/tenants/telegram/generate-link/", {
    method: "POST",
  });
}

export function fetchTelegramStatus(): Promise<TelegramStatus> {
  return apiFetch<TelegramStatus>("/api/v1/tenants/telegram/status/");
}

export function unlinkTelegram(): Promise<{ success: boolean }> {
  return apiFetch<{ success: boolean }>("/api/v1/tenants/telegram/unlink/", {
    method: "POST",
  });
}

// LINE linking
export interface LineLinkResponse {
  deep_link: string;
  qr_code: string;  // base64 data URL
  expires_at: string;
}

export interface LineStatus {
  linked: boolean;
  line_display_name?: string;
  // Fleet-wide LINE Push monthly-quota state. Surfaced so the channel
  // selector can disable the LINE radio when the cap is hit and show
  // the user why. Backed by apps/router/models.py:LineQuotaState.
  quota?: {
    exhausted: boolean;
    checked_at: string | null;
    exhausted_at: string | null;
  };
}

export function generateLineLink(): Promise<LineLinkResponse> {
  return apiFetch<LineLinkResponse>("/api/v1/tenants/line/generate-link/", {
    method: "POST",
  });
}

export function fetchLineStatus(): Promise<LineStatus> {
  return apiFetch<LineStatus>("/api/v1/tenants/line/status/");
}

export function unlinkLine(): Promise<{ success: boolean }> {
  return apiFetch<{ success: boolean }>("/api/v1/tenants/line/unlink/", {
    method: "POST",
  });
}

export function setPreferredChannel(channel: "telegram" | "line"): Promise<{ preferred_channel: string; message: string }> {
  return apiFetch<{ preferred_channel: string; message: string }>("/api/v1/tenants/line/preferred-channel/", {
    method: "PATCH",
    body: JSON.stringify({ preferred_channel: channel }),
  });
}

// Integrations
type IntegrationResponse = Integration[] | { results?: Integration[] };

export async function fetchIntegrations(): Promise<Integration[]> {
  const data = await apiFetch<IntegrationResponse>("/api/v1/integrations/");
  if (Array.isArray(data)) {
    return data;
  }
  return data.results ?? [];
}

export async function disconnectIntegration(id: string): Promise<void> {
  await apiFetch(`/api/v1/integrations/${id}/disconnect/`, {
    method: "POST",
  });
}

export function getOAuthAuthorizeUrl(provider: string): Promise<{ url: string }> {
  return apiFetch<{ url: string }>(`/api/v1/integrations/authorize/${provider}/`);
}

// Billing
export function requestStripePortal(): Promise<{ url: string }> {
  return apiFetch<{ url: string }>("/api/v1/billing/portal/", { method: "POST" });
}

export function requestStripeCheckout(): Promise<{ url: string }> {
  return apiFetch<{ url: string }>("/api/v1/billing/checkout/", {
    method: "POST",
  });
}

export function fetchTransparency(): Promise<TransparencyData> {
  return apiFetch<TransparencyData>("/api/v1/billing/usage/transparency/");
}

export function updateDonationPreference(data: {
  donation_enabled?: boolean;
  donation_percentage?: number;
}): Promise<{ donation_enabled: boolean; donation_percentage: number }> {
  return apiFetch("/api/v1/billing/donation-preference/", {
    method: "PATCH",
    body: JSON.stringify(data),
  });
}

export function updatePreferredModel(preferred_model: string): Promise<{ preferred_model: string; model_tier: string }> {
  return apiFetch("/api/v1/tenants/settings/preferred-model/", {
    method: "PATCH",
    body: JSON.stringify({ preferred_model }),
  });
}

export function updateTaskModelPreferences(
  prefs: Record<string, string>,
): Promise<{ task_model_preferences: Record<string, string> }> {
  return apiFetch("/api/v1/tenants/settings/task-model-preferences/", {
    method: "PATCH",
    body: JSON.stringify({ task_model_preferences: prefs }),
  });
}

// Automations
type AutomationResponse = Automation[] | { results?: Automation[] };

export interface AutomationInput {
  kind: "daily_brief" | "weekly_review";
  status?: "active" | "paused";
  timezone: string;
  schedule_type: "daily" | "weekly";
  schedule_time: string;
  schedule_days?: number[];
}

export interface PaginatedResponse<T> {
  count: number;
  next: string | null;
  previous: string | null;
  results: T[];
}

export async function fetchAutomations(): Promise<Automation[]> {
  const data = await apiFetch<AutomationResponse>("/api/v1/automations/");
  if (Array.isArray(data)) {
    return data;
  }
  return data.results ?? [];
}

export function createAutomation(data: AutomationInput): Promise<Automation> {
  return apiFetch<Automation>("/api/v1/automations/", {
    method: "POST",
    body: JSON.stringify(data),
  });
}

export function updateAutomation(id: string, data: Partial<AutomationInput>): Promise<Automation> {
  return apiFetch<Automation>(`/api/v1/automations/${id}/`, {
    method: "PATCH",
    body: JSON.stringify(data),
  });
}

export function deleteAutomation(id: string): Promise<void> {
  return apiFetch<void>(`/api/v1/automations/${id}/`, { method: "DELETE" });
}

export function pauseAutomation(id: string): Promise<Automation> {
  return apiFetch<Automation>(`/api/v1/automations/${id}/pause/`, { method: "POST" });
}

export function resumeAutomation(id: string): Promise<Automation> {
  return apiFetch<Automation>(`/api/v1/automations/${id}/resume/`, { method: "POST" });
}

export function runAutomationNow(id: string): Promise<AutomationRun> {
  return apiFetch<AutomationRun>(`/api/v1/automations/${id}/run/`, { method: "POST" });
}

export function fetchAutomationRuns(): Promise<PaginatedResponse<AutomationRun>> {
  return apiFetch<PaginatedResponse<AutomationRun>>("/api/v1/automations/runs/");
}

export function fetchAutomationRunsForAutomation(id: string): Promise<PaginatedResponse<AutomationRun>> {
  return apiFetch<PaginatedResponse<AutomationRun>>(`/api/v1/automations/${id}/runs/`);
}

// Journal (legacy)
/** @deprecated Use DailyNote API instead. */
export interface JournalEntryInput {
  date: string;
  mood: string;
  energy: JournalEntryEnergy;
  wins: string[];
  challenges: string[];
  reflection: string;
}

/** @deprecated */
export function fetchJournalEntries(
  params?: { date_from?: string; date_to?: string },
): Promise<JournalEntry[]> {
  const searchParams = new URLSearchParams();
  if (params?.date_from) searchParams.set("date_from", params.date_from);
  if (params?.date_to) searchParams.set("date_to", params.date_to);
  const query = searchParams.toString();
  return apiFetch<JournalEntry[]>(`/api/v1/journal/${query ? `?${query}` : ""}`);
}

/** @deprecated */
export function createJournalEntry(data: JournalEntryInput): Promise<JournalEntry> {
  return apiFetch<JournalEntry>("/api/v1/journal/", {
    method: "POST",
    body: JSON.stringify(data),
  });
}

/** @deprecated */
export function updateJournalEntry(
  id: string,
  data: Partial<JournalEntryInput>,
): Promise<JournalEntry> {
  return apiFetch<JournalEntry>(`/api/v1/journal/${id}/`, {
    method: "PATCH",
    body: JSON.stringify(data),
  });
}

/** @deprecated */
export function deleteJournalEntry(id: string): Promise<void> {
  return apiFetch<void>(`/api/v1/journal/${id}/`, { method: "DELETE" });
}

// Templates
export interface NoteTemplateInput {
  slug: string;
  name: string;
  sections: NoteTemplateSection[];
  is_default?: boolean;
}

export function fetchTemplates(): Promise<NoteTemplate[]> {
  return apiFetch<NoteTemplate[]>("/api/v1/journal/templates/");
}

export function createTemplate(data: NoteTemplateInput): Promise<NoteTemplate> {
  return apiFetch<NoteTemplate>("/api/v1/journal/templates/", {
    method: "POST",
    body: JSON.stringify(data),
  });
}

export function updateTemplate(
  id: string,
  data: Partial<NoteTemplateInput>,
): Promise<NoteTemplate> {
  return apiFetch<NoteTemplate>(`/api/v1/journal/templates/${id}/`, {
    method: "PATCH",
    body: JSON.stringify(data),
  });
}

export function deleteTemplate(id: string): Promise<void> {
  return apiFetch<void>(`/api/v1/journal/templates/${id}/`, { method: "DELETE" });
}

// Weekly Reviews
export type WeeklyReviewInput = {
  week_start: string;
  week_end: string;
  mood_summary: string;
  top_wins: string[];
  top_challenges: string[];
  lessons: string[];
  week_rating: string;
  intentions_next_week: string[];
};

export function fetchWeeklyReviews(): Promise<WeeklyReview[]> {
  return apiFetch<WeeklyReview[]>("/api/v1/journal/reviews/");
}

export function fetchWeeklyReview(id: string): Promise<WeeklyReview> {
  return apiFetch<WeeklyReview>(`/api/v1/journal/reviews/${id}/`);
}

export function createWeeklyReview(data: WeeklyReviewInput): Promise<WeeklyReview> {
  return apiFetch<WeeklyReview>("/api/v1/journal/reviews/", {
    method: "POST",
    body: JSON.stringify(data),
  });
}

export function updateWeeklyReview(id: string, data: Partial<WeeklyReviewInput>): Promise<WeeklyReview> {
  return apiFetch<WeeklyReview>(`/api/v1/journal/reviews/${id}/`, {
    method: "PATCH",
    body: JSON.stringify(data),
  });
}

export function deleteWeeklyReview(id: string): Promise<void> {
  return apiFetch<void>(`/api/v1/journal/reviews/${id}/`, { method: "DELETE" });
}

// Lessons / constellation API
export function fetchLessons(status?: string): Promise<Lesson[]> {
  const query = status ? `?status=${encodeURIComponent(status)}` : "";
  return apiFetch<Lesson[]>(`/api/v1/lessons/${query}`);
}

export function fetchPendingLessons(): Promise<Lesson[]> {
  return apiFetch<Lesson[]>("/api/v1/lessons/pending/");
}

export function approveLesson(id: number): Promise<Lesson> {
  return apiFetch<Lesson>(`/api/v1/lessons/${id}/approve/`, {
    method: "PATCH",
  });
}

export function dismissLesson(id: number): Promise<Lesson> {
  return apiFetch<Lesson>(`/api/v1/lessons/${id}/dismiss/`, {
    method: "PATCH",
  });
}

export function deleteLesson(id: number): Promise<void> {
  return apiFetch<void>(`/api/v1/lessons/${id}/`, { method: "DELETE" });
}

export function fetchConstellation(): Promise<ConstellationData> {
  return apiFetch<ConstellationData>("/api/v1/lessons/constellation/").then((data) => ({
    ...data,
    affinity_edges: data.affinity_edges ?? [],
  }));
}

/** GET /api/v1/lessons/galaxy/ — the game client's star map (auth handled by apiFetch). */
export function fetchGalaxy(): Promise<import("@/lib/constellation-game/encounter-logic").GalaxyData> {
  return apiFetch<import("@/lib/constellation-game/encounter-logic").GalaxyData>("/api/v1/lessons/galaxy/");
}


// ── Journal v2 Documents ──────────────────────────────────────────────

export function fetchDocument(kind: string, slug: string): Promise<DocumentResponse | null> {
  return apiFetch<DocumentResponse>(`/api/v1/journal/documents/${kind}/${slug}/`).catch((err) => {
    if (err && typeof err === "object" && "status" in err && (err as { status: number }).status === 404) {
      return null;
    }
    throw err;
  });
}

export function fetchDocuments(kind?: string): Promise<DocumentListItem[]> {
  const query = kind ? `?kind=${kind}` : "";
  return apiFetch<DocumentListItem[]>(`/api/v1/journal/documents/${query}`);
}

export function updateDocument(kind: string, slug: string, data: { markdown?: string; title?: string }): Promise<DocumentResponse> {
  return apiFetch<DocumentResponse>(`/api/v1/journal/documents/${kind}/${slug}/`, {
    method: "PATCH",
    body: JSON.stringify(data),
  });
}

export function appendToDocument(kind: string, slug: string, content: string): Promise<DocumentResponse> {
  return apiFetch<DocumentResponse>(`/api/v1/journal/documents/${kind}/${slug}/append/`, {
    method: "POST",
    body: JSON.stringify({ content }),
  });
}

export function fetchToday(): Promise<DocumentResponse> {
  return apiFetch<DocumentResponse>("/api/v1/journal/today/");
}

export function fetchSidebarTree(): Promise<SidebarSection[]> {
  return apiFetch<SidebarSection[]>("/api/v1/journal/tree/");
}

export function createDocument(data: { kind: string; slug: string; title: string; markdown?: string }): Promise<DocumentResponse> {
  return apiFetch<DocumentResponse>("/api/v1/journal/documents/", {
    method: "POST",
    body: JSON.stringify(data),
  });
}

export function deleteDocument(kind: string, slug: string): Promise<void> {
  return apiFetch<void>(`/api/v1/journal/documents/${kind}/${slug}/`, { method: "DELETE" });
}

export function clearDocument(kind: string, slug: string): Promise<DocumentResponse> {
  return apiFetch<DocumentResponse>(`/api/v1/journal/documents/${kind}/${slug}/clear/`, { method: "POST" });
}


// Cron Jobs (scheduled tasks managed via OpenClaw Gateway)
function normalizeCronJob(raw: Record<string, unknown>): CronJob {
  const schedule = (raw.schedule as Partial<CronJobSchedule>) ?? {};
  const payload = (raw.payload as Partial<CronJobPayload>) ?? {};
  const delivery = (raw.delivery as Partial<CronJobDelivery>) ?? {};
  return {
    jobId: (raw.jobId as string) ?? (raw.id as string) ?? undefined,
    name: (raw.name as string) ?? "Untitled",
    schedule: { kind: schedule.kind ?? "cron", expr: schedule.expr ?? "", tz: schedule.tz ?? "UTC" },
    sessionTarget: (raw.sessionTarget as string) ?? "isolated",
    payload: { kind: payload.kind ?? "agentTurn", message: payload.message ?? String((raw.payload as Record<string, unknown>)?.text ?? "") },
    delivery: { mode: delivery.mode ?? "none", channel: delivery.channel },
    enabled: (raw.enabled as boolean) ?? false,
    foreground: (raw.foreground as boolean | undefined) ?? true,
  };
}

export async function fetchCronJobs(): Promise<CronJob[]> {
  const data = await apiFetch<{ jobs?: unknown[] }>("/api/v1/cron-jobs/");
  const rawJobs = data.jobs ?? (Array.isArray(data) ? data : []);
  return rawJobs.map((j) => normalizeCronJob(j as Record<string, unknown>));
}

export function createCronJob(data: Partial<CronJob>): Promise<CronJob> {
  return apiFetch<CronJob>("/api/v1/cron-jobs/", {
    method: "POST",
    body: JSON.stringify(data),
  });
}

export function updateCronJob(nameOrId: string, data: Partial<CronJob>): Promise<CronJob> {
  return apiFetch<CronJob>(`/api/v1/cron-jobs/${encodeURIComponent(nameOrId)}/`, {
    method: "PATCH",
    body: JSON.stringify(data),
  });
}

export function deleteCronJob(nameOrId: string): Promise<void> {
  return apiFetch<void>(`/api/v1/cron-jobs/${encodeURIComponent(nameOrId)}/`, {
    method: "DELETE",
  });
}

export function toggleCronJob(nameOrId: string, enabled: boolean): Promise<CronJob> {
  return apiFetch<CronJob>(`/api/v1/cron-jobs/${encodeURIComponent(nameOrId)}/toggle/`, {
    method: "POST",
    body: JSON.stringify({ enabled }),
  });
}

export interface BulkDeleteResult {
  deleted: number;
  errors: number;
  results: Array<{ id: string; deleted: boolean; error?: string }>;
}

export function bulkDeleteCronJobs(ids: string[]): Promise<BulkDeleteResult> {
  return apiFetch<BulkDeleteResult>("/api/v1/cron-jobs/bulk-delete/", {
    method: "POST",
    body: JSON.stringify({ ids }),
  });
}

export interface BulkUpdateForegroundResult {
  updated: number;
  errors: number;
  results: Array<{ id: string; updated: boolean; skipped?: boolean; error?: string }>;
}

export function bulkUpdateForeground(ids: string[], foreground: boolean): Promise<BulkUpdateForegroundResult> {
  return apiFetch<BulkUpdateForegroundResult>("/api/v1/cron-jobs/bulk-update-foreground/", {
    method: "POST",
    body: JSON.stringify({ ids, foreground }),
  });
}

// Pending one-off reminders (schedule.kind === "at"). Always fetched from
// the gateway; lives outside the canonical-tenant Postgres read path.
function normalizePendingReminder(raw: Record<string, unknown>): PendingReminder {
  const schedule = (raw.schedule as Partial<CronJobSchedule>) ?? {};
  const payload = (raw.payload as Partial<CronJobPayload>) ?? {};
  const delivery = (raw.delivery as Partial<CronJobDelivery>) ?? {};
  return {
    jobId: (raw.jobId as string) ?? undefined,
    name: (raw.name as string) ?? "Untitled",
    firesAtMs: typeof raw.firesAtMs === "number" ? raw.firesAtMs : null,
    schedule: { kind: schedule.kind ?? "at", expr: schedule.expr ?? "", tz: schedule.tz ?? "UTC" },
    payload: { kind: payload.kind ?? "agentTurn", message: payload.message ?? "" },
    delivery: { mode: delivery.mode ?? "none", channel: delivery.channel },
  };
}

export async function fetchPendingReminders(): Promise<PendingRemindersResponse> {
  const data = await apiFetch<{ jobs?: unknown[]; soft_cap?: number; stale?: boolean }>(
    "/api/v1/cron-jobs/pending-at/",
  );
  const rawJobs = Array.isArray(data.jobs) ? data.jobs : [];
  return {
    jobs: rawJobs.map((j) => normalizePendingReminder(j as Record<string, unknown>)),
    soft_cap: typeof data.soft_cap === "number" ? data.soft_cap : 20,
    stale: Boolean(data.stale),
  };
}

export function cancelPendingReminder(name: string): Promise<void> {
  return apiFetch<void>(`/api/v1/cron-jobs/pending-at/${encodeURIComponent(name)}/`, {
    method: "DELETE",
  });
}

export interface DeleteAccountResponse {
  scheduled: boolean;
  deletion_scheduled_at?: string | null;
  detail: string;
}

export function deleteAccount(): Promise<DeleteAccountResponse> {
  return apiFetch<DeleteAccountResponse>("/api/v1/tenants/delete-account/", {
    method: "POST",
    body: JSON.stringify({ confirm: "DELETE" }),
  });
}

export function cancelAccountDeletion(): Promise<{ detail: string }> {
  return apiFetch<{ detail: string }>("/api/v1/tenants/cancel-deletion/", {
    method: "POST",
  });
}

// Working Hours
export function fetchWorkingHours(): Promise<import("@/lib/types").WorkingHoursConfig> {
  return apiFetch<import("@/lib/types").WorkingHoursConfig>("/api/v1/tenants/heartbeat/");
}

export function updateWorkingHours(data: { enabled?: boolean; start_hour?: number; feature_tips?: boolean }): Promise<import("@/lib/types").WorkingHoursConfig> {
  return apiFetch<import("@/lib/types").WorkingHoursConfig>("/api/v1/tenants/heartbeat/", {
    method: "PATCH",
    body: JSON.stringify(data),
  });
}

// Finance
export function fetchFinanceDashboard(): Promise<import("@/lib/types").FinanceDashboardData> {
  return apiFetch<import("@/lib/types").FinanceDashboardData>("/api/v1/finance/dashboard/");
}

export function fetchFinanceAccounts(): Promise<import("@/lib/types").FinanceAccount[]> {
  return apiFetch<import("@/lib/types").FinanceAccount[]>("/api/v1/finance/accounts/");
}

export function fetchArchivedFinanceAccounts(): Promise<
  import("@/lib/types").FinanceAccount[]
> {
  return apiFetch<import("@/lib/types").FinanceAccount[]>(
    "/api/v1/finance/accounts/?archived=true",
  );
}

export function unarchiveFinanceAccount(
  id: string,
): Promise<import("@/lib/types").FinanceAccount> {
  return apiFetch<import("@/lib/types").FinanceAccount>(
    `/api/v1/finance/accounts/${id}/`,
    {
      method: "PATCH",
      body: JSON.stringify({ is_active: true }),
    },
  );
}

export function createFinanceAccount(data: {
  nickname: string;
  account_type: string;
  current_balance: number;
  interest_rate?: number;
  minimum_payment?: number;
  credit_limit?: number;
  due_day?: number;
}): Promise<import("@/lib/types").FinanceAccount> {
  return apiFetch<import("@/lib/types").FinanceAccount>("/api/v1/finance/accounts/", {
    method: "POST",
    body: JSON.stringify(data),
  });
}

export function updateFinanceAccount(
  id: string,
  data: Partial<{
    nickname: string;
    account_type: string;
    current_balance: number;
    interest_rate: number;
    minimum_payment: number;
  }>,
): Promise<import("@/lib/types").FinanceAccount> {
  return apiFetch<import("@/lib/types").FinanceAccount>(`/api/v1/finance/accounts/${id}/`, {
    method: "PATCH",
    body: JSON.stringify(data),
  });
}

export function deleteFinanceAccount(id: string): Promise<void> {
  return apiFetch<void>(`/api/v1/finance/accounts/${id}/`, {
    method: "DELETE",
  });
}

export function fetchPayoffPlans(): Promise<import("@/lib/types").PayoffPlan[]> {
  return apiFetch<import("@/lib/types").PayoffPlan[]>("/api/v1/finance/payoff-plans/");
}

export function fetchFinanceSnapshots(): Promise<import("@/lib/types").FinanceSnapshot[]> {
  return apiFetch<import("@/lib/types").FinanceSnapshot[]>("/api/v1/finance/snapshots/");
}

export function updateFinanceSettings(
  data: { finance_enabled: boolean },
): Promise<{ finance_enabled: boolean; restart_required: boolean }> {
  return apiFetch<{ finance_enabled: boolean; restart_required: boolean }>("/api/v1/finance/settings/", {
    method: "PATCH",
    body: JSON.stringify(data),
  });
}

export function restartFinanceAssistant(): Promise<{ restarted: boolean }> {
  return apiFetch<{ restarted: boolean }>("/api/v1/finance/restart/", {
    method: "POST",
  });
}

// -- Fuel (Workout Tracking) --

export function fetchFuelCalendar(
  year: number,
  month: number,
): Promise<import("@/lib/types").CalendarDay[]> {
  return apiFetch<import("@/lib/types").CalendarDay[]>(
    `/api/v1/fuel/calendar/?year=${year}&month=${month}`,
  );
}

export function fetchWorkouts(params?: {
  category?: string;
  status?: string;
  date_from?: string;
  date_to?: string;
  limit?: number;
}): Promise<import("@/lib/types").FuelWorkout[]> {
  const qs = new URLSearchParams();
  if (params?.category) qs.set("category", params.category);
  if (params?.status) qs.set("status", params.status);
  if (params?.date_from) qs.set("date_from", params.date_from);
  if (params?.date_to) qs.set("date_to", params.date_to);
  if (params?.limit) qs.set("limit", String(params.limit));
  const suffix = qs.toString() ? `?${qs.toString()}` : "";
  return apiFetch<import("@/lib/types").FuelWorkout[]>(`/api/v1/fuel/workouts/${suffix}`);
}

export function fetchWorkoutCount(params?: {
  status?: string;
  category?: string;
}): Promise<{ count: number }> {
  const qs = new URLSearchParams();
  if (params?.status) qs.set("status", params.status);
  if (params?.category) qs.set("category", params.category);
  const suffix = qs.toString() ? `?${qs.toString()}` : "";
  return apiFetch<{ count: number }>(`/api/v1/fuel/workouts/count/${suffix}`);
}

export function fetchWorkout(id: string): Promise<import("@/lib/types").FuelWorkout> {
  return apiFetch<import("@/lib/types").FuelWorkout>(`/api/v1/fuel/workouts/${id}/`);
}

export function fetchScheduleWindow(window: string = "7d"): Promise<import("@/lib/types").FuelWorkout[]> {
  return apiFetch<import("@/lib/types").FuelWorkout[]>(`/api/v1/fuel/workouts/?window=${encodeURIComponent(window)}`);
}

export function skipWorkout(id: string, reason?: string): Promise<import("@/lib/types").FuelWorkout> {
  return apiFetch<import("@/lib/types").FuelWorkout>(`/api/v1/fuel/workouts/${id}/skip/`, {
    method: "POST",
    body: JSON.stringify({ reason: reason ?? "" }),
  });
}

export interface EditLockResponse {
  workout_id: string;
  edit_lock_until: string;
  edit_lock_owner: string;
  ttl_seconds: number;
  version: number;
}

export function fetchFuelVersion(): Promise<{ fuel_version: number }> {
  return apiFetch<{ fuel_version: number }>("/api/v1/fuel/version/");
}

export function acquireEditLock(workoutId: string): Promise<EditLockResponse> {
  return apiFetch<EditLockResponse>(`/api/v1/fuel/workouts/${workoutId}/edit-lock/`, {
    method: "POST",
  });
}

export function releaseEditLock(workoutId: string): Promise<void> {
  return apiFetch<void>(`/api/v1/fuel/workouts/${workoutId}/edit-lock/`, {
    method: "DELETE",
  });
}

export function completeWorkout(
  id: string,
  data?: { notes?: string; rpe?: number; duration_minutes?: number },
): Promise<import("@/lib/types").FuelWorkout> {
  return apiFetch<import("@/lib/types").FuelWorkout>(`/api/v1/fuel/workouts/${id}/complete/`, {
    method: "POST",
    body: JSON.stringify(data ?? {}),
  });
}

export function swapWorkouts(
  a: string,
  b: string,
): Promise<{ a: import("@/lib/types").FuelWorkout; b: import("@/lib/types").FuelWorkout }> {
  return apiFetch<{ a: import("@/lib/types").FuelWorkout; b: import("@/lib/types").FuelWorkout }>(
    "/api/v1/fuel/workouts/swap/",
    { method: "POST", body: JSON.stringify({ a, b }) },
  );
}

export function createWorkout(
  data: Partial<import("@/lib/types").FuelWorkout>,
): Promise<import("@/lib/types").FuelWorkout> {
  return apiFetch<import("@/lib/types").FuelWorkout>("/api/v1/fuel/workouts/", {
    method: "POST",
    body: JSON.stringify(data),
  });
}

export function updateWorkout(
  id: string,
  data: Partial<import("@/lib/types").FuelWorkout>,
): Promise<import("@/lib/types").FuelWorkout> {
  return apiFetch<import("@/lib/types").FuelWorkout>(`/api/v1/fuel/workouts/${id}/`, {
    method: "PATCH",
    body: JSON.stringify(data),
  });
}

export function deleteWorkout(id: string): Promise<void> {
  return apiFetch<void>(`/api/v1/fuel/workouts/${id}/`, {
    method: "DELETE",
  });
}

export function fetchFuelProgress(
  category: string,
): Promise<{ category: string; progress: Record<string, unknown> }> {
  return apiFetch<{ category: string; progress: Record<string, unknown> }>(
    `/api/v1/fuel/progress/?category=${category}`,
  );
}

export function fetchBodyWeight(): Promise<import("@/lib/types").BodyWeightEntry[]> {
  return apiFetch<import("@/lib/types").BodyWeightEntry[]>("/api/v1/fuel/body-weight/");
}

export function createBodyWeight(data: {
  date: string;
  weight_kg: number;
}): Promise<import("@/lib/types").BodyWeightEntry> {
  return apiFetch<import("@/lib/types").BodyWeightEntry>("/api/v1/fuel/body-weight/", {
    method: "POST",
    body: JSON.stringify(data),
  });
}

export function deleteBodyWeight(id: string): Promise<void> {
  return apiFetch<void>(`/api/v1/fuel/body-weight/${id}/`, {
    method: "DELETE",
  });
}

export function updateBodyWeight(
  id: string,
  data: { date?: string; weight_kg?: number },
): Promise<import("@/lib/types").BodyWeightEntry> {
  return apiFetch<import("@/lib/types").BodyWeightEntry>(`/api/v1/fuel/body-weight/${id}/`, {
    method: "PATCH",
    body: JSON.stringify(data),
  });
}

export function updateFuelSettings(
  data: { fuel_enabled: boolean },
): Promise<{ fuel_enabled: boolean; fuel_profile_status: import("@/lib/types").FuelOnboardingStatus | null; restart_required: boolean }> {
  return apiFetch<{ fuel_enabled: boolean; fuel_profile_status: import("@/lib/types").FuelOnboardingStatus | null; restart_required: boolean }>("/api/v1/fuel/settings/", {
    method: "PATCH",
    body: JSON.stringify(data),
  });
}

export function restartFuelAssistant(): Promise<{ restarted: boolean }> {
  return apiFetch<{ restarted: boolean }>("/api/v1/fuel/restart/", {
    method: "POST",
  });
}

export function fetchFuelProfile(): Promise<import("@/lib/types").FuelProfile> {
  return apiFetch<import("@/lib/types").FuelProfile>("/api/v1/fuel/profile/");
}

export function updateFuelProfile(
  data: Partial<import("@/lib/types").FuelProfile>,
): Promise<import("@/lib/types").FuelProfile> {
  return apiFetch<import("@/lib/types").FuelProfile>("/api/v1/fuel/profile/", {
    method: "PATCH",
    body: JSON.stringify(data),
  });
}

// Templates
export function fetchWorkoutTemplates(category?: string): Promise<import("@/lib/types").WorkoutTemplate[]> {
  const qs = category ? `?category=${category}` : "";
  return apiFetch<import("@/lib/types").WorkoutTemplate[]>(`/api/v1/fuel/templates/${qs}`);
}

export function createWorkoutTemplate(
  data: Partial<import("@/lib/types").WorkoutTemplate>,
): Promise<import("@/lib/types").WorkoutTemplate> {
  return apiFetch<import("@/lib/types").WorkoutTemplate>("/api/v1/fuel/templates/", {
    method: "POST",
    body: JSON.stringify(data),
  });
}

export function deleteWorkoutTemplate(id: string): Promise<void> {
  return apiFetch<void>(`/api/v1/fuel/templates/${id}/`, { method: "DELETE" });
}

export function duplicateWorkout(id: string): Promise<import("@/lib/types").FuelWorkout> {
  return apiFetch<import("@/lib/types").FuelWorkout>(`/api/v1/fuel/workouts/${id}/duplicate/`, { method: "POST" });
}

// Weekly volume
export function fetchWeeklyVolume(weekStart?: string): Promise<{
  week_start: string;
  week_end: string;
  by_category: { category: string; count: number; total_minutes: number | null }[];
  totals: { sessions: number; minutes: number };
}> {
  const qs = weekStart ? `?week_start=${weekStart}` : "";
  return apiFetch(`/api/v1/fuel/weekly-summary/${qs}`);
}

// PRs
export function fetchPRFeed(limit?: number): Promise<import("@/lib/types").PersonalRecord[]> {
  const qs = limit ? `?limit=${limit}` : "";
  return apiFetch<import("@/lib/types").PersonalRecord[]>(`/api/v1/fuel/prs/${qs}`);
}

// Goals
export function fetchFuelGoals(): Promise<import("@/lib/types").FuelGoal[]> {
  return apiFetch<import("@/lib/types").FuelGoal[]>("/api/v1/fuel/goals/");
}

export function createFuelGoal(
  data: Partial<import("@/lib/types").FuelGoal>,
): Promise<import("@/lib/types").FuelGoal> {
  return apiFetch<import("@/lib/types").FuelGoal>("/api/v1/fuel/goals/", {
    method: "POST",
    body: JSON.stringify(data),
  });
}

export function deleteFuelGoal(id: string): Promise<void> {
  return apiFetch<void>(`/api/v1/fuel/goals/${id}/`, { method: "DELETE" });
}

// Resting heart rate
export function fetchRestingHR(): Promise<import("@/lib/types").RestingHeartRateEntry[]> {
  return apiFetch<import("@/lib/types").RestingHeartRateEntry[]>("/api/v1/fuel/resting-hr/");
}

export function createRestingHR(data: { date: string; bpm: number }): Promise<import("@/lib/types").RestingHeartRateEntry> {
  return apiFetch<import("@/lib/types").RestingHeartRateEntry>("/api/v1/fuel/resting-hr/", {
    method: "POST",
    body: JSON.stringify(data),
  });
}

export function updateRestingHR(
  id: string,
  data: { date?: string; bpm?: number },
): Promise<import("@/lib/types").RestingHeartRateEntry> {
  return apiFetch<import("@/lib/types").RestingHeartRateEntry>(`/api/v1/fuel/resting-hr/${id}/`, {
    method: "PATCH",
    body: JSON.stringify(data),
  });
}

export function deleteRestingHR(id: string): Promise<void> {
  return apiFetch<void>(`/api/v1/fuel/resting-hr/${id}/`, { method: "DELETE" });
}

// Sleep
export function fetchSleep(): Promise<import("@/lib/types").SleepEntry[]> {
  return apiFetch<import("@/lib/types").SleepEntry[]>("/api/v1/fuel/sleep/");
}

export function createSleep(data: {
  date: string;
  duration_hours: number;
  quality?: number;
  notes?: string;
}): Promise<import("@/lib/types").SleepEntry> {
  return apiFetch<import("@/lib/types").SleepEntry>("/api/v1/fuel/sleep/", {
    method: "POST",
    body: JSON.stringify(data),
  });
}

export function updateSleep(
  id: string,
  data: { date?: string; duration_hours?: number; quality?: number | null; notes?: string },
): Promise<import("@/lib/types").SleepEntry> {
  return apiFetch<import("@/lib/types").SleepEntry>(`/api/v1/fuel/sleep/${id}/`, {
    method: "PATCH",
    body: JSON.stringify(data),
  });
}

export function deleteSleep(id: string): Promise<void> {
  return apiFetch<void>(`/api/v1/fuel/sleep/${id}/`, { method: "DELETE" });
}

// Personal Access Tokens (Connected Apps)
export function fetchPATs(): Promise<import("@/lib/types").PersonalAccessToken[]> {
  return apiFetch<import("@/lib/types").PersonalAccessToken[]>("/api/v1/auth/tokens/");
}

export function mintPAT(
  data: import("@/lib/types").PATCreateRequest,
): Promise<import("@/lib/types").PATCreateResponse> {
  return apiFetch<import("@/lib/types").PATCreateResponse>("/api/v1/auth/tokens/create/", {
    method: "POST",
    body: JSON.stringify(data),
  });
}

export async function revokePAT(id: string): Promise<void> {
  await apiFetch(`/api/v1/auth/tokens/${id}/`, { method: "DELETE" });
}

// BYO subscription credentials (bring-your-own Anthropic / OpenAI)

export function fetchByoCredentials(): Promise<import("@/lib/types").BYOCredential[]> {
  return apiFetch<import("@/lib/types").BYOCredential[]>("/api/v1/tenants/byo-credentials/");
}

export function connectByoCredential(
  data: import("@/lib/types").BYOConnectRequest,
  signal?: AbortSignal,
): Promise<import("@/lib/types").BYOConnectResponse> {
  return apiFetch<import("@/lib/types").BYOConnectResponse>("/api/v1/tenants/byo-credentials/", {
    method: "POST",
    body: JSON.stringify(data),
    signal,
  });
}

export async function disconnectByoCredential(
  id: string,
  signal?: AbortSignal,
): Promise<void> {
  await apiFetch(`/api/v1/tenants/byo-credentials/${id}/`, {
    method: "DELETE",
    signal,
  });
}

// -- Core (Mindfulness) --

// Compose-on-demand: the web orb. Creates a pending session and enqueues the
// LLM-authors-manifest → render task. Coalesces a mashed orb (returns the
// in-flight session). The caller polls fetchMeditation(id) until ready.
export function composeMeditation(): Promise<import("@/lib/types").CoreComposeResponse> {
  return apiFetch<import("@/lib/types").CoreComposeResponse>("/api/v1/core/compose/", {
    method: "POST",
  });
}

export function fetchMeditation(id: string): Promise<import("@/lib/types").MeditationSession> {
  return apiFetch<import("@/lib/types").MeditationSession>(`/api/v1/core/sessions/${id}/`);
}

// The library. Defaults to ready sessions; the list endpoint is paginated
// (DRF PageNumberPagination), so unwrap `.results`.
export async function fetchMeditations(
  status?: string,
): Promise<import("@/lib/types").MeditationSession[]> {
  const query = status ? `?status=${encodeURIComponent(status)}` : "";
  const data = await apiFetch<
    | import("@/lib/types").MeditationSession[]
    | { results?: import("@/lib/types").MeditationSession[] }
  >(`/api/v1/core/sessions/${query}`);
  if (Array.isArray(data)) return data;
  return data.results ?? [];
}

export function updateCoreSettings(
  data: { core_enabled: boolean },
): Promise<import("@/lib/types").CoreSettingsResponse> {
  return apiFetch<import("@/lib/types").CoreSettingsResponse>("/api/v1/core/settings/", {
    method: "PATCH",
    body: JSON.stringify(data),
  });
}

export function restartCoreAssistant(): Promise<{ restarted: boolean }> {
  return apiFetch<{ restarted: boolean }>("/api/v1/core/restart/", {
    method: "POST",
  });
}

export function fetchCoreProfile(): Promise<import("@/lib/types").CoreProfile> {
  return apiFetch<import("@/lib/types").CoreProfile>("/api/v1/core/profile/");
}

export function updateCoreProfile(
  data: Partial<import("@/lib/types").CoreProfile>,
): Promise<import("@/lib/types").CoreProfile> {
  return apiFetch<import("@/lib/types").CoreProfile>("/api/v1/core/profile/", {
    method: "PATCH",
    body: JSON.stringify(data),
  });
}
