"use client";

import type { QueryClient, QueryKey } from "@tanstack/react-query";

// Bump when the persisted shape changes (e.g., FuelWorkout adds a field).
// Old blobs become unreadable; the app re-fetches once.
// v3: each entry is now { d: data, u: dataUpdatedAt } instead of bare data,
// so rehydration restores the TRUE fetch time (see seedQueryClient).
const STORAGE_KEY = "nbhd_qc_v3";

const FLUSH_DEBOUNCE_MS = 500;

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

function readStorage(): PersistedShape | null {
  if (typeof window === "undefined") return null;
  try {
    const raw = window.localStorage.getItem(STORAGE_KEY);
    return raw ? (JSON.parse(raw) as PersistedShape) : null;
  } catch {
    return null;
  }
}

function writeStorage(data: PersistedShape): void {
  if (typeof window === "undefined") return;
  try {
    window.localStorage.setItem(STORAGE_KEY, JSON.stringify(data));
  } catch {
    // Quota exceeded or serialization error — drop silently.
  }
}

export function seedQueryClient(qc: QueryClient): void {
  const data = readStorage();
  if (!data) return;
  for (const [keyStr, raw] of Object.entries(data)) {
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

  let timer: number | null = null;

  const flush = () => {
    timer = null;
    const out: PersistedShape = {};
    for (const entry of qc.getQueryCache().getAll()) {
      const value = entry.state.data;
      if (value === undefined) continue;
      if (!matchesAnyPrefix(entry.queryKey)) continue;
      const persisted: PersistedEntry = { d: value, u: entry.state.dataUpdatedAt };
      out[JSON.stringify(entry.queryKey)] = persisted;
    }
    writeStorage(out);
  };

  const scheduleFlush = () => {
    if (timer != null) return;
    timer = window.setTimeout(flush, FLUSH_DEBOUNCE_MS);
  };

  const unsubscribe = qc.getQueryCache().subscribe((event) => {
    if (event.type !== "updated") return;
    if (event.action.type !== "success") return;
    if (!matchesAnyPrefix(event.query.queryKey)) return;
    scheduleFlush();
  });

  return () => {
    if (timer != null) window.clearTimeout(timer);
    unsubscribe();
  };
}

export function clearPersistedCache(): void {
  if (typeof window === "undefined") return;
  try {
    window.localStorage.removeItem(STORAGE_KEY);
  } catch {
    // ignore
  }
}
