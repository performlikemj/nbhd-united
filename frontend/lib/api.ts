import {
  clearTokens,
  getAccessToken,
  getAuthenticationEpoch,
  getRefreshToken,
  setTokens,
} from "@/lib/auth";
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
  CreditsResponse,
} from "@/lib/types";

export const API_BASE =
  process.env.NEXT_PUBLIC_API_BASE_URL ?? "http://localhost:8000";

interface RefreshFlight {
  authenticationEpoch: number;
  refreshToken: string;
  promise: Promise<RefreshedTokens>;
}

interface AuthenticationSnapshot {
  authenticationEpoch: number;
  access: string | null;
  refresh: string | null;
}

interface RefreshedTokens {
  access: string;
  refresh: string;
}

interface ApiFetchOptions {
  anonymous?: boolean;
  timeoutMs?: number;
}

let refreshFlight: RefreshFlight | null = null;

export class AuthenticationSupersededError extends Error {
  constructor() {
    super("Authentication request was superseded.");
    this.name = "AuthenticationSupersededError";
  }
}

export class ApiNetworkError extends Error {
  constructor(
    message = "Couldn't reach the server. Check your connection and try again.",
  ) {
    super(message);
    this.name = "ApiNetworkError";
  }
}

/** Shared, deduped refresh — concurrent callers await the same in-flight request. */
function getDedupedRefresh(
  authenticationEpoch: number,
  refreshToken: string,
): Promise<RefreshedTokens> {
  if (
    refreshFlight &&
    refreshFlight.authenticationEpoch === authenticationEpoch &&
    refreshFlight.refreshToken === refreshToken
  ) {
    return refreshFlight.promise;
  }

  const flight: RefreshFlight = {
    authenticationEpoch,
    refreshToken,
    promise: Promise.resolve({ access: "", refresh: "" }),
  };
  flight.promise = performRefreshAccessToken(
    authenticationEpoch,
    refreshToken,
  ).finally(() => {
    // An older account's completion must not erase a newer account's flight.
    if (refreshFlight === flight) refreshFlight = null;
  });
  refreshFlight = flight;
  return flight.promise;
}

/** Guarded refresh primitive used by API retries and the Apple link 401 leg. */
export async function refreshAccessToken(args: {
  authenticationEpoch: number;
  accessToken: string | null;
}): Promise<string> {
  assertAccessTokenCurrent(args.authenticationEpoch, args.accessToken);
  const refreshToken = getRefreshToken();
  if (!refreshToken) {
    throw new Error("No refresh token available.");
  }
  // Bind the selected refresh to the click-captured access token. A session
  // switch between either read is rejected instead of selecting B's refresh.
  assertAccessTokenCurrent(args.authenticationEpoch, args.accessToken);
  if (getRefreshToken() !== refreshToken) {
    throw new AuthenticationSupersededError();
  }
  const refreshed = await getDedupedRefresh(
    args.authenticationEpoch,
    refreshToken,
  );
  assertAuthenticationSnapshotCurrent({
    authenticationEpoch: args.authenticationEpoch,
    access: refreshed.access,
    refresh: refreshed.refresh,
  });
  return refreshed.access;
}

// Proactively refresh when the access token expires within this window, so
// requests don't burn a round-trip on a guaranteed 401 → refresh → retry.
const TOKEN_EXPIRY_SKEW_MS = 60_000;

/**
 * Best-effort check that the JWT's `exp` is within 60s (or past). Decodes the
 * payload without verification — we only need the claim, the server still
 * validates the signature. Malformed/absent tokens return false so we fall
 * back to the existing 401 → refresh → retry path.
 */
function isTokenExpiringSoon(token: string): boolean {
  try {
    const payload = token.split(".")[1];
    if (!payload) return false;
    const decoded = JSON.parse(
      atob(payload.replace(/-/g, "+").replace(/_/g, "/")),
    ) as { exp?: unknown };
    if (typeof decoded.exp !== "number") return false;
    return decoded.exp * 1000 - Date.now() < TOKEN_EXPIRY_SKEW_MS;
  } catch {
    return false;
  }
}

async function performRefreshAccessToken(
  authenticationEpoch: number,
  refresh: string,
): Promise<RefreshedTokens> {
  const response = await fetch(`${API_BASE}/api/v1/auth/refresh/`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ refresh }),
  });
  assertRefreshStillCurrent(authenticationEpoch, refresh);

  if (!response.ok) {
    throw new Error("Session expired. Please sign in again.");
  }

  const data = (await response.json()) as {
    access?: unknown;
    refresh?: unknown;
  };
  assertRefreshStillCurrent(authenticationEpoch, refresh);
  if (typeof data.access !== "string" || !data.access) {
    throw new Error("Session expired. Please sign in again.");
  }
  // With ROTATE_REFRESH_TOKENS the response carries a NEW refresh token and the
  // presented one is blacklisted on use — persist the rotated one or the next
  // refresh dies. Falls back to the old token when rotation is off (no `refresh`
  // field), so this is safe regardless of the server setting.
  const nextRefresh =
    typeof data.refresh === "string" ? data.refresh : refresh;
  setTokens(data.access, nextRefresh);
  return { access: data.access, refresh: nextRefresh };
}

async function apiFetch<T>(
  path: string,
  init?: RequestInit,
  options: ApiFetchOptions = {},
): Promise<T> {
  const { anonymous = false, timeoutMs } = options;
  const timeoutController = timeoutMs === undefined
    ? undefined
    : new AbortController();
  const timeoutId = timeoutController
    ? setTimeout(() => timeoutController.abort(), timeoutMs)
    : undefined;

  try {
    return await apiFetchRequest<T>(
      path,
      init,
      anonymous,
      timeoutController,
    );
  } finally {
    if (timeoutId !== undefined) clearTimeout(timeoutId);
  }
}

