"use client";

import type { QueryClient, QueryKey } from "@tanstack/react-query";

// Bump when the persisted shape changes (e.g., FuelWorkout adds a field).
// Old blobs become unreadable; the app re-fetches once.
// v3: each entry is now { d: data, u: dataUpdatedAt } instead of bare data,
// so rehydration restores the TRUE fetch time (see seedQueryClient).
const STORAGE_KEY = "nbhd_qc_v3";
const ACCESS_TOKEN_KEY = "nbhd_access_token";

const FLUSH_DEBOUNCE_MS = 500;

let activeQueryClient: QueryClient | null = null;
let cancelActiveFlush: (() => void) | null = null;

// One persisted query entry: the cached data plus the epoch-ms timestamp of
// when it was last fetched. Persisting `u` is what lets staleTime math
// survive a reload — without it, setQueryData stamps dataUpdatedAt=now and
// day-old data is treated as fresh.
interface PersistedEntry {
  d: unknown;
  u: number;
}

// Persist any query whose queryKey starts with one of these prefixes.
// e.g., ["fuel-workout"] matches ["fuel-workout", "<uuid>"].
const PERSISTED_PREFIXES: QueryKey[] = [
  // user-scoped
  ["me"],
  ["tenant"],
  ["preferences"],
  ["personas"],
  ["sidebar-tree"],
  // horizons + constellation/galaxy — page-level payloads so those pages
  // paint from cache on reload instead of a blank fetch state
  ["horizons"],
  ["constellation"],
  ["galaxy"],
  // fuel — page-level
  ["fuel-profile"],
  ["fuel-weekly-volume"],
  ["fuel-workout-count"],
  // fuel — tab-level
  ["fuel-schedule"],
  ["fuel-workouts"],
  ["fuel-workout"],
  ["fuel-calendar"],
  ["fuel-progress"],
  // fuel — progress sub-panels
  ["fuel-body-weight"],
  ["fuel-sleep"],
  ["fuel-resting-hr"],
];

type PersistedShape = Record<string, unknown>;

interface PersistedEnvelope {
  owner: string;
  entries: PersistedShape;
}

function matchesAnyPrefix(key: QueryKey): boolean {
  for (const prefix of PERSISTED_PREFIXES) {
    if (key.length < prefix.length) continue;
    let ok = true;
    for (let i = 0; i < prefix.length; i++) {
      if (JSON.stringify(key[i]) !== JSON.stringify(prefix[i])) {
        ok = false;
        break;
      }
    }
    if (ok) return true;
  }
  return false;
}

function readStorage(): PersistedEnvelope | null {
  if (typeof window === "undefined") return null;
  try {
    const raw = window.localStorage.getItem(STORAGE_KEY);
    if (!raw) return null;
    const parsed = JSON.parse(raw) as unknown;
    if (!isPersistedEnvelope(parsed)) {
      removeStorage();
      return null;
    }
    return parsed;
  } catch {
    removeStorage();
    return null;
  }
}

function writeStorage(data: PersistedEnvelope): void {
  if (typeof window === "undefined") return;
  try {
    window.localStorage.setItem(STORAGE_KEY, JSON.stringify(data));
  } catch {
    // Quota exceeded or serialization error — drop silently.
  }
}

export function seedQueryClient(qc: QueryClient): void {
  const envelope = readStorage();
  if (!envelope) return;

  const currentOwner = getCurrentAccessTokenOwner();
  if (!currentOwner || envelope.owner !== currentOwner) {
    removeStorage();
    return;
  }

  for (const [keyStr, raw] of Object.entries(envelope.entries)) {
    try {
      const key = JSON.parse(keyStr) as QueryKey;
      const entry = raw as Partial<PersistedEntry> | undefined;
      if (!entry || typeof entry !== "object" || !("d" in entry)) continue;
      // Restore the original fetch time so staleTime treats the data at its
      // true age: recent data paints instantly and skips a refetch, while
      // genuinely stale data (e.g. a day-old schedule) is marked stale and
      // re-validates on mount instead of masquerading as fresh.
      qc.setQueryData(key, entry.d, {
        updatedAt: typeof entry.u === "number" ? entry.u : 0,
      });
    } catch {
      // Bad entry; skip.
    }
  }
}

