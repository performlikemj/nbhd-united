"use client";

import Link from "next/link";

import { useHorizonsQuery } from "@/lib/queries";
import type { HorizonsAssistantInsight } from "@/lib/types";

/**
 * Inline strip on the Gravity tab — surfaces the 3 most recent open Gravity
 * insights with a deep-link to Horizons for the full record. Read-only —
 * confirm/dismiss lives in Horizons "What I remember."
 */
export function GravityInsightsStrip() {
  const { data } = useHorizonsQuery();
  const insights: HorizonsAssistantInsight[] = (data?.assistant_insights ?? [])
    .filter((i) => i.pillar === "gravity" && i.status === "open")
    .slice(0, 3);

  if (insights.length === 0) {
    return null;
  }

  return (
    <section
      className="mb-10 sm:mb-12 animate-reveal"
      style={{ animationDelay: "150ms" }}
      aria-label="Recent observations from your assistant"
    >
      <div className="glass-card-horizons border-l-2 border-l-accent p-5 sm:p-6">
        <div className="flex items-start justify-between gap-3">
          <div>
            <p className="font-mono text-[10px] uppercase tracking-wider text-accent">
              Your assistant noticed
            </p>
            <p className="mt-1 text-xs text-ink-muted">
              {insights.length} new observation{insights.length === 1 ? "" : "s"} waiting for your confirmation.
            </p>
          </div>
          <Link
            href="/horizons"
            className="hidden text-xs text-accent transition hover:text-accent-hover sm:inline-flex sm:items-center"
          >
            View in Horizons &rarr;
          </Link>
        </div>

        <ul className="mt-4 space-y-2">
          {insights.map((insight) => (
            <li
              key={insight.id}
              className="flex items-start gap-3 border-t border-border pt-3 first:border-t-0 first:pt-0"
            >
              <span
                aria-hidden="true"
                className="mt-1 inline-block h-1.5 w-1.5 shrink-0 rounded-full bg-accent"
              />
              <p className="text-sm leading-relaxed text-ink">{insight.statement}</p>
            </li>
          ))}
        </ul>

        <Link
          href="/horizons"
          className="mt-4 inline-flex text-xs text-accent transition hover:text-accent-hover sm:hidden"
        >
          Confirm in Horizons &rarr;
        </Link>
      </div>
    </section>
  );
}
