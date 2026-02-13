const PREVIEW_KEY = "nbhd_preview_key";

export function getPreviewKey(): string | null {
  if (typeof window === "undefined") return null;
  return localStorage.getItem(PREVIEW_KEY);
}

export function setPreviewKey(key: string): void {
  localStorage.setItem(PREVIEW_KEY, key);
}

export function clearPreviewKey(): void {
  localStorage.removeItem(PREVIEW_KEY);
}

export function hasPreviewKey(): boolean {
  return getPreviewKey() !== null;
}
