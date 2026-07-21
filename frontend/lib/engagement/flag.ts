/**
 * Gating for the frontend-only engagement prototype.
 *
 * The prototype ships dark unless explicitly enabled by either:
 *   - build flag  NEXT_PUBLIC_ENGAGEMENT_DEMO=1
 *   - per-device  localStorage.nbhd_engagement_demo = "1"
 *
 * Visiting Horizons with `?engagementDemo=1` opts the current browser in.
 */
const STORAGE_KEY = "nbhd_engagement_demo";
const CHANGE_EVENT = "nbhd-engagement-demo-change";

function isEngagementDemoBuildEnabled(): boolean {
  return process.env.NEXT_PUBLIC_ENGAGEMENT_DEMO === "1";
}

export function isEngagementDemoEnabled(): boolean {
  if (isEngagementDemoBuildEnabled()) return true;
  if (typeof window !== "undefined") {
    try {
      return window.localStorage.getItem(STORAGE_KEY) === "1";
    } catch {
      return false;
    }
  }
  return false;
}

export function enableEngagementDemoFromUrl(): boolean {
  if (typeof window === "undefined") return false;
  try {
    const shouldEnable =
      new URLSearchParams(window.location.search).get("engagementDemo") === "1";
    if (!shouldEnable) return false;
    window.localStorage.setItem(STORAGE_KEY, "1");
    window.dispatchEvent(new Event(CHANGE_EVENT));
    return true;
  } catch {
    return false;
  }
}

export function subscribeToEngagementDemoFlag(
  onStoreChange: () => void,
): () => void {
  if (typeof window === "undefined") return () => undefined;

  const handleStorage = (event: StorageEvent) => {
    if (event.key === STORAGE_KEY) onStoreChange();
  };
  window.addEventListener("storage", handleStorage);
  window.addEventListener(CHANGE_EVENT, onStoreChange);

  return () => {
    window.removeEventListener("storage", handleStorage);
    window.removeEventListener(CHANGE_EVENT, onStoreChange);
  };
}
