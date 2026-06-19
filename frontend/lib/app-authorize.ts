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

export interface AuthorizeParams {
  responseType: string;
  client: string;
  redirectUri: string;
  codeChallenge: string;
  codeChallengeMethod: string;
  state: string;
  intent: string;
}

const STORAGE_KEY = "nbhd_authorize_params";

export const ALLOWED_REDIRECT_URIS = ["nbhd://auth/callback"];

/**
 * Read the authorize params from a URL query string. Returns null when the
 * query carries none of the handshake keys (e.g. a bounce-back to
 * /app/authorize with no query, where we fall back to the stashed copy).
 */
export function parseAuthorizeParams(search: string): AuthorizeParams | null {
  const q = new URLSearchParams(search);
  if (!q.has("code_challenge") && !q.has("response_type") && !q.has("state")) {
    return null;
  }
  return {
    responseType: q.get("response_type") ?? "",
    client: q.get("client") ?? "",
    redirectUri: q.get("redirect_uri") ?? "",
    codeChallenge: q.get("code_challenge") ?? "",
    codeChallengeMethod: q.get("code_challenge_method") ?? "",
    state: q.get("state") ?? "",
    intent: q.get("intent") ?? "register",
  };
}

/** Enforce the iOS contract before spending anything on the params. */
export function isValidAuthorizeParams(p: AuthorizeParams): boolean {
  return (
    p.responseType === "code" &&
    p.client === "ios" &&
    p.codeChallengeMethod === "S256" &&
    p.codeChallenge.length > 0 &&
    p.state.length > 0 &&
    ALLOWED_REDIRECT_URIS.includes(p.redirectUri) &&
    // iOS WebAuth.Intent is a closed enum {register, signin}.
    (p.intent === "register" || p.intent === "signin")
  );
}

export function stashAuthorizeParams(p: AuthorizeParams): void {
  if (typeof window === "undefined") return;
  try {
    window.sessionStorage.setItem(STORAGE_KEY, JSON.stringify(p));
  } catch {
    // sessionStorage can throw (private mode / quota). The handoff just can't
    // survive a round trip then — the authorize page handles the missing stash.
  }
}

export function readAuthorizeParams(): AuthorizeParams | null {
  if (typeof window === "undefined") return null;
  try {
    const raw = window.sessionStorage.getItem(STORAGE_KEY);
    if (!raw) return null;
    return JSON.parse(raw) as AuthorizeParams;
  } catch {
    return null;
  }
}

export function clearAuthorizeParams(): void {
  if (typeof window === "undefined") return;
  try {
    window.sessionStorage.removeItem(STORAGE_KEY);
  } catch {
    // ignore
  }
}

/**
 * True when a web→app handoff is mid-flight — the signal for the signup/login
 * success moments to bounce back to /app/authorize instead of /onboarding.
 */
export function hasPendingAppAuthorize(): boolean {
  return readAuthorizeParams() !== null;
}
