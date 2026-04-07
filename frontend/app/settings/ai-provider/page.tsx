"use client";

import { SectionCard } from "@/components/section-card";
import { SectionCardSkeleton } from "@/components/skeleton";
import { IntelligenceMeter } from "@/components/intelligence-meter";
import { usePreferredModelMutation, useTaskModelPreferencesMutation, useTenantQuery } from "@/lib/queries";

const SCHEDULED_TASKS = [
  { slug: "morning_briefing", label: "Morning Briefing" },
  { slug: "evening_checkin", label: "Evening Check-in" },
  { slug: "week_review", label: "Week Ahead Review" },
  { slug: "background_tasks", label: "Background Tasks" },
  { slug: "heartbeat", label: "Heartbeat Check-in" },
] as const;

interface ModelUI {
  model_id: string;
  name: string;
  tagline: string;
  intelligence: number;
  input_rate: number;
  output_rate: number;
  comingSoon?: boolean;
}

const MODELS: ModelUI[] = [
  { model_id: "openrouter/minimax/minimax-m2.7", name: "MiniMax M2.7", tagline: "Fast and efficient", intelligence: 6, input_rate: 0.3, output_rate: 1.2 },
  { model_id: "openrouter/moonshotai/kimi-k2.5", name: "Kimi 2.5", tagline: "Balanced capability and cost", intelligence: 7, input_rate: 0.38, output_rate: 1.72 },
  { model_id: "openrouter/google/gemma-4-31b-it", name: "Gemma 4 31B", tagline: "Lightweight and affordable", intelligence: 6, input_rate: 0.14, output_rate: 0.40, comingSoon: true },
];

const ACTIVE_MODELS = MODELS.filter((m) => !m.comingSoon);

const DEFAULT_MODEL = "openrouter/minimax/minimax-m2.7";

export default function AIProviderPage() {
  const { data: tenant, isLoading: tenantLoading } = useTenantQuery();
  const preferredModelMutation = usePreferredModelMutation();
  const taskModelMutation = useTaskModelPreferencesMutation();

  const activeModel = tenant?.preferred_model || DEFAULT_MODEL;

  const handleSelectModel = async (id: string) => {
    await preferredModelMutation.mutateAsync(id);
  };

  if (tenantLoading) {
    return (
      <div className="space-y-4">
        <SectionCardSkeleton lines={5} />
      </div>
    );
  }

  return (
    <div className="space-y-4">
      <SectionCard
        title="AI Provider"
        subtitle="Choose your default model"
      >
        <div className="space-y-4">
          <div className="grid gap-3 sm:grid-cols-3">
            {MODELS.map((model) => {
              const isActive = !model.comingSoon && activeModel === model.model_id;
              return (
                <button
                  key={model.model_id}
                  type="button"
                  onClick={() => !model.comingSoon && handleSelectModel(model.model_id)}
                  disabled={model.comingSoon || preferredModelMutation.isPending}
                  className={`rounded-panel border-2 p-4 text-left transition ${
                    model.comingSoon
                      ? "border-border bg-surface-elevated opacity-55 cursor-not-allowed"
                      : isActive
                        ? "border-accent bg-accent/5"
                        : "border-border hover:border-accent/40"
                  } ${!model.comingSoon && preferredModelMutation.isPending ? "opacity-60" : ""}`}
                >
                  <div className="flex items-center justify-between gap-2">
                    <p className="font-medium text-ink">{model.name}</p>
                    {model.comingSoon ? (
                      <span className="rounded-full bg-amber-bg border border-amber-border px-2 py-0.5 text-xs font-medium text-amber-text">
                        Coming Soon
                      </span>
                    ) : isActive ? (
                      <span className="rounded-full bg-accent/10 px-2 py-0.5 text-xs font-medium text-accent">
                        Active
                      </span>
                    ) : null}
                  </div>
                  <div className="mt-2">
                    <IntelligenceMeter level={model.intelligence} compact />
                  </div>
                  <p className="mt-2 text-xs text-ink-muted">{model.tagline}</p>
                  <p className="mt-1 font-mono text-xs text-ink-muted">
                    ${model.input_rate}/1M in · ${model.output_rate}/1M out
                  </p>
                </button>
              );
            })}
          </div>

          <p className="text-xs text-ink-muted">
            Tap a card to switch your default model. A lighter model stretches your monthly budget further.
          </p>
        </div>
      </SectionCard>

      {/* Per-Task Model Selection */}
      <SectionCard title="Scheduled Task Models" subtitle="Choose which model runs each background task">
        <div className="space-y-3">
          {SCHEDULED_TASKS.map((task) => {
            const currentPref = (tenant?.task_model_preferences as Record<string, string> | undefined)?.[task.slug] || "";
            const defaultName = ACTIVE_MODELS.find((m) => m.model_id === activeModel)?.name ?? "default";
            return (
              <div key={task.slug} className="flex items-center justify-between gap-3 rounded-panel border border-border p-3">
                <p className="text-sm font-medium text-ink">{task.label}</p>
                <select
                  value={currentPref}
                  onChange={(e) => {
                    void taskModelMutation.mutateAsync({ [task.slug]: e.target.value });
                  }}
                  disabled={taskModelMutation.isPending}
                  className="rounded-panel border border-border bg-surface px-3 py-1.5 text-sm text-ink focus:border-accent focus:outline-none focus:ring-1 focus:ring-accent"
                >
                  <option value="">Use default ({defaultName})</option>
                  {ACTIVE_MODELS.map((m) => (
                    <option key={m.model_id} value={m.model_id}>{m.name}</option>
                  ))}
                </select>
              </div>
            );
          })}
          <p className="text-xs text-ink-muted">
            Use a lighter model for routine tasks to stretch your budget. Changes apply within an hour.
          </p>
        </div>
      </SectionCard>
    </div>
  );
}
