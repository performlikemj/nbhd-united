import { clearTokens, getAccessToken, getRefreshToken, setTokens } from "@/lib/auth";
import { AuthUser, DashboardData, Integration, Tenant, UsageRecord } from "@/lib/types";

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
      throw new Error("Session expired. Please sign in again.");
    }
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

// Dashboard
export function fetchDashboard(): Promise<DashboardData> {
  return apiFetch<DashboardData>("/api/v1/dashboard/");
}

export function fetchUsageHistory(): Promise<{ results: UsageRecord[] }> {
  return apiFetch<{ results: UsageRecord[] }>("/api/v1/dashboard/usage/");
}

// Tenants
export function fetchTenant(): Promise<Tenant> {
  return apiFetch<Tenant>("/api/v1/tenants/me/");
}

export function onboardTenant(data: { display_name?: string; language?: string }): Promise<Tenant> {
  return apiFetch<Tenant>("/api/v1/tenants/onboard/", {
    method: "POST",
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
