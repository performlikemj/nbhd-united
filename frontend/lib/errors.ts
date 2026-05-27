/**
 * Canonical error-message helper for mutations, query errors, and any
 * thrown response from `apiFetch`.
 *
 * Surfaces something the user can read regardless of whether the backend
 * returned a JSON body, a plain string, or the network just dropped.
 */

const FRIENDLY_BY_CODE: Record<string, string> = {
  not_found: "We couldn't find that record. It may have been removed elsewhere.",
  no_tenant: "No active tenant for this session.",
  no_profile: "No fitness profile yet — set one up in the Fuel tab.",
  no_container: "Your assistant isn't running right now — try again in a moment.",
};

function friendlyForStatus(status: number | undefined, fallback: string): string {
  switch (status) {
    case 404:
      return "We couldn't find that record. It may have been removed elsewhere.";
    case 401:
      return "Your session has expired — please sign in again.";
    case 403:
      return "You don't have permission to do that.";
    case 0:
      return "Looks like you're offline. Check your connection and try again.";
    default:
      if (status && status >= 500) return "The server hit an error — please try again.";
      return fallback;
  }
}

export function getErrorMessage(err: unknown): string {
  if (!(err instanceof Error)) return "Something went wrong.";

  const status = (err as Error & { status?: number }).status;
  const raw = err.message || "";

  // apiFetch puts the response body in .message — try JSON first.
  if (raw.startsWith("{")) {
    try {
      const parsed = JSON.parse(raw) as { error?: string; detail?: string; message?: string };
      if (typeof parsed.detail === "string" && parsed.detail) return parsed.detail;
      if (typeof parsed.message === "string" && parsed.message) return parsed.message;
      if (typeof parsed.error === "string" && parsed.error) {
        return FRIENDLY_BY_CODE[parsed.error] ?? parsed.error;
      }
    } catch {
      // Fall through — not JSON
    }
  }

  return friendlyForStatus(status, raw || "Something went wrong.");
}

/** Convenience predicate used by recovery UIs (phantom workout, deleted document, etc). */
export function isNotFoundError(err: unknown): boolean {
  return err instanceof Error && (err as Error & { status?: number }).status === 404;
}
