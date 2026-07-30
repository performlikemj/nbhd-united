// Pure decision logic for the Sign in with Apple browser flow.
//
// Keep this module import-free so eligibility and backend-error handling remain
// reviewable without React, the Apple SDK, or network side effects.

export const APPLE_LINK_REQUIRED_MESSAGE =
  "An account with this email already exists. Sign in with your password, then connect Apple in Settings.";
export const APPLE_SIGNUP_GATED_MESSAGE =
  "Sign-ups are invite-only right now.";
export const APPLE_RATE_LIMIT_MESSAGE =
  "Too many attempts — try again in a minute.";
export const APPLE_UNAVAILABLE_MESSAGE =
  "Apple sign-in is temporarily unavailable.";
export const APPLE_GENERIC_RETRY_MESSAGE =
  "Apple sign-in couldn’t be completed. Please try again.";
export const APPLE_ALREADY_LINKED_MESSAGE =
  "An Apple ID is already connected to this account.";
export const APPLE_ID_IN_USE_MESSAGE =
  "This Apple ID is already connected to another account.";

export type AppleFailureKind = "http" | "network" | "popup";

export interface AppleEligibilityInput {
  clientId: string;
  redirectUri: string;
  origin: string;
  hasPendingHandoff: boolean;
}

/** The Apple control is absent unless every frozen eligibility check passes. */
export function isAppleSignInEligible(input: AppleEligibilityInput): boolean {
  return (
    input.clientId.trim().length > 0 &&
    input.origin === input.redirectUri &&
    !input.hasPendingHandoff
  );
}

/**
 * Map the deliberately small backend error surface to the frozen user copy.
 * Unknown 4xx responses, invalid_grant, and popup failures stay collapsed.
 */
export function getAppleAuthErrorMessage(input: {
  kind: AppleFailureKind;
  status?: number;
  errorCode?: string | null;
}): string {
  if (input.kind === "network") return APPLE_UNAVAILABLE_MESSAGE;
  if (input.kind === "popup") return APPLE_GENERIC_RETRY_MESSAGE;

  if (input.status === 429) return APPLE_RATE_LIMIT_MESSAGE;
  if (input.status === 503) return APPLE_UNAVAILABLE_MESSAGE;

  if (input.status === 409) {
    if (input.errorCode === "link_required") {
      return APPLE_LINK_REQUIRED_MESSAGE;
    }
    if (input.errorCode === "already_linked") {
      return APPLE_ALREADY_LINKED_MESSAGE;
    }
    if (input.errorCode === "apple_id_in_use") {
      return APPLE_ID_IN_USE_MESSAGE;
    }
  }

  if (input.status === 403 && input.errorCode === "signup_gated") {
    return APPLE_SIGNUP_GATED_MESSAGE;
  }

  return APPLE_GENERIC_RETRY_MESSAGE;
}
