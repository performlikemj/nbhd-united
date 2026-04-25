"use client";

import type { QueryClient, QueryKey } from "@tanstack/react-query";

const STORAGE_KEY = "nbhd_qc_v1";

const PERSISTED_KEYS: QueryKey[] = [
  ["me"],
  ["tenant"],
  ["preferences"],
  ["personas"],
  ["sidebar-tree"],
];

const PERSISTED_KEY_STRINGS = new Set(PERSISTED_KEYS.map((k) => JSON.stringify(k)));

type PersistedShape = Record<string, unknown>;

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
  for (const key of PERSISTED_KEYS) {
    const value = data[JSON.stringify(key)];
    if (value !== undefined) {
      qc.setQueryData(key, value);
    }
  }
}

export function installPersistence(qc: QueryClient): () => void {
  if (typeof window === "undefined") return () => {};

  const flush = () => {
    const out: PersistedShape = {};
    for (const key of PERSISTED_KEYS) {
      const value = qc.getQueryData(key);
      if (value !== undefined) {
        out[JSON.stringify(key)] = value;
      }
    }
    writeStorage(out);
  };

  return qc.getQueryCache().subscribe((event) => {
    if (event.type !== "updated") return;
    if (event.action.type !== "success") return;
    const queryKeyStr = JSON.stringify(event.query.queryKey);
    if (PERSISTED_KEY_STRINGS.has(queryKeyStr)) flush();
  });
}

export function clearPersistedCache(): void {
  if (typeof window === "undefined") return;
  try {
    window.localStorage.removeItem(STORAGE_KEY);
  } catch {
    // ignore
  }
}
