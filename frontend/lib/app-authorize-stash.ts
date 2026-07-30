// Browser-side storage for the web→app PKCE handoff.
//
// Keep this module free of auth/API imports so lib/auth.ts can check the
// pending handoff without creating an app-authorize ↔ auth import cycle.

import {
  AUTHORIZE_STASH_MAX_AGE_MS,
  createAuthorizeStash,
  parseAuthorizeStash,
} from "@/lib/authorize-stash-decision";
import type { AuthorizeStashDecision } from "@/lib/authorize-stash-decision";
import type { AuthorizeParams } from "@/lib/authorize-stash-decision";

export const AUTHORIZE_STASH_STORAGE_KEY = "nbhd_authorize_params";

/**
 * Persist a fresh, validated record. False means the caller must fail the
 * handoff instead of silently sending the user through auth without a stash.
 */
export function stashAuthorizeParams(params: AuthorizeParams): boolean {
  if (typeof window === "undefined") return false;
  const record = createAuthorizeStash(params, Date.now());
  if (!record) return false;

  try {
    const serialized = JSON.stringify(record);
    window.sessionStorage.setItem(AUTHORIZE_STASH_STORAGE_KEY, serialized);
    if (
      window.sessionStorage.getItem(AUTHORIZE_STASH_STORAGE_KEY) !== serialized
    ) {
      clearAuthorizeParams();
      return false;
    }
    return true;
  } catch {
    // A failed overwrite can leave an older record behind.
    clearAuthorizeParams();
    return false;
  }
}

export function readAuthorizeParams(): AuthorizeParams | null {
  const decision = readAuthorizeStashDecision();
  if (decision.kind === "valid") return decision.params;
  return null;
}

/** Exact deadline used by Apple-button eligibility to recheck an expiring handoff. */
export function getAuthorizeStashExpiryMs(): number | null {
  const decision = readAuthorizeStashDecision();
  if (decision.kind !== "valid") return null;
  return decision.stashedAt + AUTHORIZE_STASH_MAX_AGE_MS;
}

export function clearAuthorizeParams(): void {
  if (typeof window === "undefined") return;
  try {
    window.sessionStorage.removeItem(AUTHORIZE_STASH_STORAGE_KEY);
  } catch {
    // Best effort. If storage is unavailable, it cannot drive this page.
  }
}

/**
 * True when a validated, unexpired web→app handoff is mid-flight.
 * Reading also clears malformed and expired attacker-controlled records.
 */
export function hasPendingAppAuthorize(): boolean {
  return readAuthorizeParams() !== null;
}

function readAuthorizeStashDecision(): AuthorizeStashDecision {
  if (typeof window === "undefined") return { kind: "absent" };

  let raw: string | null;
  try {
    raw = window.sessionStorage.getItem(AUTHORIZE_STASH_STORAGE_KEY);
  } catch {
    return { kind: "absent" };
  }

  const decision = parseAuthorizeStash(raw, Date.now());
  if (decision.kind !== "valid" && decision.kind !== "absent") {
    clearAuthorizeParams();
  }
  return decision;
}
