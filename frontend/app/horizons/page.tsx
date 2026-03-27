"use client";

import { GoalCard } from "@/components/goal-card";
import { HorizonsSection } from "@/components/horizons/horizons-section";
import { MomentumStrip } from "@/components/momentum-strip";
import { PendingGoal } from "@/components/pending-goal";
import { WeeklyPulse } from "@/components/weekly-pulse";
import { useHorizonsQuery } from "@/lib/queries";

function HorizonsSkeleton() {
  return (
    <div className="space-y-6 sm:space-y-8">
      <div>
        <h1 className="font-headline text-5xl font-bold tracking-tight text-ink md:text-7xl">
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

  if (isLoading) {
    return <HorizonsSkeleton />;
  }

  if (error) {
    return (
      <div className="space-y-6">
        <div>
          <h1 className="font-headline text-5xl font-bold tracking-tight text-ink md:text-7xl">
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

  const realGoals = data.goals.filter(
    (g) => !g.preview.includes("[Goal Name]") && !g.preview.includes("[Specific, measurable outcome]"),
  );

  return (
    <div className="space-y-8 sm:space-y-12">
      {/* Hero Header */}
      <div className="space-y-2">
        <h1 className="font-headline text-5xl font-bold tracking-tight text-ink md:text-7xl">
          Horizons
        </h1>
        <p className="text-lg font-light text-ink-muted">
          Your goals, your momentum.
        </p>
      </div>

      {/* Momentum — full width */}
      <HorizonsSection title="Momentum" subtitle="Last 30 days" delay={100}>
        <MomentumStrip days={data.momentum} streak={data.current_streak} />
      </HorizonsSection>

      {/* Weekly Pulse + Active Goals — side by side on large screens */}
      <div className="grid grid-cols-1 gap-8 lg:grid-cols-3">
        <div className="lg:col-span-1">
          <HorizonsSection title="Weekly Pulse" delay={200}>
            <WeeklyPulse weeks={data.weekly_pulse} />
          </HorizonsSection>
        </div>

        <div className="lg:col-span-2">
          <HorizonsSection title="Active Goals" delay={350}>
            {realGoals.length > 0 ? (
              <div className="grid grid-cols-1 gap-4 md:grid-cols-2 md:gap-6">
                {realGoals.map((goal) => (
                  <GoalCard key={goal.id} goal={goal} />
                ))}
              </div>
            ) : (
              <p className="py-6 text-center text-sm text-ink-muted">
                No goals yet. Write about your goals in your journal, and your
                assistant will help you track them.
              </p>
            )}
          </HorizonsSection>
        </div>
      </div>

      {/* Suggestions from journal */}
      {data.pending_extractions.length > 0 ? (
        <div className="animate-reveal space-y-6" style={{ animationDelay: "500ms" }}>
          <div className="flex items-center gap-3">
            <span className="text-accent text-xl" aria-hidden="true">{"\u2728"}</span>
            <h2 className="font-headline text-2xl font-bold text-ink">
              Suggestions from your journal
            </h2>
          </div>
          <div className="grid grid-cols-1 gap-4 md:grid-cols-2 lg:grid-cols-3 md:gap-6">
            {data.pending_extractions.map((extraction) => (
              <PendingGoal key={extraction.id} extraction={extraction} />
            ))}
          </div>
        </div>
      ) : null}
    </div>
  );
}
