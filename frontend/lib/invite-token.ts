// Neighborhood invite → signup auto-accept handoff (PR1.5 seam).
//
// A new user arriving via an invite link lands on /signup?invite=<token>.
// The signup page itself only creates the auth account — tenant provisioning
// (and thus the onboardTenant() call that the backend actually claims the
// invite against) doesn't happen until the PersonaScene step of /onboarding.
// The query param would be lost across that redirect, so we stash it in
// sessionStorage on the way in and read it back (once) right before the
// onboardTenant() call.
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

/** Read the stashed token and clear it so it's only ever applied once. */
export function readAndClearInviteToken(): string | null {
  if (typeof window === "undefined") return null;
  try {
    const token = window.sessionStorage.getItem(STORAGE_KEY);
    if (token) window.sessionStorage.removeItem(STORAGE_KEY);
    return token;
  } catch {
    return null;
  }
}
