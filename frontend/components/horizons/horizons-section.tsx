import { ReactNode } from "react";

export function HorizonsSection({
  title,
  subtitle,
  children,
  delay = 0,
}: {
  title: string;
  subtitle?: string;
  children: ReactNode;
  delay?: number;
}) {
  return (
    <section
      className="glass-card-horizons p-5 shadow-panel animate-reveal sm:p-8 min-w-0 overflow-visible"
      style={{ animationDelay: `${delay}ms` }}
    >
      <header className="mb-4">
        <h2 className="font-headline text-2xl font-bold text-ink">{title}</h2>
        {subtitle ? (
          <p className="mt-1 text-sm text-ink-muted">{subtitle}</p>
        ) : null}
      </header>
      {children}
    </section>
  );
}
