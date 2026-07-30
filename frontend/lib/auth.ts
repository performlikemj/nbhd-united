import {
  clearInMemoryQueryCache,
  clearPersistedCache,
  getJwtOwner,
  setActiveQueryClientOwner,
} from "@/lib/query-persist";

const ACCESS_TOKEN_KEY = "nbhd_access_token";
const REFRESH_TOKEN_KEY = "nbhd_refresh_token";
const AUTHENTICATION_GENERATION_KEY = "nbhd_auth_gen";
let authenticationEpoch = 0;

/** Snapshot used to reject requests that outlive an account switch/logout. */
export function getAuthenticationEpoch(): number {
  const persistedEpoch = readPersistedAuthenticationEpoch();
  if (persistedEpoch !== null) {
    authenticationEpoch = persistedEpoch;
  }
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
  incrementAuthenticationEpoch();
  // Never install account B's credentials over account A's in-memory or
  // persisted React Query data. Both caches, including any pending persisted
  // flush, are gone before token mutation.
  clearInMemoryQueryCache();
  clearPersistedCache();
  setTokens(tokens.access, tokens.refresh);
  setActiveQueryClientOwner(getJwtOwner(tokens.access));
}

export function clearTokens(): void {
  incrementAuthenticationEpoch();
  localStorage.removeItem(ACCESS_TOKEN_KEY);
  localStorage.removeItem(REFRESH_TOKEN_KEY);
}

export function isLoggedIn(): boolean {
  return getAccessToken() !== null;
}

function incrementAuthenticationEpoch(): void {
  const nextEpoch = getAuthenticationEpoch() + 1;
  authenticationEpoch = nextEpoch;
  if (typeof window === "undefined") return;
  try {
    window.localStorage.setItem(
      AUTHENTICATION_GENERATION_KEY,
      String(nextEpoch),
    );
  } catch {
    // Storage may be unavailable; the in-memory mirror still fences this tab.
  }
}

function readPersistedAuthenticationEpoch(): number | null {
  if (typeof window === "undefined") return null;
  try {
    const raw = window.localStorage.getItem(AUTHENTICATION_GENERATION_KEY);
    if (raw === null || raw.trim() === "") return null;
    const parsed = Number(raw);
    return Number.isSafeInteger(parsed) && parsed >= 0 ? parsed : null;
  } catch {
    return null;
  }
}
