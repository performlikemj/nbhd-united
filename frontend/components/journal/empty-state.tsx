"use client";

interface EmptyStateProps {
  title?: string;
  subtitle?: string;
}

function ConstellationDots() {
  return (
    <svg
      viewBox="0 0 200 120"
      className="w-40 h-24 mx-auto mb-6 opacity-40"
      fill="none"
      xmlns="http://www.w3.org/2000/svg"
    >
      {/* Stars */}
      <circle cx="40" cy="30" r="1.5" className="stardust-dot" style={{ animationDelay: "0s" }} fill="#7C6BF0" />
      <circle cx="70" cy="50" r="2" className="stardust-dot" style={{ animationDelay: "0.3s" }} fill="#4ECDC4" />
      <circle cx="100" cy="25" r="1" className="stardust-dot" style={{ animationDelay: "0.6s" }} fill="#E8B4B8" />
      <circle cx="130" cy="55" r="2.5" className="stardust-dot" style={{ animationDelay: "0.9s" }} fill="#7C6BF0" />
      <circle cx="160" cy="35" r="1.5" className="stardust-dot" style={{ animationDelay: "1.2s" }} fill="#4ECDC4" />
      <circle cx="90" cy="75" r="2" className="stardust-dot" style={{ animationDelay: "0.5s" }} fill="#E8B4B8" />
      <circle cx="55" cy="60" r="1" className="stardust-dot" style={{ animationDelay: "0.8s" }} fill="#64748B" />
      <circle cx="140" cy="70" r="1.5" className="stardust-dot" style={{ animationDelay: "1.0s" }} fill="#64748B" />
      {/* Constellation lines */}
      <g className="constellation-line" style={{ opacity: 0.2 }}>
        <line x1="40" y1="30" x2="70" y2="50" stroke="#7C6BF0" strokeWidth="0.5" />
        <line x1="70" y1="50" x2="100" y2="25" stroke="#7C6BF0" strokeWidth="0.5" />
        <line x1="100" y1="25" x2="130" y2="55" stroke="#4ECDC4" strokeWidth="0.5" />
        <line x1="130" y1="55" x2="160" y2="35" stroke="#4ECDC4" strokeWidth="0.5" />
        <line x1="70" y1="50" x2="90" y2="75" stroke="#E8B4B8" strokeWidth="0.5" />
        <line x1="90" y1="75" x2="130" y2="55" stroke="#E8B4B8" strokeWidth="0.5" />
        <line x1="55" y1="60" x2="70" y2="50" stroke="#64748B" strokeWidth="0.5" />
        <line x1="140" y1="70" x2="130" y2="55" stroke="#64748B" strokeWidth="0.5" />
      </g>
    </svg>
  );
}

export function EmptyState({
  title = "This orbit is quiet.",
  subtitle = "Your journal will fill as you talk with your assistant.",
}: EmptyStateProps) {
  return (
    <div className="flex flex-col items-center justify-center h-full px-6 py-16 text-center">
      <ConstellationDots />
      <h3 className="text-sm font-medium text-ink-muted">
        {title}
      </h3>
      <p className="mt-2 text-xs text-ink-faint max-w-xs leading-relaxed">
        {subtitle}
      </p>
    </div>
  );
}
