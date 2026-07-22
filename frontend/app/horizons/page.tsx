"use client";

import { useState, type ReactNode } from "react";

import { GoalCard } from "@/components/goal-card";
import { MoodTrendSparkline } from "@/components/horizons/mood-trend-sparkline";
import { NorthStarSection } from "@/components/horizons/north-star-section";
import { TopicSignalsSection } from "@/components/horizons/topic-signals-section";
import { InsightCard } from "@/components/insight-card";
import { MomentumStrip } from "@/components/momentum-strip";
import { PendingGoal } from "@/components/pending-goal";
import { WeeklyPulse } from "@/components/weekly-pulse";
import { useHorizonsQuery } from "@/lib/queries";

function QuietSection({
  title,
  subtitle,
  delay,
  children,
}: {
  title: string;
  subtitle?: string;
  delay: number;
  children: ReactNode;
}) {
  return (
    <section
      className="animate-reveal min-w-0 motion-reduce:animate-none"
      style={{ animationDelay: `${delay}ms` }}
    >
      <header className="mb-4 sm:mb-5">
        <h2 className="font-headline text-xl font-medium tracking-tight text-ink sm:text-2xl">
          {title}
        </h2>
        {subtitle ? (
          <p className="mt-1 max-w-2xl text-sm text-ink-muted">{subtitle}</p>
        ) : null}
      </header>
      {children}
    </section>
  );
}

function HorizonsSkeleton() {
  return (
    <div className="space-y-6 sm:space-y-8">
      <div>
        <h1 className="font-headline text-4xl font-semibold tracking-tight text-ink sm:text-5xl">
          Horizons
        </h1>
        <p className="mt-2 text-lg font-light text-ink-muted">
          Your goals, your momentum.
        </p>
      </div>
      {[1, 2, 3].map((i) => (
        <div
          key={i}
          className="glass-card-horizons animate-pulse p-5 sm:p-8"
        >
          <div className="mb-4 h-6 w-32 rounded bg-surface-elevated" />
          <div className="space-y-2">
            {Array.from({ length: i + 1 }).map((_, j) => (
              <div
                key={j}
                className="h-4 rounded bg-surface-elevated"
                style={{ width: `${70 - j * 15}%` }}
              />
            ))}
          </div>
        </div>
      ))}
    </div>
  );
}

export default function HorizonsPage() {
  const { data, isLoading, error } = useHorizonsQuery();
  const [showAllInsights, setShowAllInsights] = useState(false);

  if (isLoading) {
    return <HorizonsSkeleton />;
  }

  if (error) {
    return (
      <div className="space-y-6">
        <div>
          <h1 className="font-headline text-4xl font-semibold tracking-tight text-ink sm:text-5xl">
            Horizons
          </h1>
        </div>
        <div className="glass-card-horizons p-5 text-sm text-rose-text sm:p-8">
          Failed to load Horizons.{" "}
          {error instanceof Error ? error.message : "Please try again."}
        </div>
      </div>
    );
  }

  if (!data) return null;

  const insights = data.assistant_insights ?? [];
  const visibleInsights = showAllInsights ? insights : insights.slice(0, 3);

  return (
    <div className="space-y-8 sm:space-y-12">
      <div className="space-y-2">
        <h1 className="font-headline text-4xl font-semibold tracking-tight text-ink sm:text-5xl">
          Horizons
        </h1>
        <p className="text-lg font-light text-ink-muted">
          Your goals, your momentum.
        </p>
      </div>

      <NorthStarSection items={data.north_star ?? []} delay={80} />

      <section
        className="glass-card-horizons min-w-0 animate-reveal p-5 motion-reduce:animate-none sm:p-6"
        style={{ animationDelay: "140ms" }}
      >
        <MomentumStrip days={data.momentum} streak={data.current_streak} />
      </section>

      <QuietSection title="Active Goals" delay={220}>
        {data.goals.length > 0 ? (
          <div className="grid grid-cols-1 gap-4 md:grid-cols-2 md:gap-6">
            {data.goals.map((goal) => (
              <GoalCard key={goal.id} goal={goal} />
            ))}
          </div>
        ) : (
          <div className="glass-card-horizons p-5 sm:p-6">
            <p className="py-4 text-center text-sm text-ink-muted">
              No goals yet. Write about your goals in your journal, and your
              assistant will help you track them.
            </p>
          </div>
        )}
      </QuietSection>

      {data.pending_extractions.length > 0 ? (
        <QuietSection title="Waiting on you" delay={300}>
          <div className="grid grid-cols-1 gap-4 md:grid-cols-2 md:gap-6 lg:grid-cols-3">
            {data.pending_extractions.map((extraction) => (
              <PendingGoal key={extraction.id} extraction={extraction} />
            ))}
          </div>
        </QuietSection>
      ) : null}

      <QuietSection title="Reflection" delay={380}>
        <div className="glass-card-horizons p-5 sm:p-6">
          <div className="mb-3 flex flex-wrap items-center justify-between gap-3">
            <h3 className="font-headline text-base font-medium text-ink sm:text-lg">
              Weekly Pulse
            </h3>
            <MoodTrendSparkline entries={data.mood_trend ?? []} />
          </div>
          <WeeklyPulse
            weeks={data.weekly_pulse}
            documents={data.weekly_documents}
          />
        </div>
      </QuietSection>

      {insights.length > 0 ? (
        <QuietSection
          title="What I remember"
          subtitle="Patterns I've noticed across your pillars. Confirm or correct so I keep getting it right."
          delay={460}
        >
          <div className="grid grid-cols-1 gap-4 md:grid-cols-2 md:gap-6">
            {visibleInsights.map((insight) => (
              <InsightCard key={insight.id} insight={insight} />
            ))}
          </div>
          {insights.length > 3 ? (
            <button
              type="button"
              onClick={() => setShowAllInsights((shown) => !shown)}
              aria-expanded={showAllInsights}
              className="mt-4 min-h-[44px] rounded-full border border-border px-4 py-2 text-sm font-medium text-ink-muted transition hover:border-border-strong hover:bg-surface-hover hover:text-ink"
            >
              {showAllInsights ? "Show fewer" : `Show all (${insights.length})`}
            </button>
          ) : null}
        </QuietSection>
      ) : null}

      <TopicSignalsSection signals={data.topic_signals ?? []} delay={520} />
    </div>
  );
}
