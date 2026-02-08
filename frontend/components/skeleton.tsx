import clsx from "clsx";

export function Skeleton({ className }: { className?: string }) {
  return (
    <div
      className={clsx("animate-pulse rounded bg-ink/10", className)}
    />
  );
}

export function StatCardSkeleton() {
  return (
    <article className="rounded-panel border border-ink/10 bg-white p-4">
      <Skeleton className="h-3 w-24" />
      <Skeleton className="mt-4 h-7 w-20" />
      <Skeleton className="mt-3 h-3 w-32" />
    </article>
  );
}

export function SectionCardSkeleton({ lines = 3 }: { lines?: number }) {
  return (
    <section className="rounded-panel border border-ink/10 bg-card/95 p-5 shadow-panel">
      <Skeleton className="h-5 w-40" />
      <Skeleton className="mt-2 h-3 w-56" />
      <div className="mt-5 space-y-3">
        {Array.from({ length: lines }).map((_, i) => (
          <Skeleton key={i} className="h-4 w-full" />
        ))}
      </div>
    </section>
  );
}
