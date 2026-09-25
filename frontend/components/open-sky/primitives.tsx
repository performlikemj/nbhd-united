import clsx from "clsx";
import type { ReactNode } from "react";

/** One serif title per page, with an optional small uppercase eyebrow and a trailing slot. */
export function OpenSkyPageHeader({
  eyebrow,
  title,
  subtitle,
  actions,
}: {
  eyebrow?: ReactNode;
  title: ReactNode;
  subtitle?: ReactNode;
  actions?: ReactNode;
}) {
  return (
    <header className="flex flex-wrap items-end justify-between gap-x-6 gap-y-3 pb-6">
      <div className="min-w-0">
        {eyebrow ? <p className="os-label mb-2">{eyebrow}</p> : null}
        <h1 className="os-page-title">{title}</h1>
        {subtitle ? <p className="mt-2 text-[0.9375rem] text-os-muted">{subtitle}</p> : null}
      </div>
      {actions ? <div className="flex items-center gap-3">{actions}</div> : null}
    </header>
  );
}

/** A group of content on the sky: hairline above, small uppercase label, no box. */
export function OpenSkySection({
  label,
  trailing,
  children,
  className,
  id,
}: {
  label?: ReactNode;
  trailing?: ReactNode;
  children: ReactNode;
  className?: string;
  id?: string;
}) {
  return (
    <section id={id} aria-label={typeof label === "string" ? label : undefined} className={clsx("os-hairline-top pt-4", className)}>
      {label || trailing ? (
        <div className="mb-3 flex items-center justify-between gap-3">
          {label ? <h2 className="os-label">{label}</h2> : <span />}
          {trailing}
        </div>
      ) : null}
      {children}
    </section>
  );
}

/** Ghost circle with a word underneath — the iPhone Talk/Core control language. */
export function GhostCircleButton({
  label,
  icon,
  onClick,
  href,
  size = 52,
  disabled,
}: {
  label: string;
  icon: ReactNode;
  onClick?: () => void;
  href?: string;
  size?: number;
  disabled?: boolean;
}) {
  const circle = (
    <span
      className="flex items-center justify-center rounded-full border border-os-ring text-white transition group-hover:border-os-accent-line group-hover:text-os-accent"
      style={{ width: size, height: size }}
      aria-hidden="true"
    >
      {icon}
    </span>
  );
  const word = <span className="text-[0.8125rem] text-os-muted">{label}</span>;
  const cls = "group os-focus inline-flex flex-col items-center gap-2 rounded-2xl disabled:opacity-40";
  if (href) {
    return (
      <a href={href} className={cls} aria-label={label}>
        {circle}
        {word}
      </a>
    );
  }
  return (
    <button type="button" onClick={onClick} disabled={disabled} className={cls} aria-label={label}>
      {circle}
      {word}
    </button>
  );
}

/** A plain row: title left, quiet detail right, hairline above. */
export function OpenSkyRow({
  title,
  detail,
  href,
  onClick,
}: {
  title: ReactNode;
  detail?: ReactNode;
  href?: string;
  onClick?: () => void;
}) {
  const inner = (
    <>
      <span className="min-w-0 truncate text-[1.0625rem] text-os-ink">{title}</span>
      {detail ? <span className="shrink-0 text-[0.875rem] text-os-muted">{detail}</span> : null}
    </>
  );
  const cls = "os-focus flex min-h-[52px] w-full items-center justify-between gap-4 os-hairline-top text-left";
  if (href) return <a href={href} className={cls}>{inner}</a>;
  if (onClick) return <button type="button" onClick={onClick} className={cls}>{inner}</button>;
  return <div className={cls}>{inner}</div>;
}
