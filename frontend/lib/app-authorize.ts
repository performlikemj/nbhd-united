// Web→app PKCE handoff (iOS "Create an account").
//
// The iOS app opens https://hoodunited.org/app/authorize?... in an
// ASWebAuthenticationSession carrying its PKCE challenge + state. The SPA
// validates the params, routes the user through the existing register/sign-in
// UI, and — once a fresh access token is in hand — mints a one-time code via
// POST /api/v1/auth/authorize/ and redirects nbhd://auth/callback?code=&state=.
//
// These helpers carry the params across the register/login round trip via
// sessionStorage (survives client-side navigation within the one browser
// session, cleared when the session ends). Keep ALLOWED_REDIRECT_URIS in sync
// with the backend AUTH_ALLOWED_REDIRECT_URIS and the iOS WebAuth.redirectURI.

import {
  getAccessToken,
  getAuthenticationEpoch,
  getRefreshToken,
  setTokens,
} from "@/lib/auth";
import { API_BASE } from "@/lib/api";

export {
  ALLOWED_REDIRECT_URIS,
  isValidAuthorizeParams,
  parseAuthorizeParams,
} from "@/lib/authorize-stash-decision";
export type { AuthorizeParams } from "@/lib/authorize-stash-decision";
export {
  clearAuthorizeParams,
  hasPendingAppAuthorize,
  readAuthorizeParams,
  stashAuthorizeParams,
} from "@/lib/app-authorize-stash";

export interface ProbedIdentity {
  email: string;
  displayName: string;
}

export interface UnusableProbeSession {
  kind: "unusable";
  accessToken: string | null;
  refreshToken: string | null;
}

export type ProbeIdentityResult =
  | ProbedIdentity
  | UnusableProbeSession
  | "superseded";

/**
 * Resolve WHO the current browser session belongs to, for the authorize page's
 * "you already have an account" decision.
 *
 * Deliberately a raw fetch — NOT the shared `apiFetch` from lib/api.ts, whose
 * 401 handler calls `window.location.href = "/login"` (lib/api.ts) and would
 * hijack the authorize routing (a register intent must be able to land on
 * /signup). We replicate just the parts we need: try GET /me with the current
 * access token, and on a 401 attempt exactly one manual refresh (mirroring the
 * rotate-refresh persistence) before giving up.
 *
 * Returns the account on a live session, or an unusable-session snapshot when
 * there is no usable token. The caller conditionally clears only that exact
 * refresh token before routing to auth.
 */
export async function probeIdentity(): Promise<ProbeIdentityResult> {
  const probeEpoch = getAuthenticationEpoch();
  let token = getAccessToken();
  if (!token) return getUnusableProbeSession(probeEpoch, null);

  let res = await fetchMeRaw(token);
  if (!isProbeCurrent(probeEpoch, token)) return "superseded";
  if (res && res.status === 401) {
    const refreshed = await tryRefreshRaw();
    if (refreshed.kind === "superseded") return "superseded";
    if (refreshed.kind === "failed") {
      return getUnusableProbeSession(probeEpoch, token);
    }
    token = refreshed.access;
    res = await fetchMeRaw(token);
    if (!isProbeCurrent(probeEpoch, token)) return "superseded";
  }
  if (!res || !res.ok) {
    return getUnusableProbeSession(probeEpoch, token);
  }

  try {
    const data = (await res.json()) as { email?: unknown; display_name?: unknown };
    if (!isProbeCurrent(probeEpoch, token)) return "superseded";
    if (typeof data.email === "string" && data.email) {
      return {
        email: data.email,
        displayName: typeof data.display_name === "string" ? data.display_name : "",
      };
    }
  } catch {
    // Body wasn't JSON. A replacement session still wins over this stale probe.
    if (!isProbeCurrent(probeEpoch, token)) return "superseded";
  }
  return getUnusableProbeSession(probeEpoch, token);
}

async function fetchMeRaw(token: string): Promise<Response | null> {
  try {
    return await fetch(`${API_BASE}/api/v1/auth/me/`, {
      headers: { authorization: `Bearer ${token}` },
    });
  } catch {
    return null;
  }
}

/**
 * One-shot refresh mirroring lib/api.ts's rotation handling (persist the rotated
 * refresh token, fall back to the presented one when rotation is off) but
 * WITHOUT its navigate-to-/login side effect. Returns the new access token, or
 * null on failure. Does not clear tokens — the caller owns that decision.
 */
type RawRefreshResult =
  | { kind: "refreshed"; access: string }
  | { kind: "failed" }
  | { kind: "superseded" };

async function tryRefreshRaw(): Promise<RawRefreshResult> {
  const authenticationEpoch = getAuthenticationEpoch();
  const refresh = getRefreshToken();
  if (!refresh) return { kind: "failed" };
  try {
    const res = await fetch(`${API_BASE}/api/v1/auth/refresh/`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ refresh }),
    });
    if (!isRawRefreshCurrent(authenticationEpoch, refresh)) {
      return { kind: "superseded" };
    }
    if (!res.ok) return { kind: "failed" };
    const data = (await res.json()) as { access?: unknown; refresh?: unknown };
    if (!isRawRefreshCurrent(authenticationEpoch, refresh)) {
      return { kind: "superseded" };
    }
    if (typeof data.access !== "string" || !data.access) {
      return { kind: "failed" };
    }
    setTokens(data.access, typeof data.refresh === "string" ? data.refresh : refresh);
    return { kind: "refreshed", access: data.access };
  } catch {
    return isRawRefreshCurrent(authenticationEpoch, refresh)
      ? { kind: "failed" }
      : { kind: "superseded" };
  }
}

function isRawRefreshCurrent(
  expectedEpoch: number,
  expectedRefresh: string,
): boolean {
  return (
    getAuthenticationEpoch() === expectedEpoch &&
    getRefreshToken() === expectedRefresh
  );
}

function isProbeCurrent(expectedEpoch: number, expectedAccess: string): boolean {
  return (
    getAuthenticationEpoch() === expectedEpoch &&
    getAccessToken() === expectedAccess
  );
}

function getUnusableProbeSession(
  expectedEpoch: number,
  expectedAccess: string | null,
): UnusableProbeSession | "superseded" {
  const refreshToken = getRefreshToken();
  if (
    getAccessToken() !== expectedAccess ||
    getRefreshToken() !== refreshToken ||
    getAuthenticationEpoch() !== expectedEpoch
  ) {
    return "superseded";
  }
  return {
    kind: "unusable",
    accessToken: expectedAccess,
    refreshToken,
  };
}
