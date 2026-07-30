// Pure validation for the web→app PKCE handoff stash.
//
// Deliberately import-free: storage is attacker-controlled browser data, so the
// complete shape, iOS contract, and 15-minute lifetime are reviewable here
// without any React or Web Storage side effects.

export interface AuthorizeParams {
  responseType: string;
  client: string;
  redirectUri: string;
  codeChallenge: string;
  codeChallengeMethod: string;
  state: string;
  intent: string;
}

export interface AuthorizeStashRecord {
  params: AuthorizeParams;
  stashedAt: number;
}

export type AuthorizeStashDecision =
  | { kind: "valid"; params: AuthorizeParams }
  | { kind: "absent" }
  | { kind: "invalid" }
  | { kind: "expired" };

export const AUTHORIZE_STASH_MAX_AGE_MS = 15 * 60 * 1000;

export const ALLOWED_REDIRECT_URIS = ["nbhd://auth/callback"];

/**
 * Read the authorize params from a URL query string. Returns null when the
 * query carries none of the handshake keys (e.g. a bounce-back to
 * /app/authorize with no query, where the stashed copy is used).
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

/** Enforce the iOS contract before using or storing the params. */
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

/** Build the only record shape that may be persisted to sessionStorage. */
export function createAuthorizeStash(
  params: AuthorizeParams,
  nowMs: number,
): AuthorizeStashRecord | null {
  if (!Number.isFinite(nowMs) || nowMs < 0 || !isValidAuthorizeParams(params)) {
    return null;
  }
  return {
    params: { ...params },
    stashedAt: nowMs,
  };
}

/**
 * Parse untrusted sessionStorage data. Callers clear both `invalid` and
 * `expired`; `absent` means there is nothing to clear.
 */
export function parseAuthorizeStash(
  raw: string | null,
  nowMs: number,
): AuthorizeStashDecision {
  if (raw === null) return { kind: "absent" };
  if (!Number.isFinite(nowMs) || nowMs < 0) return { kind: "invalid" };

  let value: unknown;
  try {
    value = JSON.parse(raw);
  } catch {
    return { kind: "invalid" };
  }

  if (!isRecord(value)) return { kind: "invalid" };
  if (
    typeof value.stashedAt !== "number" ||
    !Number.isFinite(value.stashedAt) ||
    value.stashedAt < 0 ||
    value.stashedAt > nowMs
  ) {
    return { kind: "invalid" };
  }

  const params = parseStoredParams(value.params);
  if (!params) return { kind: "invalid" };

  if (nowMs - value.stashedAt > AUTHORIZE_STASH_MAX_AGE_MS) {
    return { kind: "expired" };
  }
  return { kind: "valid", params };
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function parseStoredParams(value: unknown): AuthorizeParams | null {
  if (!isRecord(value)) return null;
  if (
    typeof value.responseType !== "string" ||
    typeof value.client !== "string" ||
    typeof value.redirectUri !== "string" ||
    typeof value.codeChallenge !== "string" ||
    typeof value.codeChallengeMethod !== "string" ||
    typeof value.state !== "string" ||
    typeof value.intent !== "string"
  ) {
    return null;
  }

  const params: AuthorizeParams = {
    responseType: value.responseType,
    client: value.client,
    redirectUri: value.redirectUri,
    codeChallenge: value.codeChallenge,
    codeChallengeMethod: value.codeChallengeMethod,
    state: value.state,
    intent: value.intent,
  };
  return isValidAuthorizeParams(params) ? params : null;
}
