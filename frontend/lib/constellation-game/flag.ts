/**
 * Gating for the constellation "exploration mode" (the playable galaxy).
 *
 * Phase-1 ships DARK: hidden for everyone unless explicitly opted in, so the
 * nega-self encounter is never forced on a user. Two ways to turn it on:
 *   - build flag  NEXT_PUBLIC_CONSTELLATION_PLAY=1   (enable for everyone)
 *   - per-device  localStorage.nbhd_play_beta = "1"  (opt your own browser in,
 *                 no redeploy — for testing in prod)
 *
 * Phase-2 will replace this with a proper per-user settings toggle.
 */
export function isPlayEnabled(): boolean {
  if (process.env.NEXT_PUBLIC_CONSTELLATION_PLAY === "1") return true;
  if (typeof window !== "undefined") {
    try {
      return window.localStorage.getItem("nbhd_play_beta") === "1";
    } catch {
      return false;
    }
  }
  return false;
}
