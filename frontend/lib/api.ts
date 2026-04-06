import { clearTokens, getAccessToken, getRefreshToken, setTokens } from "@/lib/auth";
import {
  AuthUser,
  Automation,
  AutomationRun,
  CronJob,
  CronJobDelivery,
  CronJobPayload,
  CronJobSchedule,
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
    clearTokens();
    if (typeof window !== "undefined" && window.location.pathname !== "/login") {
      window.location.href = "/login";
    }
    throw new Error("Session expired. Please sign in again.");
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

export function approveExtraction(id: string): Promise<{ id: string; status: string }> {
  return apiFetch<{ id: string; status: string }>(`/api/v1/journal/extractions/${id}/approve/`, { method: "POST" });
}

export function dismissExtraction(id: string): Promise<{ id: string; status: string }> {
  return apiFetch<{ id: string; status: string }>(`/api/v1/journal/extractions/${id}/dismiss/`, { method: "POST" });
}

// Tenants
export function fetchTenant(): Promise<Tenant> {
  return apiFetch<Tenant>("/api/v1/tenants/me/");
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

export function fetchConstellation(): Promise<ConstellationData> {
  return apiFetch<ConstellationData>("/api/v1/lessons/constellation/").then((data) => ({
    ...data,
    affinity_edges: data.affinity_edges ?? [],
  }));
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

export function updateFinanceSettings(data: { finance_enabled: boolean }): Promise<{ finance_enabled: boolean }> {
  return apiFetch<{ finance_enabled: boolean }>("/api/v1/finance/settings/", {
    method: "PATCH",
    body: JSON.stringify(data),
  });
}
