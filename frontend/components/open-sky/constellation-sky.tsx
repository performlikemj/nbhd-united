"use client";

import { useLayoutEffect, useMemo, useState } from "react";

import { GhostCircleButton } from "@/components/open-sky/primitives";
import { SkyCanvas } from "@/components/open-sky/sky-canvas";
import { MilkyWayRenderer, skyThemes } from "@/lib/sky-art/milky-way";
import type { ConstellationData } from "@/lib/types";

const Chevron = ({ dir }: { dir: "left" | "right" }) => (
  <svg width="18" height="18" viewBox="0 0 24 24" fill="none" aria-hidden="true">
    <path d={dir === "left" ? "M15 5l-7 7 7 7" : "M9 5l7 7-7 7"} stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" />
  </svg>
);

/**
 * Constellation hero: the Milky Way with each of the user's themes drawn as a
 * constellation (same stable layout as the iPhone). Theme names are real
 * buttons; Previous / Next step through; Fly in opens the galaxy when enabled.
 */
export function ConstellationSky({ data, playEnabled }: { data: ConstellationData; playEnabled: boolean }) {
  const themes = useMemo(() => skyThemes(data), [data]);
  const [painter] = useState(() => new MilkyWayRenderer());
  const [selected, setSelected] = useState(0);
  const index = themes.length ? Math.min(selected, themes.length - 1) : 0;
  // Layout effect: lands before SkyCanvas's (passive) repaint for this change.
  useLayoutEffect(() => {
    painter.setScene(themes, index);
  }, [painter, themes, index]);
  const theme = themes[index];
  const step = (d: number) => setSelected((s) => (themes.length ? (s + d + themes.length) % themes.length : 0));

  return (
    <section aria-label="Your themes in the sky" className="relative -mx-4 sm:-mx-6 md:mx-0">
      <div className="relative overflow-hidden">
        <SkyCanvas
          painter={painter}
          redrawKey={`${index}:${themes.length}`}
          label={themes.length ? `The Milky Way with ${themes.length} of your themes drawn as constellations` : "The Milky Way"}
          className="os-sky-fade block h-[440px] w-full sm:h-[520px]"
        />
        {themes.map((th, k) => (
          <button
            key={th.id}
            type="button"
            onClick={() => setSelected(k)}
            aria-pressed={k === index}
            className={`os-focus absolute min-h-[32px] -translate-y-full whitespace-nowrap rounded px-1 text-[0.75rem] tracking-[0.08em] transition ${k === index ? "text-os-ink" : "text-os-muted hover:text-os-ink"}`}
            style={
              th.label.x > 0.6
                ? { right: `${Math.max(3, (1 - Math.max(...th.stars.map((p) => p.x)) - 0.02) * 100)}%`, top: `${(th.label.y + 0.045) * 100}%` }
                : { left: `${th.label.x * 100}%`, top: `${(th.label.y + 0.045) * 100}%` }
            }
          >
            {th.name}
          </button>
        ))}
        {!themes.length ? (
          <p className="absolute inset-x-0 top-1/2 -translate-y-1/2 px-6 text-center text-[0.9375rem] text-os-muted">
            Your themes will appear here as you save lessons.
          </p>
        ) : null}
      </div>

      {theme ? (
        <div className="px-4 pt-2 sm:px-6 md:px-0">
          <p className="os-label">
            Theme · {lessonCount(theme.count)}
          </p>
          <h2 className="mt-2 font-serif text-[1.75rem] leading-tight text-os-ink">{theme.name}</h2>
          <p className="mt-2 max-w-[56ch] text-[0.9375rem] text-os-muted">{theme.lessons[0]?.text}</p>
          <div className="mt-6 flex items-start gap-8">
            <GhostCircleButton label="Previous" icon={<Chevron dir="left" />} onClick={() => step(-1)} disabled={themes.length < 2} />
            {playEnabled ? (
              <GhostCircleButton
                label="Fly in"
                href="/constellation/play"
                icon={
                  <svg width="14" height="14" viewBox="0 0 10 10" fill="none" aria-hidden="true">
                    <path d="M2 1.4l6.2 3.6L2 8.6z" fill="currentColor" />
                  </svg>
                }
              />
            ) : null}
            <GhostCircleButton label="Next" icon={<Chevron dir="right" />} onClick={() => step(1)} disabled={themes.length < 2} />
          </div>
        </div>
      ) : null}
    </section>
  );
}

function lessonCount(n: number) {
  return `${n} ${n === 1 ? "lesson" : "lessons"}`;
}
