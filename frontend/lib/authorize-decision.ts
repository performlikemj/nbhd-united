// Pure decision logic for the web→app signup/sign-in handoff (/app/authorize).
//
// Deliberately import-free so the security-critical branch table is trivially
// unit-testable and can NEVER silently complete a "register" into a leftover
// browser session belonging to a different account. The page (page.tsx) owns the
// side effects (network, navigation, token clearing); this module only decides
// WHICH step to take. See docs/web-signup-account-confirmation-flow.md.

export type AuthIntent = "register" | "signin";
export type AuthPath = "/signup" | "/login";

/**
 * The next thing the authorize page should do.
 *
 * - `redirect-auth`  — send the user to the real auth UI. `clearFirst` performs a
 *                      local logout (clear this browser's tokens) of a dead or
 *                      explicitly-rejected leftover session before routing.
 * - `probe-identity` — a session pre-existed this flow; resolve WHO it is, then ask.
 * - `choose-account` — show "Continue as <email>" / "Use a different account".
 * - `finish`         — mint the one-time PKCE code and redirect back to the app.
 */
export type AuthorizeStep =
  | { kind: "redirect-auth"; target: AuthPath; clearFirst: boolean }
  | { kind: "probe-identity" }
  | { kind: "choose-account"; email: string }
  | { kind: "finish" };

/** Where an unauthenticated user of this intent belongs. Unknown intent → signup. */
export function authPathForIntent(intent: string): AuthPath {
  return intent === "signin" ? "/login" : "/signup";
}

/**
 * Decide the step from synchronous signals, before any network call.
 *
 * @param firstHop   the PKCE params arrived in the URL (a fresh hop from the app),
 *                   vs. recovered from the sessionStorage stash (a post-auth
 *                   bounce-back from /signup or /login).
 * @param isLoggedIn a token exists in this browser — NOT proof of WHICH account.
 */
export function decideInitialStep(args: {
  firstHop: boolean;
  isLoggedIn: boolean;
  intent: string;
}): AuthorizeStep {
  const target = authPathForIntent(args.intent);

  if (!args.firstHop) {
    // Bounce-back: the user JUST authenticated in this flow, so the live token is
    // the one they chose — completing it is always correct. (Defensive: if a
    // bounce-back somehow arrives unauthenticated, route to auth rather than mint.)
    return args.isLoggedIn
      ? { kind: "finish" }
      : { kind: "redirect-auth", target, clearFirst: false };
  }

  // First hop from the app.
  if (!args.isLoggedIn) {
    return { kind: "redirect-auth", target, clearFirst: false };
  }

  // A session pre-existed this flow. NEVER trust it blindly — this is exactly the
  // path that used to silently complete a register into a leftover account. Find
  // out whose session it is and let the user decide.
  return { kind: "probe-identity" };
}

/**
 * Decide the step after the identity probe resolves.
 *
 * @param identity the resolved account, or `null` for a dead/expired leftover
 *                 session (no usable token).
 */
export function decideAfterProbe(
  identity: { email: string } | null,
  intent: string,
): AuthorizeStep {
  if (identity === null) {
    // Dead session — clear it locally (a reversible local logout) and route to auth.
    return { kind: "redirect-auth", target: authPathForIntent(intent), clearFirst: true };
  }
  return { kind: "choose-account", email: identity.email };
}

/**
 * The step taken when the user explicitly rejects the leftover account on the
 * choose-account screen. D2: clear the leftover session first (local logout),
 * then route to the auth UI for the original intent.
 */
export function stepForDifferentAccount(intent: string): AuthorizeStep {
  return { kind: "redirect-auth", target: authPathForIntent(intent), clearFirst: true };
}
