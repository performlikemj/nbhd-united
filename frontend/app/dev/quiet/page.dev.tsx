"use client";

import { SleepBars, WeightLine } from "@/components/open-sky/charts";
import { GhostCircleButton, OpenSkyPageHeader, OpenSkyRow, OpenSkySection } from "@/components/open-sky/primitives";

/** DEV-ONLY (page.dev.tsx): every Open Sky primitive on one sheet. Not in production builds. */
export default function OpenSkyPrimitivesPage() {
  const swatches = [
    ["os-sky", "bg-os-sky"],
    ["os-ink", "bg-os-ink"],
    ["os-muted", "bg-os-muted"],
    ["os-faint", "bg-os-faint"],
    ["os-hairline", "bg-os-hairline"],
    ["os-accent", "bg-os-accent"],
    ["os-done", "bg-os-done"],
    ["os-attn", "bg-os-attn"],
    ["os-danger", "bg-os-danger"],
  ];
  return (
    <div className="space-y-10">
      <OpenSkyPageHeader eyebrow="Dev · primitives" title="Open Sky" subtitle="Everything the web redesign is built from." />
      <OpenSkySection label="Tokens">
        <ul className="grid grid-cols-3 gap-4 sm:grid-cols-5">
          {swatches.map(([name, cls]) => (
            <li key={name} className="flex items-center gap-3">
              <span className={`h-8 w-8 rounded-full border border-os-hairline ${cls}`} />
              <span className="text-[0.8125rem] text-os-muted">{name}</span>
            </li>
          ))}
        </ul>
      </OpenSkySection>
      <OpenSkySection label="Type">
        <p className="os-page-title">Serif page title</p>
        <p className="os-label mt-4">Section label</p>
        <p className="mt-2 text-[1.0625rem] text-os-ink">Body text on the sky, 17px.</p>
        <p className="text-[0.9375rem] text-os-muted">Muted secondary text.</p>
        <p className="text-[0.8125rem] text-os-faint">Faint helper text.</p>
      </OpenSkySection>
      <OpenSkySection label="Buttons">
        <div className="flex flex-wrap items-center gap-6">
          <button type="button" className="os-btn os-focus">Primary outline</button>
          <button type="button" className="os-btn-text os-focus">Plain text</button>
          <GhostCircleButton label="Begin" icon={<svg viewBox="0 0 24 24" className="h-5 w-5" fill="currentColor"><path d="M8 5v14l11-7z" /></svg>} />
        </div>
      </OpenSkySection>
      <OpenSkySection label="Rows">
        <OpenSkyRow title="Daily notes" detail="32" href="#" />
        <OpenSkyRow title="Weekly reviews" detail="8" href="#" />
      </OpenSkySection>
      <div className="grid gap-10 md:grid-cols-2">
        <OpenSkySection label="Sleep bars">
          <SleepBars
            nights={["M", "T", "W", "T", "F", "S", "S"].map((l, i) => ({ label: l, date: `d${i}`, hours: [7.2, 6.4, 7.8, null, 6.9, 7.5, 6.8][i] }))}
          />
        </OpenSkySection>
        <OpenSkySection label="Weight line">
          <WeightLine points={[72.9, 72.7, 72.6, 72.4, 72.2, 71.9, 71.6].map((kg, i) => ({ date: `2026-09-0${i + 1}`, kg }))} />
        </OpenSkySection>
      </div>
    </div>
  );
}
