import { useMemo } from "react";

type StyleTuple = {
  bg: string;
  text: string;
};

const tones: Record<string, StyleTuple> = {
  active: { bg: "var(--status-emerald-bg)", text: "var(--status-emerald-text)" },
  paused: { bg: "var(--status-slate-bg)", text: "var(--status-slate-text)" },
  pending: { bg: "var(--status-amber-bg)", text: "var(--status-amber-text)" },
  running: { bg: "var(--status-sky-bg)", text: "var(--status-sky-text)" },
  succeeded: { bg: "var(--status-emerald-bg)", text: "var(--status-emerald-text)" },
  failed: { bg: "var(--status-rose-bg)", text: "var(--status-rose-text)" },
  skipped: { bg: "var(--status-slate-bg)", text: "var(--status-slate-text)" },
  manual: { bg: "var(--status-indigo-bg)", text: "var(--status-indigo-text)" },
  schedule: { bg: "var(--status-violet-bg)", text: "var(--status-violet-text)" },
  provisioning: { bg: "var(--status-sky-bg)", text: "var(--status-sky-text)" },
  suspended: { bg: "var(--status-rose-bg)", text: "var(--status-rose-text)" },
  deprovisioning: { bg: "var(--status-orange-bg)", text: "var(--status-orange-text)" },
  deleted: { bg: "var(--status-slate-bg)", text: "var(--status-slate-text)" },
  revoked: { bg: "var(--status-slate-bg)", text: "var(--status-slate-text)" },
  expired: { bg: "var(--status-amber-bg)", text: "var(--status-amber-text)" },
  error: { bg: "var(--status-rose-bg)", text: "var(--status-rose-text)" },
  low: { bg: "var(--status-amber-bg)", text: "var(--status-amber-text)" },
  medium: { bg: "var(--status-sky-bg)", text: "var(--status-sky-text)" },
  high: { bg: "var(--status-emerald-bg)", text: "var(--status-emerald-text)" },
  "thumbs-up": { bg: "var(--status-emerald-bg)", text: "var(--status-emerald-text)" },
  "thumbs-down": { bg: "var(--status-rose-bg)", text: "var(--status-rose-text)" },
  meh: { bg: "var(--status-amber-bg)", text: "var(--status-amber-text)" },
};

const fallbackTone: StyleTuple = {
  bg: "var(--status-slate-bg)",
  text: "var(--status-slate-text)",
};

export function StatusPill({ status }: { status: string }) {
  const style = useMemo(() => tones[status] ?? fallbackTone, [status]);

  return (
    <span
      className="inline-flex rounded-full px-2.5 py-1 text-xs font-medium capitalize"
      style={{
        backgroundColor: style.bg,
        color: style.text,
      }}
    >
      {status}
    </span>
  );
}
