export function ConstellationLines({ className = "" }: { className?: string }) {
  return (
    <svg
      className={`pointer-events-none absolute inset-0 h-full w-full opacity-20 ${className}`}
      xmlns="http://www.w3.org/2000/svg"
      aria-hidden="true"
    >
      <line
        x1="25%"
        y1="25%"
        x2="40%"
        y2="45%"
        stroke="white"
        strokeWidth="0.5"
        strokeDasharray="4 4"
      />
      <line
        x1="40%"
        y1="45%"
        x2="60%"
        y2="35%"
        stroke="white"
        strokeWidth="0.5"
        strokeDasharray="4 4"
      />
      <line
        x1="60%"
        y1="35%"
        x2="75%"
        y2="55%"
        stroke="white"
        strokeWidth="0.5"
        strokeDasharray="4 4"
      />
      <line
        x1="15%"
        y1="60%"
        x2="35%"
        y2="70%"
        stroke="white"
        strokeWidth="0.5"
        strokeDasharray="4 4"
      />
      <circle cx="25%" cy="25%" r="2" fill="rgba(124,107,240,0.6)" />
      <circle cx="40%" cy="45%" r="1.5" fill="rgba(255,255,255,0.5)" />
      <circle cx="60%" cy="35%" r="2" fill="rgba(78,205,196,0.6)" />
      <circle cx="75%" cy="55%" r="1.5" fill="rgba(232,180,184,0.5)" />
      <circle cx="15%" cy="60%" r="1.5" fill="rgba(255,255,255,0.4)" />
      <circle cx="35%" cy="70%" r="2" fill="rgba(124,107,240,0.5)" />
    </svg>
  );
}
