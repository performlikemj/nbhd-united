"use client";

import clsx from "clsx";
import { useMemo, useState } from "react";

import { BYOProviderCard } from "@/components/byo/byo-provider-card";
import { ConnectAnthropicModal } from "@/components/byo/connect-anthropic-modal";
import { DisconnectModal } from "@/components/byo/disconnect-modal";
import { IntelligenceMeter } from "@/components/intelligence-meter";
import { SectionCard } from "@/components/section-card";
import { SectionCardSkeleton } from "@/components/skeleton";
import { ACTIVE_MODELS, DEFAULT_MODEL, MODELS, type ModelUI } from "@/lib/models";
import {
  useByoCredentialsQuery,
  usePreferredModelMutation,
  useTaskModelPreferencesMutation,
  useTenantQuery,
} from "@/lib/queries";
import type { BYOCredential } from "@/lib/types";

const SCHEDULED_TASKS = [
  { slug: "morning_briefing", label: "Morning Briefing" },
  { slug: "evening_checkin", label: "Evening Check-in" },
  { slug: "week_review", label: "Week Ahead Review" },
  { slug: "background_tasks", label: "Background Tasks" },
  { slug: "heartbeat", label: "Heartbeat Check-in" },
] as const;

type OpenModal = null | "connect-anthropic" | "disconnect-anthropic";

function findCred(
  creds: BYOCredential[] | undefined,
  provider: "anthropic" | "openai",
): BYOCredential | undefined {
  return creds?.find((c) => c.provider === provider);
}

function isModelAvailable(
  model: ModelUI,
  anthropicCred: BYOCredential | undefined,
): boolean {
  if (!model.requires) return true;
  if (model.requires === "byo-anthropic") {
    return Boolean(
      anthropicCred && (anthropicCred.status === "verified" || anthropicCred.status === "pending"),
    );
  }
  // future: byo-openai
  return false;
}

