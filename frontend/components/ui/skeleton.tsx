/**
 * Shared skeleton primitive. Pulses subtly to indicate loading without
 * shouting. Sized purely by className so callers control layout.
 *
 * Use `aria-busy="true"` and `role="status"` on the wrapping region — not
 * on each bar — so screen readers announce one "loading" event per panel.
 */
export function SkelBar({ className = "" }: { className?: string }) {
  return <div aria-hidden="true" className={`animate-pulse rounded bg-ink/10 ${className}`} />;
}
