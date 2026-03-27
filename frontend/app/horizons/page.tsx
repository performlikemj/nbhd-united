"use client";

import { GoalCard } from "@/components/goal-card";
import { MomentumStrip } from "@/components/momentum-strip";
import { PendingGoal } from "@/components/pending-goal";
import { SectionCard } from "@/components/section-card";
import { SectionCardSkeleton } from "@/components/skeleton";
import { WeeklyPulse } from "@/components/weekly-pulse";
import { useHorizonsQuery } from "@/lib/queries";

export default function HorizonsPage() {
  const { data, isLoading, error } = useHorizonsQuery();

  if (isLoading) {
    return (
      <div className="space-y-6">
        <div>
          <h1 className="font-headline text-3xl font-bold text-ink sm:text-4xl">
            Horizons
          </h1>
          <p className="mt-1 text-sm text-ink-muted">
            Your goals, your momentum.
          </p>
        </div>
        <SectionCardSkeleton lines={2} />
        <SectionCardSkeleton lines={3} />
        <SectionCardSkeleton lines={4} />
      </div>
    );
  }

  if (error) {
    return (
      <div className="space-y-6">
        <div>
          <h1 className="font-headline text-3xl font-bold text-ink sm:text-4xl">
            Horizons
          </h1>
        </div>
        <div className="rounded-panel border border-rose-border bg-rose-bg p-4 text-sm text-rose-text">
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
    <div className="space-y-6 sm:space-y-8">
      {/* Header */}
      <div>
        <h1 className="font-headline text-3xl font-bold tracking-tight text-ink sm:text-4xl">
          Horizons
        </h1>
        <p className="mt-1 text-sm text-ink-muted">
          Your goals, your momentum.
        </p>
      </div>

      {/* Momentum — full width */}
      <SectionCard title="Momentum" subtitle="Last 30 days" delay={100}>
        <MomentumStrip days={data.momentum} streak={data.current_streak} />
      </SectionCard>

      {/* Weekly Pulse + Active Goals — side by side on large screens */}
      <div className="grid grid-cols-1 gap-6 lg:grid-cols-3">
        <div className="lg:col-span-1">
          <SectionCard title="Weekly Pulse" delay={200}>
            <WeeklyPulse weeks={data.weekly_pulse} />
          </SectionCard>
        </div>

        <div className="lg:col-span-2">
          <SectionCard title="Active Goals" delay={350}>
            {realGoals.length > 0 ? (
              <div className="grid grid-cols-1 gap-3 sm:gap-4 md:grid-cols-2">
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
          </SectionCard>
        </div>
      </div>

      {/* Suggestions from journal */}
      {data.pending_extractions.length > 0 ? (
        <div className="animate-reveal" style={{ animationDelay: "500ms" }}>
          <h2 className="font-headline text-xl font-bold text-ink">
            Suggestions from your journal
          </h2>
          <div className="mt-3 grid grid-cols-1 gap-3 sm:gap-4 md:grid-cols-2">
            {data.pending_extractions.map((extraction) => (
              <PendingGoal key={extraction.id} extraction={extraction} />
            ))}
          </div>
        </div>
      ) : null}
    </div>
  );
}
