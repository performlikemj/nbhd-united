import {
  clearInMemoryQueryCache,
  clearPersistedCache,
} from "@/lib/query-persist";

const ACCESS_TOKEN_KEY = "nbhd_access_token";
const REFRESH_TOKEN_KEY = "nbhd_refresh_token";
let authenticationEpoch = 0;

/** Snapshot used to reject requests that outlive an account switch/logout. */
export function getAuthenticationEpoch(): number {
  return authenticationEpoch;
}

export function getAccessToken(): string | null {
  if (typeof window === "undefined") return null;
  return localStorage.getItem(ACCESS_TOKEN_KEY);
}

export function getRefreshToken(): string | null {
  if (typeof window === "undefined") return null;
  return localStorage.getItem(REFRESH_TOKEN_KEY);
}

export function setTokens(access: string, refresh: string): void {
  localStorage.setItem(ACCESS_TOKEN_KEY, access);
  localStorage.setItem(REFRESH_TOKEN_KEY, refresh);
}

export function completeAuthentication(tokens: {
  access: string;
  refresh: string;
}): void {
  authenticationEpoch += 1;
  // Never install account B's credentials over account A's in-memory or
  // persisted React Query data. Both caches are gone before token mutation.
  clearInMemoryQueryCache();
  clearPersistedCache();
  setTokens(tokens.access, tokens.refresh);
}

export function clearTokens(): void {
  authenticationEpoch += 1;
  localStorage.removeItem(ACCESS_TOKEN_KEY);
  localStorage.removeItem(REFRESH_TOKEN_KEY);
}

export function isLoggedIn(): boolean {
  return getAccessToken() !== null;
}
