/**
 * Orphan-draft stash: salvages user input from the workout detail drawer
 * when the underlying row 404s mid-edit (assistant runtime deleted it,
 * plan regen replaced it, another tab removed it).
 *
 * Capture trigger: 404 only — either on the drawer's GET or on a save
 * mutation. We never auto-stash idle drawers because the recovery
 * banner is intrusive enough that false positives would train users to
 * dismiss it.
 *
 * Storage: localStorage, tenant-scoped, 7-day TTL pruned on read.
 * Nothing here ever syncs server-side — sensitive notes ("knees achy")
 * stay on the device until the user explicitly commits via the
 * recovery panel.
 */

const STORAGE_KEY = "nbhd_fuel_orphan_drafts_v1";
const TTL_MS = 7 * 24 * 60 * 60 * 1000;

export type OrphanDraftSource = "phantom_404" | "mutation_404";

export interface OrphanDraft {
  /** ID assigned by stashOrphan — also the localStorage map key. */
  stashId: string;
  /** Epoch ms when the stash was created. Drives TTL eviction. */
  capturedAt: number;
  /** The row id that 404'd. Used to scrub the same phantom from caches. */
  originalWorkoutId: string;
  date: string;
  category: string;
  activity: string;
  duration_minutes: number | null;
  rpe: number | null;
  notes: string;
  detail_json: Record<string, unknown>;
  source: OrphanDraftSource;
  /**
   * The original workout's status at the time of stash. Optional for
   * backwards compatibility with drafts captured before phase 6 of the
   * plan-reconciler work — those default to ``"planned"`` on recovery so
   * a future-dated edit isn't silently downgraded to a completed log.
   */
  status?: "done" | "planned";
}

type StashShape = Record<string /* tenantId */, Record<string /* stashId */, OrphanDraft>>;

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
    // Same-tab listeners (e.g. the Fuel-page banner) need an explicit
    // event — the native `storage` event only fires on other tabs.
    window.dispatchEvent(new CustomEvent("nbhd:orphan-drafts-changed"));
  } catch {
    // Quota or serialization failure — silently drop. Recovery is best-effort.
  }
}

function newStashId(): string {
  // Don't need cryptographic uniqueness — collision across stashes for the
  // same tenant inside the same millisecond is acceptable to drop.
  return `${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 8)}`;
}

/**
 * Stash a draft. Returns the new stashId, or null if the payload is
 * empty enough that recovery would be more confusing than helpful.
 */
export function stashOrphan(
  tenantId: string,
  payload: Omit<OrphanDraft, "stashId" | "capturedAt">,
): string | null {
  if (!tenantId) return null;
  if (!hasMeaningfulInput(payload)) return null;

  const stash = readStash();
  const forTenant = stash[tenantId] ?? {};
  const stashId = newStashId();
  forTenant[stashId] = { ...payload, stashId, capturedAt: Date.now() };
  stash[tenantId] = forTenant;
  writeStash(stash);
  return stashId;
}

export function listOrphans(tenantId: string): OrphanDraft[] {
  if (!tenantId) return [];
  const stash = readStash();
  const forTenant = stash[tenantId];
  if (!forTenant) return [];

  const now = Date.now();
  const survivors: Record<string, OrphanDraft> = {};
  for (const [id, draft] of Object.entries(forTenant)) {
    if (now - draft.capturedAt > TTL_MS) continue;
    survivors[id] = draft;
  }

  if (Object.keys(survivors).length !== Object.keys(forTenant).length) {
    stash[tenantId] = survivors;
    writeStash(stash);
  }
  // Newest first — usually what the user wants to act on.
  return Object.values(survivors).sort((a, b) => b.capturedAt - a.capturedAt);
}

export function getOrphan(tenantId: string, stashId: string): OrphanDraft | null {
  return listOrphans(tenantId).find((d) => d.stashId === stashId) ?? null;
}

export function discardOrphan(tenantId: string, stashId: string): void {
  if (!tenantId || !stashId) return;
  const stash = readStash();
  if (!stash[tenantId]) return;
  if (!(stashId in stash[tenantId])) return;
  delete stash[tenantId][stashId];
  if (Object.keys(stash[tenantId]).length === 0) delete stash[tenantId];
  writeStash(stash);
}

/**
 * Did the user actually enter anything worth preserving? Pure activity
 * name from a planned row doesn't count — what we care about is real
 * metrics or notes the user typed during the session.
 */
function hasMeaningfulInput(d: Omit<OrphanDraft, "stashId" | "capturedAt">): boolean {
  if (d.notes && d.notes.trim().length > 0) return true;
  if (d.rpe != null) return true;
  if (d.duration_minutes != null) return true;
  // detail_json is the cardio metrics / strength exercises bucket. Treat
  // any populated leaf as meaningful — covers "ran 3.8 mi" even when
  // notes/rpe/duration are blank.
  if (hasPopulatedLeaf(d.detail_json)) return true;
  return false;
}

function hasPopulatedLeaf(obj: unknown): boolean {
  if (obj == null) return false;
  if (typeof obj === "string") return obj.trim().length > 0;
  if (typeof obj === "number") return Number.isFinite(obj) && obj !== 0;
  if (typeof obj === "boolean") return obj;
  if (Array.isArray(obj)) return obj.some(hasPopulatedLeaf);
  if (typeof obj === "object") return Object.values(obj as Record<string, unknown>).some(hasPopulatedLeaf);
  return false;
}

// Exposed for unit testing the categorizer.
export const __testing = { hasMeaningfulInput, hasPopulatedLeaf, TTL_MS };
