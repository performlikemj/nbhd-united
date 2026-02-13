"use client";

import clsx from "clsx";

import type { PersonaOption } from "@/lib/api";

export function PersonaSelector({
  personas,
  selected,
  onSelect,
}: {
  personas: PersonaOption[];
  selected: string;
  onSelect: (key: string) => void;
}) {
  return (
    <div className="grid gap-3 sm:grid-cols-2">
      {personas.map((p) => {
        const isSelected = p.key === selected;
        return (
          <button
            key={p.key}
            type="button"
            onClick={() => onSelect(p.key)}
            className={clsx(
              "flex items-start gap-3 rounded-panel border p-4 text-left transition cursor-pointer",
              isSelected
                ? "border-accent bg-accent/5 ring-2 ring-accent/20"
                : "border-ink/15 bg-white hover:border-ink/30",
            )}
          >
            <span className="text-2xl leading-none">{p.emoji}</span>
            <div className="flex-1">
              <p className={clsx("font-medium", isSelected ? "text-accent" : "text-ink")}>
                {p.label}
              </p>
              <p className="mt-0.5 text-sm text-ink/60">{p.description}</p>
            </div>
            {isSelected && (
              <svg className="h-5 w-5 shrink-0 text-accent" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2.5}>
                <path strokeLinecap="round" strokeLinejoin="round" d="M4.5 12.75l6 6 9-13.5" />
              </svg>
            )}
          </button>
        );
      })}
    </div>
  );
}
