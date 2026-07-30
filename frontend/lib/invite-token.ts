// Neighborhood invite → signup auto-accept handoff (PR1.5 seam).
//
// A new user arriving via an invite link lands on /signup?invite=<token>.
// The signup page itself only creates the auth account — tenant provisioning
// (and thus the onboardTenant() call that the backend actually claims the
// invite against) doesn't happen until the PersonaScene step of /onboarding.
// The query param would be lost across that redirect, so we stash it in
// sessionStorage on the way in and read it back right before the
// onboardTenant() call. The token is cleared only after onboarding succeeds,
// so a transient failure can retry with the same invite.
//
// Mirrors the stash/read/clear shape of lib/app-authorize.ts.

const STORAGE_KEY = "nbhd_invite_token";

export function stashInviteToken(token: string): void {
  if (typeof window === "undefined") return;
  try {
    window.sessionStorage.setItem(STORAGE_KEY, token);
  } catch {
    // sessionStorage can throw (private mode / quota). The invite simply
    // won't auto-accept — never block signup over this.
  }
}

/** Read the stashed token without consuming it. */
export function peekInviteToken(): string | null {
  if (typeof window === "undefined") return null;
  try {
    return window.sessionStorage.getItem(STORAGE_KEY);
  } catch {
    return null;
  }
}

/** Consume the token after the invite-bearing onboarding request succeeds. */
export function clearInviteToken(): void {
  if (typeof window === "undefined") return;
  try {
    window.sessionStorage.removeItem(STORAGE_KEY);
  } catch {
    // Best effort. A repeated claim is safe for the backend to reject.
  }
}