export default function AIProviderPage() {
  const { data: tenant, isLoading: tenantLoading } = useTenantQuery();
  const byoEnabled = Boolean(tenant?.byo_models_enabled);
  const { data: byoCreds } = useByoCredentialsQuery();
  const preferredModelMutation = usePreferredModelMutation();
  const taskModelMutation = useTaskModelPreferencesMutation();

  const [openModal, setOpenModal] = useState<OpenModal>(null);

  const anthropicCred = useMemo(() => findCred(byoCreds, "anthropic"), [byoCreds]);
  const openaiCred = useMemo(() => findCred(byoCreds, "openai"), [byoCreds]);

  const activeModel = tenant?.preferred_model || DEFAULT_MODEL;
  const fallbackModelName = useMemo(() => {
    const m = ACTIVE_MODELS.find((x) => x.model_id === DEFAULT_MODEL);
    return m?.name ?? "MiniMax M2.7";
  }, []);

  const handleSelectModel = async (model: ModelUI) => {
    if (model.comingSoon || preferredModelMutation.isPending) return;
    if (!isModelAvailable(model, anthropicCred)) {
      // Model gated on BYO Anthropic — open the connect modal instead.
      if (model.requires === "byo-anthropic") setOpenModal("connect-anthropic");
      return;
    }
    await preferredModelMutation.mutateAsync(model.model_id);
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
      {byoEnabled ? (
        <SectionCard
          title="Use your own subscription"
          subtitle="Connect your Pro/Max account and we'll route inference through your subscription instead of charging tokens to your platform plan."
        >
          <div className="grid gap-3 sm:grid-cols-2 sm:gap-4">
            <BYOProviderCard
              provider="anthropic"
              cred={anthropicCred}
              onConnect={() => setOpenModal("connect-anthropic")}
              onDisconnect={() => setOpenModal("disconnect-anthropic")}
            />
            <BYOProviderCard
              provider="openai"
              cred={openaiCred}
              onConnect={() => {
                /* OpenAI not yet supported */
              }}
              onDisconnect={() => {
                /* unreachable */
              }}
              disabled
            />
          </div>
        </SectionCard>
      ) : null}

      <SectionCard title="AI Provider" subtitle="Choose your default model">
        <div className="space-y-4">
          <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
            {MODELS.map((model) => {
              const available = isModelAvailable(model, anthropicCred);
              const isActive = !model.comingSoon && available && activeModel === model.model_id;
              const requiresLabel =
                model.requires === "byo-anthropic" ? "Requires Anthropic connect" : "";

              return (
                <button
                  key={model.model_id}
                  type="button"
                  onClick={() => void handleSelectModel(model)}
                  disabled={model.comingSoon || preferredModelMutation.isPending}
                  className={clsx(
                    "rounded-panel border-2 p-4 text-left transition",
                    model.comingSoon
                      ? "border-border bg-surface-elevated opacity-55 cursor-not-allowed"
                      : !available
                        ? "border-dashed border-border bg-surface-elevated/60 hover:border-accent/40"
                        : isActive
                          ? "border-accent bg-accent/5"
                          : "border-border hover:border-accent/40",
                    !model.comingSoon && preferredModelMutation.isPending && "opacity-60",
                  )}
                  aria-label={
                    !available ? `${model.name} — connect Anthropic to enable` : model.name
                  }
                >
                  <div className="flex items-center justify-between gap-2">
                    <p className="font-medium text-ink">{model.name}</p>
                    {model.comingSoon ? (
                      <span className="rounded-full bg-amber-bg border border-amber-border px-2 py-0.5 text-xs font-medium text-amber-text">
                        Coming Soon
                      </span>
                    ) : !available ? (
                      <span className="rounded-full border border-border bg-surface px-2 py-0.5 text-xs font-medium text-ink-faint">
                        Locked
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
                  {model.requires === "byo-anthropic" && !available ? (
                    <p className="mt-1 text-xs text-accent">Connect Anthropic to enable →</p>
                  ) : (
                    <p className="mt-1 font-mono text-xs text-ink-muted">
                      {model.input_rate === 0 && model.output_rate === 0
                        ? "Pay your provider directly"
                        : `$${model.input_rate}/1M in · $${model.output_rate}/1M out`}
                    </p>
                  )}
                  {!available && requiresLabel ? (
                    <p className="sr-only">{requiresLabel}</p>
                  ) : null}
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
            const currentPref =
              (tenant?.task_model_preferences as Record<string, string> | undefined)?.[task.slug] || "";
            const defaultName =
              ACTIVE_MODELS.find((m) => m.model_id === activeModel)?.name ?? "default";
            return (
              <div
                key={task.slug}
                className="flex flex-col gap-2 rounded-panel border border-border p-3 sm:flex-row sm:items-center sm:justify-between"
              >
                <p className="text-sm font-medium text-ink">{task.label}</p>
                <select
                  value={currentPref}
                  onChange={(e) => {
                    void taskModelMutation.mutateAsync({ [task.slug]: e.target.value });
                  }}
                  disabled={taskModelMutation.isPending}
                  className="rounded-panel border border-border bg-surface px-3 py-1.5 text-sm text-ink focus:border-accent focus:outline-none focus:ring-1 focus:ring-accent min-h-[36px]"
                >
                  <option value="">Use default ({defaultName})</option>
                  {ACTIVE_MODELS.filter((m) => isModelAvailable(m, anthropicCred)).map((m) => (
                    <option key={m.model_id} value={m.model_id}>
                      {m.name}
                    </option>
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

      <ConnectAnthropicModal
        open={openModal === "connect-anthropic"}
        onClose={() => setOpenModal(null)}
      />
      <DisconnectModal
        open={openModal === "disconnect-anthropic"}
        cred={anthropicCred}
        fallbackModelName={fallbackModelName}
        onClose={() => setOpenModal(null)}
      />
    </div>
  );
}
