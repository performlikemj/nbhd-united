function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

/**
 * Select the oldest usable proactive assistant message from a chat-feed page.
 *
 * The feed is intentionally treated as unknown at this boundary: an absent or
 * changed envelope/row field simply means there is no web greeting to render.
 */
export function selectGreeting(payload: unknown): string | null {
  if (!isRecord(payload) || !Array.isArray(payload.messages)) return null;

  let earliestText: string | null = null;
  let earliestTimestamp = Number.POSITIVE_INFINITY;

  for (const row of payload.messages) {
    if (!isRecord(row)) continue;
    if (row.role !== "assistant" || row.source !== "cron") continue;
    if (typeof row.text !== "string" || typeof row.created_at !== "string") {
      continue;
    }

    const text = row.text.trim();
    const timestamp = Date.parse(row.created_at);
    if (!text || !Number.isFinite(timestamp)) continue;

    if (timestamp < earliestTimestamp) {
      earliestText = text;
      earliestTimestamp = timestamp;
    }
  }

  return earliestText;
}
