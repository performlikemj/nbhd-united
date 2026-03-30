"use client";

import clsx from "clsx";
import { useState } from "react";

import { useOnboardMutation, useMeQuery, usePersonasQuery } from "@/lib/queries";
import type { PersonaOption } from "@/lib/api";

const autoOnboardAttempted = new Set<string>();

export function PersonaScene() {
  const { data: me } = useMeQuery();
  const { data: personas } = usePersonasQuery();
  const { mutateAsync: onboardTenant, isPending, isSuccess } = useOnboardMutation();
  const [selected, setSelected] = useState("neighbor");

  const handleContinue = async () => {
    const userId = me?.id;
    if (!userId || isPending || isSuccess) return;
    if (autoOnboardAttempted.has(userId)) return;
    autoOnboardAttempted.add(userId);
    try {
      await onboardTenant({ display_name: me.display_name, agent_persona: selected });
    } catch {
      autoOnboardAttempted.delete(userId);
    }
  };

  return (
    <div className="w-full max-w-[580px] flex flex-col items-center text-center">
      <span className="font-mono text-[11px] uppercase tracking-[0.2em] text-[#5dd9d0] mb-4">
        STEP 1 OF 3
      </span>

      <h1 className="font-display text-3xl sm:text-5xl font-extrabold text-[#e0e3e8] tracking-tight mb-3 leading-tight">
        Choose your guide
      </h1>

      <p className="text-white/50 text-[15px] max-w-[400px] leading-relaxed mb-10">
        Pick a personality for your AI companion. You can change this anytime in settings.
      </p>

      {personas && (
        <div className="grid gap-3 sm:grid-cols-2 w-full mb-8">
          {personas.map((p: PersonaOption) => {
            const isSelected = p.key === selected;
            return (
              <button
                key={p.key}
                type="button"
                onClick={() => setSelected(p.key)}
                className={clsx(
                  "flex items-start gap-3 rounded-[16px] p-4 text-left transition cursor-pointer",
                  "bg-[#12161b]/50 backdrop-blur-sm border",
                  isSelected
                    ? "border-[#7C6BF0]/40 shadow-[0_0_20px_rgba(124,107,240,0.15)]"
                    : "border-white/[0.06] hover:border-white/[0.12]",
                )}
              >
                <span className="text-2xl leading-none">{p.emoji}</span>
                <div className="flex-1">
                  <p className={clsx("font-medium text-sm", isSelected ? "text-[#c7bfff]" : "text-[#e0e3e8]")}>
                    {p.label}
                  </p>
                  <p className="mt-0.5 text-xs text-white/40 leading-relaxed">{p.description}</p>
                </div>
                {isSelected && (
                  <svg className="h-5 w-5 shrink-0 text-[#c7bfff]" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2.5}>
                    <path strokeLinecap="round" strokeLinejoin="round" d="M4.5 12.75l6 6 9-13.5" />
                  </svg>
                )}
              </button>
            );
          })}
        </div>
      )}

      <button
        type="button"
        onClick={handleContinue}
        disabled={isPending}
        className="glow-purple rounded-full bg-[#7C6BF0] px-8 py-3 text-sm font-semibold text-white transition-all hover:brightness-110 active:scale-95 disabled:opacity-50 disabled:cursor-not-allowed"
      >
        {isPending ? "Setting up..." : "Continue"}
      </button>
    </div>
  );
}
