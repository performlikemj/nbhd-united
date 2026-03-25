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
      <div className="space-y-4 sm:space-y-6">
        <div>
          <h1 className="font-display text-2xl text-ink sm:text-3xl">
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
      <div className="space-y-4 sm:space-y-6">
        <div>
          <h1 className="font-display text-2xl text-ink sm:text-3xl">
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

  return (
    <div className="space-y-4 sm:space-y-6">
      <div>
        <h1 className="font-display text-2xl text-ink sm:text-3xl">
          Horizons
        </h1>
        <p className="mt-1 text-sm text-ink-muted">
          Your goals, your momentum.
        </p>
      </div>

      <SectionCard title="Momentum" subtitle="Last 30 days" delay={100}>
        <MomentumStrip days={data.momentum} streak={data.current_streak} />
      </SectionCard>

      {(data.weekly_pulse.length > 0 || data.weekly_documents.length > 0) ? (
        <SectionCard title="Weekly Pulse" delay={200}>
          <WeeklyPulse weeks={data.weekly_pulse} weeklyDocuments={data.weekly_documents} />
        </SectionCard>
      ) : null}

      <SectionCard title="Active Goals" delay={350}>
        {(() => {
          const realGoals = data.goals.filter(
            (g) => !g.preview.includes("[Goal Name]") && !g.preview.includes("[Specific, measurable outcome]"),
          );
          return realGoals.length > 0 ? (
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
          );
        })()}
      </SectionCard>

      {data.pending_extractions.length > 0 ? (
        <div className="animate-reveal" style={{ animationDelay: "500ms" }}>
          <h2 className="font-display text-xl text-ink">
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
