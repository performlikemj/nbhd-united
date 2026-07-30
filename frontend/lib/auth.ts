import {
  clearInMemoryQueryCache,
  clearPersistedCache,
  replaceActiveQueryClient,
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
  rotateAuthenticationEpoch();
  // Never install account B's credentials over account A's in-memory or
  // persisted React Query data. Both caches, including any pending persisted
  // flush, are gone before token mutation.
  clearInMemoryQueryCache();
  clearPersistedCache();
  setTokens(tokens.access, tokens.refresh);
  // Providers replace the cleared client with a new, B-owned instance. During
  // SSR/tests there is no registered callback, so the clear above is the
  // complete fallback behavior.
  replaceActiveQueryClient();
}

export function clearTokens(expectedRefresh?: string | null): boolean {
  if (
    expectedRefresh !== undefined &&
    getRefreshToken() !== expectedRefresh
  ) {
    return false;
  }
  rotateAuthenticationEpoch();
  localStorage.removeItem(ACCESS_TOKEN_KEY);
  localStorage.removeItem(REFRESH_TOKEN_KEY);
  return true;
}

export function isLoggedIn(): boolean {
  return getAccessToken() !== null;
}

function rotateAuthenticationEpoch(): void {
  const nextEpoch = createRandomAuthenticationEpoch();
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

function createRandomAuthenticationEpoch(): number {
  if (typeof globalThis.crypto !== "undefined") {
    const words = new Uint32Array(2);
    globalThis.crypto.getRandomValues(words);
    // 21 high bits + 32 low bits = the full non-negative safe-integer range.
    return (words[0]! & 0x1fffff) * 0x100000000 + words[1]!;
  }
  return Math.floor(Math.random() * Number.MAX_SAFE_INTEGER);
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
