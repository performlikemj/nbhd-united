import { clearTokens, getAccessToken, getRefreshToken, setTokens } from "@/lib/auth";
import {
  AuthUser,
  Automation,
  AutomationRun,
  DailyNoteResponse,
  DashboardData,
  Integration,
  JournalEntry,
  JournalEntryEnergy,
  NoteTemplate,
  NoteTemplateSection,
  Tenant,
  UsageRecord,
  UsageSummary,
  UserMemoryResponse,
  WeeklyReview,
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
    if (typeof window !== "undefined") {
      window.location.href = "/login";
    }
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
      throw new Error("Session expired. Please sign in again.");
    }
  }

  if (response.status === 401) {
    clearTokens();
    if (typeof window !== "undefined") {
      window.location.href = "/login";
    }
    throw new Error("Session expired. Please sign in again.");
  }

  if (!response.ok) {
    const message = await response.text();
    throw new Error(message || `Request failed: ${response.status}`);
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
  inviteCode?: string,
): Promise<{ access: string; refresh: string }> {
  return apiFetch<{ access: string; refresh: string }>("/api/v1/auth/signup/", {
    method: "POST",
    body: JSON.stringify({ email, password, display_name: displayName, invite_code: inviteCode }),
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

export function requestStripeCheckout(tier: string): Promise<{ url: string }> {
  return apiFetch<{ url: string }>("/api/v1/billing/checkout/", {
    method: "POST",
    body: JSON.stringify({ tier }),
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

// Daily Notes
export function fetchDailyNote(date: string): Promise<DailyNoteResponse> {
  return apiFetch<DailyNoteResponse>(`/api/v1/journal/daily/${date}/`);
}

export interface DailyNoteEntryInput {
  content: string;
  mood?: string;
  energy?: number;
  time?: string;
}

export function createDailyNoteEntry(date: string, data: DailyNoteEntryInput): Promise<DailyNoteResponse> {
  return apiFetch<DailyNoteResponse>(`/api/v1/journal/daily/${date}/entries/`, {
    method: "POST",
    body: JSON.stringify(data),
  });
}

export function updateDailyNoteEntry(
  date: string,
  index: number,
  data: Partial<{ content: string; mood: string; energy: number }>,
): Promise<DailyNoteResponse> {
  return apiFetch<DailyNoteResponse>(`/api/v1/journal/daily/${date}/entries/${index}/`, {
    method: "PATCH",
    body: JSON.stringify(data),
  });
}

export function deleteDailyNoteEntry(date: string, index: number): Promise<DailyNoteResponse> {
  return apiFetch<DailyNoteResponse>(`/api/v1/journal/daily/${date}/entries/${index}/`, {
    method: "DELETE",
  });
}

export interface DailyNoteTemplateInput {
  template_id?: string | null;
  markdown: string;
  sections: NoteTemplateSection[];
}

export function updateDailyNoteSection(
  date: string,
  slug: string,
  content: string,
): Promise<DailyNoteResponse> {
  return apiFetch<DailyNoteResponse>(`/api/v1/journal/daily/${date}/sections/${slug}/`, {
    method: "PATCH",
    body: JSON.stringify({ content }),
  });
}

export function updateDailyNoteTemplate(
  date: string,
  data: DailyNoteTemplateInput,
): Promise<DailyNoteResponse> {
  return apiFetch<DailyNoteResponse>(`/api/v1/journal/daily/${date}/template/`, {
    method: "PUT",
    body: JSON.stringify(data),
  });
}

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

// User Memory
export function fetchMemory(): Promise<UserMemoryResponse> {
  return apiFetch<UserMemoryResponse>("/api/v1/journal/memory/");
}

export function updateMemory(markdown: string): Promise<UserMemoryResponse> {
  return apiFetch<UserMemoryResponse>("/api/v1/journal/memory/", {
    method: "PUT",
    body: JSON.stringify({ markdown }),
  });
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
