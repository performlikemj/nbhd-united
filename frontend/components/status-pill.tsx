import clsx from "clsx";

const tones: Record<string, string> = {
  active: "bg-emerald-100 text-emerald-800",
  pending: "bg-amber-100 text-amber-800",
  provisioning: "bg-sky-100 text-sky-800",
  suspended: "bg-rose-100 text-rose-800",
  deprovisioning: "bg-orange-100 text-orange-800",
  deleted: "bg-slate-100 text-slate-700",
  revoked: "bg-slate-100 text-slate-700",
  expired: "bg-amber-100 text-amber-800",
  error: "bg-rose-100 text-rose-800",
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