export function installPersistence(qc: QueryClient): () => void {
  if (typeof window === "undefined") return () => {};

  activeQueryClient = qc;
  let timer: number | null = null;

  const cancelPendingFlush = () => {
    if (timer === null) return;
    window.clearTimeout(timer);
    timer = null;
  };
  cancelActiveFlush = cancelPendingFlush;

  const flush = (scheduledOwner: string | null) => {
    timer = null;
    // A debounce scheduled under Account A may fire after Account B installs
    // tokens. Never let that stale closure recreate A's persisted cache.
    if (
      !scheduledOwner ||
      getCurrentAccessTokenOwner() !== scheduledOwner
    ) {
      return;
    }

    const out: PersistedShape = {};
    for (const entry of qc.getQueryCache().getAll()) {
      const value = entry.state.data;
      if (value === undefined) continue;
      if (!matchesAnyPrefix(entry.queryKey)) continue;
      const persisted: PersistedEntry = { d: value, u: entry.state.dataUpdatedAt };
      out[JSON.stringify(entry.queryKey)] = persisted;
    }
    writeStorage({ owner: scheduledOwner, entries: out });
  };

  const scheduleFlush = () => {
    if (timer != null) return;
    const scheduledOwner = getCurrentAccessTokenOwner();
    if (!scheduledOwner) return;
    timer = window.setTimeout(
      () => flush(scheduledOwner),
      FLUSH_DEBOUNCE_MS,
    );
  };

  const unsubscribe = qc.getQueryCache().subscribe((event) => {
    if (event.type !== "updated") return;
    if (event.action.type !== "success") return;
    if (!matchesAnyPrefix(event.query.queryKey)) return;
    scheduleFlush();
  });

  return () => {
    cancelPendingFlush();
    if (activeQueryClient === qc) activeQueryClient = null;
    if (cancelActiveFlush === cancelPendingFlush) cancelActiveFlush = null;
    unsubscribe();
  };
}

export function clearInMemoryQueryCache(): void {
  activeQueryClient?.clear();
}

export function clearPersistedCache(): void {
  if (typeof window === "undefined") return;
  cancelActiveFlush?.();
  removeStorage();
}

function removeStorage(): void {
  if (typeof window === "undefined") return;
  try {
    window.localStorage.removeItem(STORAGE_KEY);
  } catch {
    // ignore
  }
}

function isPersistedEnvelope(value: unknown): value is PersistedEnvelope {
  return (
    isRecord(value) &&
    typeof value.owner === "string" &&
    value.owner.length > 0 &&
    isRecord(value.entries)
  );
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function getCurrentAccessTokenOwner(): string | null {
  if (typeof window === "undefined") return null;
  try {
    return getJwtOwner(window.localStorage.getItem(ACCESS_TOKEN_KEY));
  } catch {
    return null;
  }
}

/** Decode only the untrusted owner claim; API authentication still verifies JWTs. */
function getJwtOwner(token: string | null): string | null {
  if (!token) return null;
  try {
    const payload = token.split(".")[1];
    if (!payload) return null;
    const base64 = payload.replace(/-/g, "+").replace(/_/g, "/");
    const padded = base64.padEnd(Math.ceil(base64.length / 4) * 4, "=");
    const claims = JSON.parse(atob(padded)) as {
      user_id?: unknown;
      sub?: unknown;
    };
    return normalizeOwner(claims.user_id) ?? normalizeOwner(claims.sub);
  } catch {
    return null;
  }
}

function normalizeOwner(value: unknown): string | null {
  if (typeof value === "string" && value.length > 0) return value;
  if (typeof value === "number" && Number.isFinite(value)) {
    return String(value);
  }
  return null;
}
