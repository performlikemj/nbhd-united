import clsx from "clsx";

const tones: Record<string, string> = {
  active: "bg-emerald-100 text-emerald-800",
  paused: "bg-slate-100 text-slate-700",
  pending: "bg-amber-100 text-amber-800",
  running: "bg-sky-100 text-sky-800",
  succeeded: "bg-emerald-100 text-emerald-800",
  failed: "bg-rose-100 text-rose-800",
  skipped: "bg-slate-100 text-slate-700",
  manual: "bg-indigo-100 text-indigo-800",
  schedule: "bg-violet-100 text-violet-800",
  provisioning: "bg-sky-100 text-sky-800",
  suspended: "bg-rose-100 text-rose-800",
  deprovisioning: "bg-orange-100 text-orange-800",
  deleted: "bg-slate-100 text-slate-700",
  revoked: "bg-slate-100 text-slate-700",
  expired: "bg-amber-100 text-amber-800",
  error: "bg-rose-100 text-rose-800",
  low: "bg-amber-100 text-amber-800",
  medium: "bg-sky-100 text-sky-800",
  high: "bg-emerald-100 text-emerald-800",
};

export function StatusPill({ status }: { status: string }) {
  return (
    <span
      className={clsx(
        "inline-flex rounded-full px-2.5 py-1 text-xs font-medium capitalize",
        tones[status] ?? "bg-slate-100 text-slate-700"
      )}
    >
      {status}
    </span>
  );
}
