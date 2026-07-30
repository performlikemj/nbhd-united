// Browser-side storage for the web→app PKCE handoff.
//
// Keep this module free of auth/API imports so lib/auth.ts can check the
// pending handoff without creating an app-authorize ↔ auth import cycle.

import {
  createAuthorizeStash,
  parseAuthorizeStash,
} from "@/lib/authorize-stash-decision";
import type { AuthorizeParams } from "@/lib/authorize-stash-decision";

const STORAGE_KEY = "nbhd_authorize_params";

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
    window.sessionStorage.setItem(STORAGE_KEY, serialized);
    if (window.sessionStorage.getItem(STORAGE_KEY) !== serialized) {
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
  if (typeof window === "undefined") return null;

  let raw: string | null;
  try {
    raw = window.sessionStorage.getItem(STORAGE_KEY);
  } catch {
    return null;
  }

  const decision = parseAuthorizeStash(raw, Date.now());
  if (decision.kind === "valid") return decision.params;
  if (decision.kind !== "absent") clearAuthorizeParams();
  return null;
}

export function clearAuthorizeParams(): void {
  if (typeof window === "undefined") return;
  try {
    window.sessionStorage.removeItem(STORAGE_KEY);
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
