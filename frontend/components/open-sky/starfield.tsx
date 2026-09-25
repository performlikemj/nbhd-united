"use client";

import { useEffect, useState } from "react";

/** Deterministic PRNG so the sky is identical on every paint (no hydration jitter). */
function rng(seed: number) {
  let s = seed >>> 0;
  return () => {
    s = (s + 0x6d2b79f5) >>> 0;
    let t = s;
    t = Math.imul(t ^ (t >>> 15), t | 1);
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

interface Star { x: number; y: number; r: number; a: number; tint: string; twinkle: boolean; delay: number }

const STARS: Star[] = (() => {
  const next = rng(0x5eed);
  return Array.from({ length: 68 }, () => {
    const t = next();
    return {
      x: next() * 1440,
      y: next() * 900,
      r: 0.5 + Math.pow(next(), 3) * 0.9,
      a: 0.16 + Math.pow(next(), 2) * 0.46,
      tint: t > 0.9 ? "#fff1de" : t > 0.75 ? "#dcebff" : "#edf3ff",
      twinkle: next() > 0.82,
      delay: next() * 6,
    };
  });
})();

/**
 * Open Sky backdrop: a faint static starfield (~68 stars per 1440×900). A few
 * stars twinkle slowly; none under prefers-reduced-motion (CSS), and the
 * animation pauses while the tab is hidden.
 */
export function OpenSkyStarfield() {
  const [hidden, setHidden] = useState(false);
  useEffect(() => {
    const onVis = () => setHidden(document.visibilityState === "hidden");
    document.addEventListener("visibilitychange", onVis);
    return () => document.removeEventListener("visibilitychange", onVis);
  }, []);

  return (
    <svg
      aria-hidden="true"
      className="pointer-events-none fixed inset-0 -z-10 h-full w-full"
      viewBox="0 0 1440 900"
      preserveAspectRatio="xMidYMid slice"
      style={{ background: "var(--os-sky)" }}
    >
      {STARS.map((s, i) => (
        <circle
          key={i}
          cx={s.x}
          cy={s.y}
          r={s.r}
          fill={s.tint}
          opacity={s.a}
          className={s.twinkle ? "os-twinkle" : undefined}
          style={
            s.twinkle
              ? ({
                  ["--os-a" as string]: s.a,
                  animationDelay: `${s.delay}s`,
                  animationPlayState: hidden ? "paused" : "running",
                } as React.CSSProperties)
              : undefined
          }
        />
      ))}
    </svg>
  );
}
