"use client";

/**
 * A subtle SVG synapse/constellation network that sits behind content.
 * Nodes are connected by soft branching lines with gentle pulse animations.
 * Renders at low opacity to reinforce the neural/constellation metaphor.
 */
export function SynapseNetwork({ className = "" }: { className?: string }) {
  return (
    <svg
      className={`pointer-events-none absolute inset-0 h-full w-full ${className}`}
      viewBox="0 0 1440 900"
      preserveAspectRatio="xMidYMid slice"
      xmlns="http://www.w3.org/2000/svg"
      aria-hidden="true"
    >
      <defs>
        {/* Soft glow for nodes */}
        <radialGradient id="syn-glow-purple" cx="50%" cy="50%" r="50%">
          <stop offset="0%" stopColor="#7C6BF0" stopOpacity="0.6" />
          <stop offset="100%" stopColor="#7C6BF0" stopOpacity="0" />
        </radialGradient>
        <radialGradient id="syn-glow-teal" cx="50%" cy="50%" r="50%">
          <stop offset="0%" stopColor="#4ECDC4" stopOpacity="0.5" />
          <stop offset="100%" stopColor="#4ECDC4" stopOpacity="0" />
        </radialGradient>
        <radialGradient id="syn-glow-pink" cx="50%" cy="50%" r="50%">
          <stop offset="0%" stopColor="#E8B4B8" stopOpacity="0.5" />
          <stop offset="100%" stopColor="#E8B4B8" stopOpacity="0" />
        </radialGradient>
        <radialGradient id="syn-glow-white" cx="50%" cy="50%" r="50%">
          <stop offset="0%" stopColor="#ffffff" stopOpacity="0.4" />
          <stop offset="100%" stopColor="#ffffff" stopOpacity="0" />
        </radialGradient>

        {/* Gradient for connection lines */}
        <linearGradient id="syn-line-fade" x1="0%" y1="0%" x2="100%" y2="0%">
          <stop offset="0%" stopColor="#7C6BF0" stopOpacity="0.3" />
          <stop offset="50%" stopColor="#4ECDC4" stopOpacity="0.15" />
          <stop offset="100%" stopColor="#E8B4B8" stopOpacity="0.3" />
        </linearGradient>
      </defs>

      {/* ── Connection lines (dendrites) ── */}
      <g strokeWidth="0.75" fill="none" opacity="0.35">
        {/* Cluster A — upper left */}
        <path d="M120,180 Q200,120 310,200" stroke="#7C6BF0" />
        <path d="M310,200 Q380,250 420,160" stroke="#7C6BF0" />
        <path d="M310,200 Q340,300 260,340" stroke="#4ECDC4" />
        <path d="M120,180 Q80,260 160,320" stroke="#E8B4B8" />
        <path d="M160,320 Q220,340 260,340" stroke="#E8B4B8" />

        {/* Cluster B — center top */}
        <path d="M620,80 Q700,140 760,100" stroke="#4ECDC4" />
        <path d="M620,80 Q640,170 720,200" stroke="#7C6BF0" />
        <path d="M720,200 Q800,180 760,100" stroke="white" />
        <path d="M720,200 Q680,280 740,340" stroke="#4ECDC4" />

        {/* Cluster C — upper right */}
        <path d="M1080,120 Q1140,80 1220,140" stroke="#E8B4B8" />
        <path d="M1220,140 Q1280,200 1200,260" stroke="#7C6BF0" />
        <path d="M1080,120 Q1040,200 1100,250" stroke="#4ECDC4" />
        <path d="M1100,250 Q1160,280 1200,260" stroke="white" />
        <path d="M1200,260 Q1300,300 1340,240" stroke="#E8B4B8" />

        {/* Cluster D — center */}
        <path d="M580,420 Q660,380 740,440" stroke="#7C6BF0" />
        <path d="M740,440 Q820,460 860,400" stroke="#4ECDC4" />
        <path d="M580,420 Q540,500 620,540" stroke="#E8B4B8" />
        <path d="M620,540 Q700,520 740,440" stroke="white" />

        {/* Cluster E — lower left */}
        <path d="M160,580 Q240,540 300,600" stroke="#4ECDC4" />
        <path d="M300,600 Q360,660 320,720" stroke="#7C6BF0" />
        <path d="M160,580 Q120,660 200,720" stroke="white" />
        <path d="M200,720 Q260,740 320,720" stroke="#E8B4B8" />

        {/* Cluster F — lower right */}
        <path d="M1040,560 Q1120,520 1180,580" stroke="#E8B4B8" />
        <path d="M1180,580 Q1240,640 1160,700" stroke="#7C6BF0" />
        <path d="M1040,560 Q1000,640 1060,700" stroke="#4ECDC4" />
        <path d="M1060,700 Q1120,720 1160,700" stroke="white" />

        {/* Long-range connections between clusters */}
        <path d="M420,160 Q520,120 620,80" stroke="url(#syn-line-fade)" strokeDasharray="6 8" />
        <path d="M260,340 Q420,380 580,420" stroke="url(#syn-line-fade)" strokeDasharray="6 8" />
        <path d="M740,340 Q900,300 1080,120" stroke="url(#syn-line-fade)" strokeDasharray="6 8" />
        <path d="M860,400 Q960,480 1040,560" stroke="url(#syn-line-fade)" strokeDasharray="6 8" />
        <path d="M620,540 Q440,560 300,600" stroke="url(#syn-line-fade)" strokeDasharray="6 8" />
        <path d="M740,440 Q900,520 1040,560" stroke="url(#syn-line-fade)" strokeDasharray="6 8" />
      </g>

      {/* ── Nodes ── */}
      <g>
        {/* Cluster A nodes */}
        <circle cx="120" cy="180" r="8" fill="url(#syn-glow-purple)" className="animate-twinkle" />
        <circle cx="120" cy="180" r="2" fill="#7C6BF0" opacity="0.7" />
        <circle cx="310" cy="200" r="10" fill="url(#syn-glow-teal)" className="animate-twinkle-slow" />
        <circle cx="310" cy="200" r="2.5" fill="#4ECDC4" opacity="0.6" />
        <circle cx="420" cy="160" r="6" fill="url(#syn-glow-white)" className="animate-twinkle" />
        <circle cx="420" cy="160" r="1.5" fill="white" opacity="0.5" />
        <circle cx="260" cy="340" r="8" fill="url(#syn-glow-pink)" className="animate-twinkle-slow" />
        <circle cx="260" cy="340" r="2" fill="#E8B4B8" opacity="0.6" />
        <circle cx="160" cy="320" r="6" fill="url(#syn-glow-purple)" className="animate-twinkle-fast" />
        <circle cx="160" cy="320" r="1.5" fill="#7C6BF0" opacity="0.5" />

        {/* Cluster B nodes */}
        <circle cx="620" cy="80" r="10" fill="url(#syn-glow-purple)" className="animate-twinkle-slow" />
        <circle cx="620" cy="80" r="2.5" fill="#7C6BF0" opacity="0.7" />
        <circle cx="760" cy="100" r="6" fill="url(#syn-glow-teal)" className="animate-twinkle" />
        <circle cx="760" cy="100" r="1.5" fill="#4ECDC4" opacity="0.5" />
        <circle cx="720" cy="200" r="8" fill="url(#syn-glow-white)" className="animate-twinkle-fast" />
        <circle cx="720" cy="200" r="2" fill="white" opacity="0.5" />
        <circle cx="740" cy="340" r="7" fill="url(#syn-glow-pink)" className="animate-twinkle" />
        <circle cx="740" cy="340" r="1.5" fill="#E8B4B8" opacity="0.5" />

        {/* Cluster C nodes */}
        <circle cx="1080" cy="120" r="8" fill="url(#syn-glow-teal)" className="animate-twinkle" />
        <circle cx="1080" cy="120" r="2" fill="#4ECDC4" opacity="0.6" />
        <circle cx="1220" cy="140" r="10" fill="url(#syn-glow-pink)" className="animate-twinkle-slow" />
        <circle cx="1220" cy="140" r="2.5" fill="#E8B4B8" opacity="0.7" />
        <circle cx="1200" cy="260" r="8" fill="url(#syn-glow-purple)" className="animate-twinkle-fast" />
        <circle cx="1200" cy="260" r="2" fill="#7C6BF0" opacity="0.6" />
        <circle cx="1100" cy="250" r="6" fill="url(#syn-glow-white)" className="animate-twinkle" />
        <circle cx="1100" cy="250" r="1.5" fill="white" opacity="0.5" />
        <circle cx="1340" cy="240" r="7" fill="url(#syn-glow-teal)" className="animate-twinkle-slow" />
        <circle cx="1340" cy="240" r="1.5" fill="#4ECDC4" opacity="0.5" />

        {/* Cluster D nodes */}
        <circle cx="580" cy="420" r="8" fill="url(#syn-glow-pink)" className="animate-twinkle" />
        <circle cx="580" cy="420" r="2" fill="#E8B4B8" opacity="0.6" />
        <circle cx="740" cy="440" r="10" fill="url(#syn-glow-purple)" className="animate-twinkle-slow" />
        <circle cx="740" cy="440" r="2.5" fill="#7C6BF0" opacity="0.7" />
        <circle cx="860" cy="400" r="7" fill="url(#syn-glow-teal)" className="animate-twinkle-fast" />
        <circle cx="860" cy="400" r="1.5" fill="#4ECDC4" opacity="0.5" />
        <circle cx="620" cy="540" r="8" fill="url(#syn-glow-white)" className="animate-twinkle" />
        <circle cx="620" cy="540" r="2" fill="white" opacity="0.5" />

        {/* Cluster E nodes */}
        <circle cx="160" cy="580" r="8" fill="url(#syn-glow-teal)" className="animate-twinkle-slow" />
        <circle cx="160" cy="580" r="2" fill="#4ECDC4" opacity="0.6" />
        <circle cx="300" cy="600" r="10" fill="url(#syn-glow-purple)" className="animate-twinkle" />
        <circle cx="300" cy="600" r="2.5" fill="#7C6BF0" opacity="0.7" />
        <circle cx="320" cy="720" r="7" fill="url(#syn-glow-pink)" className="animate-twinkle-fast" />
        <circle cx="320" cy="720" r="1.5" fill="#E8B4B8" opacity="0.5" />
        <circle cx="200" cy="720" r="6" fill="url(#syn-glow-white)" className="animate-twinkle" />
        <circle cx="200" cy="720" r="1.5" fill="white" opacity="0.5" />

        {/* Cluster F nodes */}
        <circle cx="1040" cy="560" r="8" fill="url(#syn-glow-pink)" className="animate-twinkle" />
        <circle cx="1040" cy="560" r="2" fill="#E8B4B8" opacity="0.6" />
        <circle cx="1180" cy="580" r="10" fill="url(#syn-glow-teal)" className="animate-twinkle-slow" />
        <circle cx="1180" cy="580" r="2.5" fill="#4ECDC4" opacity="0.7" />
        <circle cx="1160" cy="700" r="8" fill="url(#syn-glow-purple)" className="animate-twinkle-fast" />
        <circle cx="1160" cy="700" r="2" fill="#7C6BF0" opacity="0.6" />
        <circle cx="1060" cy="700" r="6" fill="url(#syn-glow-white)" className="animate-twinkle" />
        <circle cx="1060" cy="700" r="1.5" fill="white" opacity="0.5" />
      </g>
    </svg>
  );
}
