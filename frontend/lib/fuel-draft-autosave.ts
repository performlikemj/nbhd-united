/**
 * Continuous autosave for in-progress Fuel edits.
 *
 * Sibling to (but distinct from) orphan-drafts.ts:
 *   - orphan-drafts stashes ONLY when a workout row 404s mid-edit (the
 *     assistant/another tab deleted it) — recovery means "the row is gone".
 *   - this layer persists whatever the user is actively typing, debounced on
 *     change and flushed on pagehide/unmount, so navigating away mid-edit
 *     (closing the drawer, a browser back-swipe, a reload) never silently
 *     discards a logged set — recovery means "the row still exists, re-apply
 *     your unsaved edits to it".
 *
 * Storage: localStorage, tenant-scoped, 7-day TTL pruned on read. Nothing
 * syncs server-side — same on-device privacy contract as orphan-drafts
 * (sensitive notes like "knees achy" stay on the device until the user
 * explicitly saves).
 *
 * Keying: edit drafts use the workout id; the New Workout wizard uses the
 * sentinel key NEW_WORKOUT_KEY.
 */

const STORAGE_KEY = "nbhd_fuel_autosave_v1";
const TTL_MS = 7 * 24 * 60 * 60 * 1000;

/** Sentinel key for the not-yet-created workout in the New Workout wizard. */
export const NEW_WORKOUT_KEY = "new";

export interface AutosaveEntry<P = unknown> {
  /** Workout id, or NEW_WORKOUT_KEY for the create wizard. Also the map key. */
  key: string;
  /** Epoch ms of the last edit. Drives TTL eviction + the restore label. */
  updatedAt: number;
  /**
   * Server `workout.updated_at` (ISO) when editing began — lets the caller
   * flag that the row changed upstream while the user was away. null for new
   * drafts (no server row yet).
   */
  baseUpdatedAt: string | null;
  /** Caller-defined payload (the editable draft fields). */
  payload: P;
}

type StashShape = Record<string /* tenantId */, Record<string /* key */, AutosaveEntry>>;

function readStash(): StashShape {
  if (typeof window === "undefined") return {};
  try {
    const raw = window.localStorage.getItem(STORAGE_KEY);
    if (!raw) return {};
    const parsed = JSON.parse(raw) as unknown;
    return parsed && typeof parsed === "object" ? (parsed as StashShape) : {};
  } catch {
    return {};
  }
}

function writeStash(s: StashShape): void {
  if (typeof window === "undefined") return;
  try {
    window.localStorage.setItem(STORAGE_KEY, JSON.stringify(s));
  } catch {
    // Quota or serialization failure — silently drop. Autosave is best-effort.
  }
}

/** Drop expired entries for a tenant. Returns the surviving map (mutated copy). */
function pruneTenant(forTenant: Record<string, AutosaveEntry>, now: number): Record<string, AutosaveEntry> {
  const survivors: Record<string, AutosaveEntry> = {};
  for (const [k, entry] of Object.entries(forTenant)) {
    if (now - entry.updatedAt > TTL_MS) continue;
    survivors[k] = entry;
  }
  return survivors;
}

/**
 * Write (or replace) the autosave snapshot for `key`. Callers debounce; this
 * is a plain synchronous overwrite of the latest known draft.
 */
export function saveDraft<P>(
  tenantId: string,
  key: string,
  payload: P,
  baseUpdatedAt: string | null = null,
): void {
  if (!tenantId || !key) return;
  const stash = readStash();
  const now = Date.now();
  const forTenant = pruneTenant(stash[tenantId] ?? {}, now);
  forTenant[key] = { key, updatedAt: now, baseUpdatedAt, payload };
  stash[tenantId] = forTenant;
  writeStash(stash);
}

/** Read the snapshot for `key`, pruning expired entries as a side effect. */
export function loadDraft<P>(tenantId: string, key: string): AutosaveEntry<P> | null {
  if (!tenantId || !key) return null;
  const stash = readStash();
  const forTenant = stash[tenantId];
  if (!forTenant) return null;

  const now = Date.now();
  const survivors = pruneTenant(forTenant, now);
  if (Object.keys(survivors).length !== Object.keys(forTenant).length) {
    if (Object.keys(survivors).length === 0) delete stash[tenantId];
    else stash[tenantId] = survivors;
    writeStash(stash);
  }
  return (survivors[key] as AutosaveEntry<P> | undefined) ?? null;
}

/** Delete the snapshot for `key` (e.g. after a successful save). */
export function clearDraft(tenantId: string, key: string): void {
  if (!tenantId || !key) return;
  const stash = readStash();
  if (!stash[tenantId]) return;
  if (!(key in stash[tenantId])) return;
  delete stash[tenantId][key];
  if (Object.keys(stash[tenantId]).length === 0) delete stash[tenantId];
  writeStash(stash);
}

// Exposed for unit testing once a frontend test runner is wired.
export const __testing = { TTL_MS, pruneTenant };