async function apiFetchRequest<T>(
  path: string,
  init: RequestInit | undefined,
  anonymous: boolean,
  timeoutController: AbortController | undefined,
): Promise<T> {
  let requestSession: AuthenticationSnapshot = anonymous
    ? { authenticationEpoch: -1, access: null, refresh: null }
    : captureAuthenticationSnapshot();
  let accessToken = requestSession.access;

  // Proactive refresh: if the token is expired or about to expire, await the
  // (deduped) refresh before firing. Fail open on any error — the reactive
  // 401 → refresh → retry below still covers us.
  const proactiveRefresh = requestSession.refresh;
  if (
    !anonymous &&
    accessToken &&
    proactiveRefresh &&
    isTokenExpiringSoon(accessToken)
  ) {
    try {
      assertAuthenticationSnapshotCurrent(requestSession);
      const refreshed = await getDedupedRefresh(
        requestSession.authenticationEpoch,
        proactiveRefresh,
      );
      requestSession = {
        authenticationEpoch: requestSession.authenticationEpoch,
        access: refreshed.access,
        refresh: refreshed.refresh,
      };
      assertAuthenticationSnapshotCurrent(requestSession);
      accessToken = refreshed.access;
    } catch (error) {
      if (error instanceof AuthenticationSupersededError) throw error;
      assertAuthenticationSnapshotCurrent(requestSession);
      // Proceed with the stale token; the 401 handler decides what to do.
    }
  }

  const headers: Record<string, string> = {
    "Content-Type": "application/json",
    ...(init?.headers as Record<string, string> ?? {}),
  };

  if (anonymous) {
    for (const name of Object.keys(headers)) {
      if (name.toLowerCase() === "authorization") delete headers[name];
    }
  } else if (accessToken) {
    headers["Authorization"] = `Bearer ${accessToken}`;
  }

  // When timeoutMs is set, it owns the request signal.
  const requestInit: RequestInit = {
    ...init,
    headers,
    signal: timeoutController?.signal ?? init?.signal,
  };

  let response = await fetchApiResponse(path, requestInit);

  const reactiveRefresh = requestSession.refresh;
  if (!anonymous && response.status === 401 && reactiveRefresh) {
    try {
      assertAuthenticationSnapshotCurrent(requestSession);
      const refreshed = await getDedupedRefresh(
        requestSession.authenticationEpoch,
        reactiveRefresh,
      );
      requestSession = {
        authenticationEpoch: requestSession.authenticationEpoch,
        access: refreshed.access,
        refresh: refreshed.refresh,
      };
      // Never replay Account A's request after Account B takes the session.
      assertAuthenticationSnapshotCurrent(requestSession);

      headers["Authorization"] = `Bearer ${refreshed.access}`;
      response = await fetchApiResponse(path, requestInit);
    } catch (error) {
      if (
        error instanceof AuthenticationSupersededError ||
        error instanceof ApiNetworkError
      ) {
        throw error;
      }
      assertAuthenticationSnapshotCurrent(requestSession);
      // Fall through — response is still the original 401
    }
  }

  if (!anonymous && response.status === 401) {
    // A stale request may observe a 401 after another account has signed in.
    // That request must not clear or redirect the replacement session.
    assertAuthenticationSnapshotCurrent(requestSession);
    // Distinguish "your session expired" from "those credentials don't work".
    // The first case requires a prior refresh token; the second is the
    // backend rejecting login / signup / password-reset-confirm payloads.
    // Showing "Session expired" on a fresh login attempt was confusing —
    // see PR fixing #696-adjacent UX.
    const hadSession = requestSession.refresh !== null;
    if (!clearTokens(requestSession.refresh)) {
      throw new AuthenticationSupersededError();
    }
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

async function fetchApiResponse(
  path: string,
  init: RequestInit,
): Promise<Response> {
  try {
    return await fetch(`${API_BASE}${path}`, init);
  } catch (error) {
    if (
      error instanceof TypeError ||
      (typeof error === "object" &&
        error !== null &&
        "name" in error &&
        error.name === "AbortError")
    ) {
      throw new ApiNetworkError();
    }
    throw error;
  }
}

function assertRefreshStillCurrent(
  expectedEpoch: number,
  expectedRefresh: string,
): void {
  if (
    getAuthenticationEpoch() !== expectedEpoch ||
    getRefreshToken() !== expectedRefresh
  ) {
    throw new AuthenticationSupersededError();
  }
}

function captureAuthenticationSnapshot(): AuthenticationSnapshot {
  return {
    authenticationEpoch: getAuthenticationEpoch(),
    access: getAccessToken(),
    refresh: getRefreshToken(),
  };
}

function assertAuthenticationSnapshotCurrent(
  expected: AuthenticationSnapshot,
): void {
  if (
    getAccessToken() !== expected.access ||
    getRefreshToken() !== expected.refresh ||
    getAuthenticationEpoch() !== expected.authenticationEpoch
  ) {
    throw new AuthenticationSupersededError();
  }
}

function assertAccessTokenCurrent(
  expectedEpoch: number,
  expectedAccess: string | null,
): void {
  if (
    getAccessToken() !== expectedAccess ||
    getAuthenticationEpoch() !== expectedEpoch
  ) {
    throw new AuthenticationSupersededError();
  }
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
  return apiFetch<{ detail: string }>(
    "/api/v1/auth/password-reset/request/",
    {
      method: "POST",
      body: JSON.stringify({ email }),
    },
    {
      anonymous: true,
      timeoutMs: 20_000,
    },
  );
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
    {
      anonymous: true,
      timeoutMs: 20_000,
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

// First page of the existing cross-channel chat feed. Keep the response
// unknown here so the welcome-card selector can fail closed if the iOS-owned
// wire shape changes or a field is absent.
export function fetchChatMessagesFirstPage(): Promise<unknown> {
  return apiFetch<unknown>("/api/v1/chat/messages/");
}

// Journal current-status projection — live state derived from typed models
// + the finance ledger (never a stale baked copy). See status_projection.py.
export function fetchJournalStatus(): Promise<import("@/lib/types").JournalStatus> {
  // Bypass the browser HTTP cache. The API applies a default
  // `Cache-Control: private, max-age=10, stale-while-revalidate=60`
  // (config/cache_middleware.py) to GET reads. For this mutation-sensitive
  // status list that meant a task just completed via POST .../complete/
  // reappeared for up to 10s, because React Query's post-mutation refetch
  // was served the stale cached body instead of hitting the server. React
  // Query's own `staleTime` (60s) is the real request dedup here, so
  // `no-store` only forces freshness on the refetches that actually fire.
  return apiFetch<import("@/lib/types").JournalStatus>("/api/v1/journal/status/", {
    cache: "no-store",
  });
}

// Typed-task writes — update the row, not synthesized markdown (which GET
// re-derives and the backend now rejects via 409). See lifecycle_views.py.
export function completeTask(taskId: string): Promise<unknown> {
  return apiFetch<unknown>(`/api/v1/journal/tasks/${taskId}/complete/`, { method: "POST" });
}

export function reopenTask(taskId: string): Promise<unknown> {
  return apiFetch<unknown>(`/api/v1/journal/tasks/${taskId}/reopen/`, { method: "POST" });
}

export function approveExtraction(id: string): Promise<{ id: string; status: string }> {
  return apiFetch<{ id: string; status: string }>(`/api/v1/journal/extractions/${id}/approve/`, { method: "POST" });
}

export function dismissExtraction(id: string): Promise<{ id: string; status: string }> {
  return apiFetch<{ id: string; status: string }>(`/api/v1/journal/extractions/${id}/dismiss/`, { method: "POST" });
}

// North Star (Purpose) — the direction above goals. User-authored purposes are
// created confirmed; confirming/retiring an assistant-proposed one is a status
// PATCH. All invalidate the Horizons query so the North Star card updates.
export function createPurpose(
  statement: string,
  pillars?: string[],
): Promise<import("@/lib/types").HorizonsNorthStar> {
  return apiFetch<import("@/lib/types").HorizonsNorthStar>("/api/v1/journal/purposes/", {
    method: "POST",
    body: JSON.stringify({ statement, pillars: pillars ?? [] }),
    headers: { "Content-Type": "application/json" },
  });
}

export function updatePurpose(
  id: string,
  patch: { statement?: string; status?: import("@/lib/types").NorthStarStatus; pillars?: string[] },
): Promise<import("@/lib/types").HorizonsNorthStar> {
  return apiFetch<import("@/lib/types").HorizonsNorthStar>(`/api/v1/journal/purposes/${id}/`, {
    method: "PATCH",
    body: JSON.stringify(patch),
    headers: { "Content-Type": "application/json" },
  });
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
export async function fetchTenant(): Promise<Tenant> {
  const me = await fetchMe();
  if (!me.tenant) {
    throw new Error("No tenant found. Complete onboarding first.");
  }
  return me.tenant;
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

export interface EntityRegistryAddResult {
  placeholder: string;
  name: string;
  relationship: string;
  notes: string;
  // True when the name had been on the Ignore list and adding it here cleared
  // that deny key (newest user intent wins).
  denylist_removed: boolean;
}

export interface AddEntityRegistryInput {
  name: string;
  entity_type?: "PERSON" | "LOCATION";
  relationship?: string;
  notes?: string;
  acknowledge_warning?: boolean;
}

// Manually bind a known entity, mirroring the redactor's minting. A brand-new
// name mints the next placeholder off the same per-type high-water counter
// (201 "created"); an already-bound name (case-insensitive canonical match)
// returns its existing placeholder with relationship/notes merged (200
// "exists"). Either way the name is removed from the Ignore list if present —
// reflected in `denylist_removed`.
//
// A 422 {warning} is an EXPECTED branch, not a failure: the hygiene heuristics
// flag a probable common-word/fragment footgun and want the user to confirm.
// Like apiFetchStatus's 202/409 handling we model it as a returned value (so
// the caller can swap into a confirm state and retry with
// acknowledge_warning=true) rather than a thrown Error. Only 400 validation
// and unexpected statuses throw.
export type AddEntityRegistryResponse =
  | { status: "created" | "exists"; entry: EntityRegistryAddResult }
  | { status: "warning"; warning: string };

export async function addEntityRegistryEntry(
  input: AddEntityRegistryInput,
): Promise<AddEntityRegistryResponse> {
  const { status, data } = await apiFetchStatus<
    EntityRegistryAddResult & { warning?: string; detail?: string }
  >("/api/v1/tenants/settings/entity-registry/", {
    method: "POST",
    body: JSON.stringify(input),
  });

  if (status === 200 || status === 201) {
    return {
      status: status === 201 ? "created" : "exists",
      entry: {
        placeholder: data.placeholder,
        name: data.name,
        relationship: data.relationship ?? "",
        notes: data.notes ?? "",
        denylist_removed: Boolean(data.denylist_removed),
      },
    };
  }

  if (status === 422) {
    return {
      status: "warning",
      warning:
        data?.warning ??
        "This looks like a common word or fragment. Hiding it may redact ordinary text.",
    };
  }

  const message = data?.detail ?? data?.warning ?? `Request failed: ${status}`;
  const err = new Error(message);
  (err as Error & { status: number }).status = status;
  throw err;
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

export interface EntityRegistryBulkDeleteResult {
  deleted: string[];
  not_found: string[];
  denied: string[];
}

// Bulk-delete entity-registry bindings by placeholder. Deletion alone only
// removes the binding — the redactor re-mints a fresh placeholder the next
// time it detects the same name — so `deny: true` also adds each deleted
// binding's name to the Ignore list (denylist), which is what actually stops
// future redaction. Partial-success contract mirrors the denylist bulk-add:
// unknown placeholders come back in `not_found`; names added to the denylist
// come back in `denied`.
export function bulkDeleteEntityRegistryEntries(
  placeholders: string[],
  deny: boolean,
): Promise<EntityRegistryBulkDeleteResult> {
  return apiFetch<EntityRegistryBulkDeleteResult>(
    "/api/v1/tenants/settings/entity-registry/bulk/",
    {
      method: "POST",
      body: JSON.stringify({ placeholders, deny }),
    },
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

// Tier-2 PII review queue — the PERSON/LOCATION bindings the assistant is
// hiding that the user hasn't judged yet. "Keep" stamps the entry as reviewed
// (drops it from the queue); "clean" reuses bulkDeleteEntityRegistryEntries
// with deny=true. `total` is the full unreviewed backlog; `entries` is the
// newest-first page (capped server-side) so the card can say "hiding N values".
// This is also the contract the iOS on-device review flow consumes.
export interface PIIReviewQueueEntry {
  placeholder: string;
  name: string;
  relationship: string;
  notes: string;
}

export interface PIIReviewQueue {
  entries: PIIReviewQueueEntry[];
  total: number;
}

export function fetchPIIReviewQueue(): Promise<PIIReviewQueue> {
  return apiFetch<PIIReviewQueue>("/api/v1/tenants/settings/pii-review-queue/");
}

export interface PIIReviewKeepResult {
  kept: string[];
  not_found: string[];
}

export function keepPIIReviewEntries(placeholders: string[]): Promise<PIIReviewKeepResult> {
  return apiFetch<PIIReviewKeepResult>("/api/v1/tenants/settings/pii-review-queue/keep/", {
    method: "POST",
    body: JSON.stringify({ placeholders }),
  });
}

export function onboardTenant(data: { display_name?: string; language?: string; agent_persona?: string; invite_token?: string }): Promise<Tenant> {
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

// Push registration status (token-free)
export interface PushStatus {
  registered: boolean;
}

export function fetchPushStatus(): Promise<PushStatus> {
  return apiFetch<PushStatus>("/api/v1/push/status/");
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
  // UI can disable the LINE connect action when the cap is hit and show
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

export function fetchCredits(): Promise<CreditsResponse> {
  return apiFetch<CreditsResponse>("/api/v1/billing/credits/");
}

export function requestCreditCheckout(packId: string): Promise<{ url: string }> {
  return apiFetch<{ url: string }>("/api/v1/billing/credits/checkout/", {
    method: "POST",
    body: JSON.stringify({ pack_id: packId }),
  });
}

export function fetchTransparency(): Promise<TransparencyData> {
  return apiFetch<TransparencyData>("/api/v1/billing/usage/transparency/");
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

// ── Galaxy co-pilot ───────────────────────────────────────────────────────
// The in-game line shown when the player lands on (or lingers near) a star.
// The backend computes the spatial evidence; the LLM only phrases it. Always
// resolves to a line — `source: "fallback"` is the deterministic one served
// when the model is off/unreachable. `point` is the star the co-pilot gestures
// at (Phase 3 waypoint); null when there's nothing worth pointing to.
export interface CopilotPoint {
  star_id: number;
  label: string;
  reason: string;
}

export interface CopilotReflection {
  line: string;
  point: CopilotPoint | null;
  source: "llm" | "fallback";
}

export interface ReflectInput {
  star_id: number;
  recent_star_ids?: number[];
  nearby_star_ids?: number[];
  ship?: { x: number; y: number };
  mode?: "land" | "ambient";
}

/** POST /api/v1/lessons/galaxy/reflect/ — the co-pilot's grounded line for a star. */
export function reflectGalaxy(input: ReflectInput): Promise<CopilotReflection> {
  return apiFetch<CopilotReflection>("/api/v1/lessons/galaxy/reflect/", {
    method: "POST",
    body: JSON.stringify(input),
  });
}

// ── Star notes ────────────────────────────────────────────────────────────
// Free-text notes the user attaches to a star while exploring — a little extra
// context they add in their own words. Persisted as StarJournalEntry rows that
// also feed future co-pilot/tutoring context. Backed by the existing
// lessons/<id>/journal/ (list) + journal/create/ endpoints.
export interface StarNote {
  id: string;
  star: number;
  text: string;
  entry_type: string;
  tags: string[];
  created_at: string;
  star_stage?: string; // present on the create response — the star may have grown
}

export function fetchStarNotes(starId: number): Promise<StarNote[]> {
  return apiFetch<StarNote[]>(`/api/v1/lessons/${starId}/journal/`);
}

export function createStarNote(starId: number, text: string): Promise<StarNote> {
  return apiFetch<StarNote>(`/api/v1/lessons/${starId}/journal/create/`, {
    method: "POST",
    body: JSON.stringify({ text, entry_type: "revisit" }),
  });
}

// ── Tutoring (the 5-phase "go deeper" loop) ───────────────────────────────
// The user's assistant walks one star through restate → deepen → stress-test →
// connect → apply. Backed by lessons/<id>/tutor/{start,message,end}/. Completing
// it grows the star (returns new_star_stage). LLM spend is system-attributed.
export interface TutorTurn {
  session_id: string;
  message: string;
  current_phase: string;
  phase_index: number;
  total_phases: number;
  phase_complete?: boolean;
  mastery_achieved?: boolean;
  session_close?: TutorEnd;
}

export interface TutorEnd {
  session_id: string;
  tutoring_session_id?: string;
  phases_completed: string[];
  mastery_achieved: boolean;
  new_star_stage?: string;
}

export function tutorStart(starId: number): Promise<TutorTurn> {
  return apiFetch<TutorTurn>(`/api/v1/lessons/${starId}/tutor/start/`, { method: "POST" });
}

export function tutorMessage(
  starId: number,
  sessionId: string,
  message: string,
  action: "continue" | "skip" | "end" = "continue",
): Promise<TutorTurn> {
  return apiFetch<TutorTurn>(`/api/v1/lessons/${starId}/tutor/message/`, {
    method: "POST",
    body: JSON.stringify({ session_id: sessionId, message, action }),
  });
}

export function tutorEnd(starId: number, sessionId: string): Promise<TutorEnd> {
  return apiFetch<TutorEnd>(`/api/v1/lessons/${starId}/tutor/end/`, {
    method: "POST",
    body: JSON.stringify({ session_id: sessionId }),
  });
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

// sautai account link (Connected Apps → "powered by sautai" meal planning).
// The connect key is sent to Django, which exchanges it server-side — the key
// never persists and the platform secret never reaches the browser.
export function fetchSautaiLink(): Promise<import("@/lib/types").SautaiLinkStatus> {
  return apiFetch<import("@/lib/types").SautaiLinkStatus>("/api/v1/integrations/sautai/link/");
}

export function connectSautaiLink(
  connectKey: string,
): Promise<import("@/lib/types").SautaiLinkConnectResponse> {
  return apiFetch<import("@/lib/types").SautaiLinkConnectResponse>("/api/v1/integrations/sautai/link/", {
    method: "POST",
    body: JSON.stringify({ connect_key: connectKey }),
  });
}

export function disconnectSautaiLink(): Promise<import("@/lib/types").SautaiLinkStatus> {
  return apiFetch<import("@/lib/types").SautaiLinkStatus>("/api/v1/integrations/sautai/link/", {
    method: "DELETE",
  });
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

// Leave feedback on a rendered sit (thumbs + optional short note). Only the
// feedback fields are writable server-side; everything else is ignored.
export function submitMeditationFeedback(
  id: string,
  data: { user_feedback?: string; feedback_note?: string },
): Promise<import("@/lib/types").MeditationSession> {
  return apiFetch<import("@/lib/types").MeditationSession>(`/api/v1/core/sessions/${id}/`, {
    method: "PATCH",
    body: JSON.stringify(data),
  });
}

// ── Neighborhood (Friends) ───────────────────────────────────────────────────
// All addressed by friendship_id — never a tenant_id — per apps/friends/urls.py.
export function fetchNeighborhood(): Promise<import("@/lib/types").NeighborhoodData> {
  return apiFetch<import("@/lib/types").NeighborhoodData>("/api/v1/friends/");
}

export function sendWave(data: {
  handle: string;
  note?: string;
}): Promise<import("@/lib/types").WaveResult> {
  return apiFetch<import("@/lib/types").WaveResult>("/api/v1/friends/waves/", {
    method: "POST",
    body: JSON.stringify(data),
  });
}

export function acceptWave(friendshipId: string): Promise<import("@/lib/types").FriendshipStatusResult> {
  return apiFetch<import("@/lib/types").FriendshipStatusResult>(
    `/api/v1/friends/waves/${friendshipId}/accept/`,
    { method: "POST" },
  );
}

export function declineWave(friendshipId: string): Promise<import("@/lib/types").FriendshipStatusResult> {
  return apiFetch<import("@/lib/types").FriendshipStatusResult>(
    `/api/v1/friends/waves/${friendshipId}/decline/`,
    { method: "POST" },
  );
}

export function blockWave(friendshipId: string): Promise<import("@/lib/types").FriendshipStatusResult> {
  return apiFetch<import("@/lib/types").FriendshipStatusResult>(
    `/api/v1/friends/waves/${friendshipId}/block/`,
    { method: "POST" },
  );
}

export function unfriend(friendshipId: string): Promise<import("@/lib/types").FriendshipStatusResult> {
  return apiFetch<import("@/lib/types").FriendshipStatusResult>(`/api/v1/friends/${friendshipId}/`, {
    method: "DELETE",
  });
}

export function fetchNeighborProfile(): Promise<import("@/lib/types").NeighborProfile> {
  return apiFetch<import("@/lib/types").NeighborProfile>("/api/v1/friends/profile/");
}

export function updateNeighborProfile(
  data: Partial<import("@/lib/types").NeighborProfile>,
): Promise<import("@/lib/types").NeighborProfile> {
  return apiFetch<import("@/lib/types").NeighborProfile>("/api/v1/friends/profile/", {
    method: "PATCH",
    body: JSON.stringify(data),
  });
}

export function createFriendInvite(
  data: { max_uses?: number; expires_in_days?: number } = {},
): Promise<import("@/lib/types").FriendInvite> {
  return apiFetch<import("@/lib/types").FriendInvite>("/api/v1/friends/invites/", {
    method: "POST",
    body: JSON.stringify(data),
  });
}

// ── Neighborhood shares (PR2) ────────────────────────────────────────────────
// propose → scrub → preview → approve → publish.

export function shareLesson(
  lessonId: number,
  friendshipId: string,
): Promise<{ pending_share_id: string; status: string }> {
  return apiFetch<{ pending_share_id: string; status: string }>(`/api/v1/lessons/${lessonId}/share/`, {
    method: "POST",
    body: JSON.stringify({ friendship_id: friendshipId }),
  });
}

// PR7: same endpoint, a circle audience instead of a single neighbor.
export function shareLessonToCircle(
  lessonId: number,
  circleId: string,
): Promise<{ pending_share_id: string; status: string }> {
  return apiFetch<{ pending_share_id: string; status: string }>(`/api/v1/lessons/${lessonId}/share/`, {
    method: "POST",
    body: JSON.stringify({ target_circle_id: circleId }),
  });
}

export function fetchPendingShares(): Promise<import("@/lib/types").PendingShare[]> {
  return apiFetch<import("@/lib/types").PendingShare[]>("/api/v1/friends/shares/pending/");
}

/**
 * Same auth/refresh handling as apiFetch above, but never throws on a
 * "successful failure" status (202 "still scrubbing", 409 "can't share") —
 * it hands the status back so the share-preview poll and the approve flow
 * can branch on it directly instead of parsing a thrown error's `.status`.
 */
async function apiFetchStatus<T>(
  path: string,
  init?: RequestInit,
): Promise<{ status: number; data: T }> {
  let requestSession = captureAuthenticationSnapshot();
  let accessToken = requestSession.access;

  const proactiveRefresh = requestSession.refresh;
  if (
    accessToken &&
    proactiveRefresh &&
    isTokenExpiringSoon(accessToken)
  ) {
    try {
      assertAuthenticationSnapshotCurrent(requestSession);
      const refreshed = await getDedupedRefresh(
        requestSession.authenticationEpoch,
        proactiveRefresh,
      );
      requestSession = {
        authenticationEpoch: requestSession.authenticationEpoch,
        access: refreshed.access,
        refresh: refreshed.refresh,
      };
      assertAuthenticationSnapshotCurrent(requestSession);
      accessToken = refreshed.access;
    } catch (error) {
      if (error instanceof AuthenticationSupersededError) throw error;
      assertAuthenticationSnapshotCurrent(requestSession);
      // Proceed with the stale token; a 401 below is handled the same way.
    }
  }

  const headers: Record<string, string> = {
    "Content-Type": "application/json",
    ...(init?.headers as Record<string, string> ?? {}),
  };
  if (accessToken) {
    headers["Authorization"] = `Bearer ${accessToken}`;
  }

  let response = await fetch(`${API_BASE}${path}`, { ...init, headers });

  const reactiveRefresh = requestSession.refresh;
  if (response.status === 401 && reactiveRefresh) {
    try {
      assertAuthenticationSnapshotCurrent(requestSession);
      const refreshed = await getDedupedRefresh(
        requestSession.authenticationEpoch,
        reactiveRefresh,
      );
      requestSession = {
        authenticationEpoch: requestSession.authenticationEpoch,
        access: refreshed.access,
        refresh: refreshed.refresh,
      };
      assertAuthenticationSnapshotCurrent(requestSession);
      headers["Authorization"] = `Bearer ${refreshed.access}`;
      response = await fetch(`${API_BASE}${path}`, { ...init, headers });
    } catch (error) {
      if (error instanceof AuthenticationSupersededError) throw error;
      assertAuthenticationSnapshotCurrent(requestSession);
      // Fall through — response is still the original 401.
    }
  }

  if (response.status === 204) {
    return { status: response.status, data: undefined as T };
  }

  let data: unknown;
  try {
    data = await response.json();
  } catch {
    data = undefined;
  }
  return { status: response.status, data: data as T };
}

export type SharePreviewResult =
  | { status: 200; data: import("@/lib/types").SharePreview }
  | { status: 202 | 409; detail: string };

async function fetchSharePreviewByQuery(qs: URLSearchParams): Promise<SharePreviewResult> {
  const { status, data } = await apiFetchStatus<
    import("@/lib/types").SharePreview & { detail?: string }
  >(`/api/v1/friends/shares/preview/?${qs.toString()}`);

  if (status === 200) return { status, data: data as import("@/lib/types").SharePreview };
  if (status === 202 || status === 409) {
    return { status, detail: data?.detail ?? "Something went wrong." };
  }
  const err = new Error(data?.detail ?? `Request failed: ${status}`);
  (err as Error & { status: number }).status = status;
  throw err;
}

// GET /api/v1/friends/shares/preview/ — 200 once the scrub is ready, 202
// while it's still running (poll again), 409 if the scrub failed outright.
export function fetchSharePreview(lessonId: number, friendshipId: string): Promise<SharePreviewResult> {
  return fetchSharePreviewByQuery(
    new URLSearchParams({ lesson_id: String(lessonId), friendship_id: friendshipId }),
  );
}

// GET /api/v1/friends/shares/preview/?circle_id= (PR7) — same contract as
// fetchSharePreview above, widened for a circle audience.
export function fetchCircleSharePreview(lessonId: number, circleId: string): Promise<SharePreviewResult> {
  return fetchSharePreviewByQuery(new URLSearchParams({ lesson_id: String(lessonId), circle_id: circleId }));
}

export interface ApproveShareSuccess {
  pending_share_id: string;
  status: string;
  grant_id: string;
}

export type ApproveShareResult =
  | { status: 200; data: ApproveShareSuccess }
  | { status: 202 | 409; detail: string };

// POST /api/v1/friends/shares/<id>/approve/ — 200 publishes immediately, 202
// means an edited `final_text` triggered a re-scrub (go back to preview/poll
// before it can be approved again), 409 means the scrub failed.
export async function approveShare(id: string, finalText?: string): Promise<ApproveShareResult> {
  const { status, data } = await apiFetchStatus<ApproveShareSuccess & { detail?: string }>(
    `/api/v1/friends/shares/${id}/approve/`,
    {
      method: "POST",
      body: JSON.stringify(finalText !== undefined ? { final_text: finalText } : {}),
    },
  );

  if (status === 200) return { status, data: data as ApproveShareSuccess };
  if (status === 202 || status === 409) {
    return { status, detail: data?.detail ?? "Something went wrong." };
  }
  const err = new Error(data?.detail ?? `Request failed: ${status}`);
  (err as Error & { status: number }).status = status;
  throw err;
}

export function rejectShare(id: string): Promise<{ pending_share_id: string; status: string }> {
  return apiFetch<{ pending_share_id: string; status: string }>(`/api/v1/friends/shares/${id}/reject/`, {
    method: "POST",
  });
}

export function revokeShare(lessonId: number, grantId: string): Promise<{ revoked: boolean }> {
  return apiFetch<{ revoked: boolean }>(`/api/v1/lessons/${lessonId}/share/${grantId}/`, {
    method: "DELETE",
  });
}

// ── Wormholes & warp (PR3) ────────────────────────────────────────────────────
// Every call is addressed by friendship_id or shared_lesson_id — never a raw
// tenant id. The friend galaxy fetch is IMPERATIVE (called at warp time by the
// Phaser scene) and deliberately NOT a persisted react-query — a stale neighbor
// galaxy must never replay from localStorage.

/** GET /api/v1/friends/wormholes/ — warp targets for the home galaxy's rim. */
export function fetchWormholes(): Promise<import("@/lib/types").Wormhole[]> {
  return apiFetch<import("@/lib/types").Wormhole[]>("/api/v1/friends/wormholes/");
}

/** GET /api/v1/friends/<id>/galaxy/ — a neighbor's shared constellation (read-only). */
export function fetchFriendGalaxy(friendshipId: string): Promise<import("@/lib/types").FriendGalaxyData> {
  return apiFetch<import("@/lib/types").FriendGalaxyData>(`/api/v1/friends/${friendshipId}/galaxy/`);
}

/** POST /api/v1/friends/<id>/visited/ — advance the "new since last visit" watermark. */
export function markWormholeVisited(friendshipId: string): Promise<{ friendship_id: string; last_visited_at: string }> {
  return apiFetch<{ friendship_id: string; last_visited_at: string }>(`/api/v1/friends/${friendshipId}/visited/`, {
    method: "POST",
  });
}

/** POST /api/v1/friends/shares/<id>/adopt/ — bring a neighbor's spark home (pending). */
export function adoptSpark(sharedLessonId: string): Promise<import("@/lib/types").AdoptResult> {
  return apiFetch<import("@/lib/types").AdoptResult>(`/api/v1/friends/shares/${sharedLessonId}/adopt/`, {
    method: "POST",
  });
}

// ── Absorbed items (PR4) ──────────────────────────────────────────────────
// Transparency surface for what a neighbor's shared spark the assistant has
// pulled into its own context (via agent tooling) — and a manual purge.

/** GET /api/v1/friends/absorbed/ — everything the assistant currently holds. */
export function fetchAbsorbed(): Promise<import("@/lib/types").AbsorbedItem[]> {
  return apiFetch<import("@/lib/types").AbsorbedItem[]>("/api/v1/friends/absorbed/");
}

/** POST /api/v1/friends/absorbed/<id>/purge/ — tell the assistant to stop using it. */
export function purgeAbsorbed(id: string): Promise<void> {
  return apiFetch<void>(`/api/v1/friends/absorbed/${id}/purge/`, {
    method: "POST",
  });
}

// ── Friend chat (PR5) ──────────────────────────────────────────────────────
// 1:1 threads between accepted neighbors — every call addressed by
// thread_id, never a raw tenant_id. Trailing slashes required.

/** GET /api/v1/friends/threads/ — every thread the tenant is a member of. */
export function fetchThreads(): Promise<import("@/lib/types").ChatThread[]> {
  return apiFetch<import("@/lib/types").ChatThread[]>("/api/v1/friends/threads/");
}

/** POST /api/v1/friends/threads/ — open (or fetch) the 1:1 thread for a friendship. */
export function openThread(
  friendshipId: string,
): Promise<{ thread_id: string; friendship_id: string }> {
  return apiFetch<{ thread_id: string; friendship_id: string }>("/api/v1/friends/threads/", {
    method: "POST",
    body: JSON.stringify({ friendship_id: friendshipId }),
  });
}

/** GET /api/v1/friends/threads/<id>/messages/ — a page of messages, newest-cursor first. */
export function fetchThreadMessages(
  threadId: string,
  since?: string,
): Promise<import("@/lib/types").ChatPage> {
  const qs = new URLSearchParams({ limit: "50" });
  if (since) qs.set("since", since);
  return apiFetch<import("@/lib/types").ChatPage>(
    `/api/v1/friends/threads/${threadId}/messages/?${qs.toString()}`,
  );
}

export function sendThreadMessage(
  threadId: string,
  data: { client_msg_id: string; text: string },
): Promise<{ public_id: string; seq: number; created: boolean }> {
  return apiFetch<{ public_id: string; seq: number; created: boolean }>(
    `/api/v1/friends/threads/${threadId}/messages/`,
    { method: "POST", body: JSON.stringify(data) },
  );
}

export function markThreadRead(
  threadId: string,
): Promise<{ thread_id: string; last_read_seq: number }> {
  return apiFetch<{ thread_id: string; last_read_seq: number }>(
    `/api/v1/friends/threads/${threadId}/read/`,
    { method: "POST" },
  );
}

export function patchThreadMembership(
  threadId: string,
  data: { muted?: boolean; agent_absorb_enabled?: boolean },
): Promise<{ thread_id: string; muted: boolean; agent_absorb_enabled: boolean }> {
  return apiFetch<{ thread_id: string; muted: boolean; agent_absorb_enabled: boolean }>(
    `/api/v1/friends/threads/${threadId}/membership/`,
    { method: "PATCH", body: JSON.stringify(data) },
  );
}

// ── Missions (PR6) ────────────────────────────────────────────────────────
// Shared goals between accepted neighbors. Trailing slashes required, same
// as the rest of apps/friends/urls.py.

/** GET /api/v1/friends/missions/ — my missions (active memberships only). */
export function fetchMissions(): Promise<import("@/lib/types").MissionSummary[]> {
  return apiFetch<import("@/lib/types").MissionSummary[]>("/api/v1/friends/missions/");
}

/** POST /api/v1/friends/missions/ — create a 1:1 mission on an accepted friendship. */
export function createMission(data: {
  friendship_id: string;
  title: string;
  description?: string;
  target?: import("@/lib/types").MissionTarget;
  target_date?: string;
}): Promise<{ mission_id: string }> {
  return apiFetch<{ mission_id: string }>("/api/v1/friends/missions/", {
    method: "POST",
    body: JSON.stringify(data),
  });
}

/** GET /api/v1/friends/missions/<id>/ — mission + crew projection. */
export function fetchMissionDetail(id: string): Promise<import("@/lib/types").MissionDetail> {
  return apiFetch<import("@/lib/types").MissionDetail>(`/api/v1/friends/missions/${id}/`);
}

export interface PatchMissionSuccess {
  mission_id: string;
  version: number;
  title: string;
}

export type PatchMissionResult =
  | { status: 200; data: PatchMissionSuccess }
  | { status: 409; detail: string; version?: number };

/**
 * PATCH /api/v1/friends/missions/<id>/ — optimistic edit. Same status-aware
 * contract as approveShare above: 200 applies the edit, 409 means a
 * version/lock conflict — the caller shows `detail` and asks the user to
 * refresh rather than clobbering someone else's concurrent write.
 */
export async function patchMission(
  id: string,
  data: {
    version: number;
    title?: string;
    target?: import("@/lib/types").MissionTarget;
    target_date?: string | null;
  },
): Promise<PatchMissionResult> {
  const { status, data: body } = await apiFetchStatus<
    PatchMissionSuccess & { detail?: string; version?: number }
  >(`/api/v1/friends/missions/${id}/`, {
    method: "PATCH",
    body: JSON.stringify(data),
  });

  if (status === 200) return { status, data: body as PatchMissionSuccess };
  if (status === 409) {
    return { status, detail: body?.detail ?? "Something went wrong.", version: body?.version };
  }
  const err = new Error(body?.detail ?? `Request failed: ${status}`);
  (err as Error & { status: number }).status = status;
  throw err;
}

export function joinMission(
  id: string,
  commitment?: string,
): Promise<{ mission_id: string; status: string }> {
  return apiFetch<{ mission_id: string; status: string }>(`/api/v1/friends/missions/${id}/join/`, {
    method: "POST",
    body: JSON.stringify(commitment ? { commitment } : {}),
  });
}

export function leaveMission(id: string): Promise<{ mission_id: string; status: string }> {
  return apiFetch<{ mission_id: string; status: string }>(`/api/v1/friends/missions/${id}/leave/`, {
    method: "POST",
  });
}

export function addMissionUpdate(
  id: string,
  data: { kind: "note" | "progress" | "milestone"; text: string },
): Promise<{ id: string; kind: string }> {
  return apiFetch<{ id: string; kind: string }>(`/api/v1/friends/missions/${id}/updates/`, {
    method: "POST",
    body: JSON.stringify(data),
  });
}

export function addMissionTask(
  id: string,
  data: { title: string; description?: string; due_date?: string },
): Promise<{ task_id: string; title: string }> {
  return apiFetch<{ task_id: string; title: string }>(`/api/v1/friends/missions/${id}/tasks/`, {
    method: "POST",
    body: JSON.stringify(data),
  });
}

/** GET /api/v1/friends/mission-actions/ — my agent-proposed Mission tasks. */
export function fetchGoalActions(): Promise<import("@/lib/types").PendingGoalAction[]> {
  return apiFetch<import("@/lib/types").PendingGoalAction[]>("/api/v1/friends/mission-actions/");
}

export function approveGoalAction(
  id: string,
): Promise<{ action_id: string; status: string; task_id: string }> {
  return apiFetch<{ action_id: string; status: string; task_id: string }>(
    `/api/v1/friends/mission-actions/${id}/approve/`,
    { method: "POST" },
  );
}

export function rejectGoalAction(id: string): Promise<{ action_id: string; status: string }> {
  return apiFetch<{ action_id: string; status: string }>(
    `/api/v1/friends/mission-actions/${id}/reject/`,
    { method: "POST" },
  );
}

// ── Circles (PR7) ────────────────────────────────────────────────────────
// Groups built on edges (design §2.11) — trailing slashes required, same as
// the rest of apps/friends/urls.py. See apps/friends/circles.py for the
// service layer these views wrap.

/** GET /api/v1/friends/circles/ — my circles (active memberships only). */
export function fetchCircles(): Promise<import("@/lib/types").CircleSummary[]> {
  return apiFetch<import("@/lib/types").CircleSummary[]>("/api/v1/friends/circles/");
}

/** POST /api/v1/friends/circles/ — start a circle (creator becomes admin). */
export function createCircle(data: {
  name: string;
  description?: string;
  hue?: number;
}): Promise<{ circle_id: string }> {
  return apiFetch<{ circle_id: string }>("/api/v1/friends/circles/", {
    method: "POST",
    body: JSON.stringify(data),
  });
}

/** POST /api/v1/friends/circles/join/ {invite_code} — join via a code shared
 * by a neighbor already in the circle. */
export function joinCircle(inviteCode: string): Promise<import("@/lib/types").CircleJoinResult> {
  return apiFetch<import("@/lib/types").CircleJoinResult>("/api/v1/friends/circles/join/", {
    method: "POST",
    body: JSON.stringify({ invite_code: inviteCode }),
  });
}

/** GET /api/v1/friends/circles/<id>/ — members + (admin-only) invite code + thread_id. */
export function fetchCircleDetail(circleId: string): Promise<import("@/lib/types").CircleDetail> {
  return apiFetch<import("@/lib/types").CircleDetail>(`/api/v1/friends/circles/${circleId}/`);
}

/** POST /api/v1/friends/circles/<id>/members/ {handle} — wave a neighbor into the circle. */
export function addCircleMember(
  circleId: string,
  handle: string,
): Promise<{ circle_id: string; added: string }> {
  return apiFetch<{ circle_id: string; added: string }>(`/api/v1/friends/circles/${circleId}/members/`, {
    method: "POST",
    body: JSON.stringify({ handle }),
  });
}

/**
 * POST /api/v1/friends/circles/<id>/leave/ {keep?} — leave the circle.
 * Omitting `keep` (or passing `keep: false`) purges what the assistant
 * absorbed from this circle; `keep: true` is the explicit opt-in to retain it.
 */
export function leaveCircle(
  circleId: string,
  keep?: boolean,
): Promise<import("@/lib/types").CircleLeaveResult> {
  return apiFetch<import("@/lib/types").CircleLeaveResult>(`/api/v1/friends/circles/${circleId}/leave/`, {
    method: "POST",
    body: JSON.stringify(keep ? { keep: true } : {}),
  });
}

/** POST /api/v1/friends/circles/<id>/remove/ {handle} — admin removes a member. */
export function removeCircleMember(
  circleId: string,
  handle: string,
): Promise<{ circle_id: string; removed: string }> {
  return apiFetch<{ circle_id: string; removed: string }>(`/api/v1/friends/circles/${circleId}/remove/`, {
    method: "POST",
    body: JSON.stringify({ handle }),
  });
}

/** POST /api/v1/friends/circles/<id>/invite-code/ — admin regenerates the code. */
export function regenerateInviteCode(circleId: string): Promise<{ circle_id: string; invite_code: string }> {
  return apiFetch<{ circle_id: string; invite_code: string }>(
    `/api/v1/friends/circles/${circleId}/invite-code/`,
    { method: "POST" },
  );
}
