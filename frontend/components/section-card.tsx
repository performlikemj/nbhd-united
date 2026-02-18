import { ReactNode } from "react";

export function SectionCard({
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
      className="rounded-panel border border-ink/10 bg-card/95 p-4 shadow-panel animate-reveal sm:p-5 min-w-0 overflow-hidden"
      style={{ animationDelay: `${delay}ms` }}
    >
      <header className="mb-4">
        <h2 className="text-xl font-semibold text-ink">{title}</h2>
        {subtitle ? <p className="mt-1 text-sm text-ink/65">{subtitle}</p> : null}
      </header>
      {children}
    </section>
  );
}
