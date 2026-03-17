"use client";

import { useEffect, useMemo, useState } from "react";

import { SectionCard } from "@/components/section-card";
import { SectionCardSkeleton } from "@/components/skeleton";
import { IntelligenceMeter } from "@/components/intelligence-meter";
import { useLLMConfigQuery, usePreferredModelMutation, useTaskModelPreferencesMutation, useTenantQuery, useUpdateLLMConfigMutation } from "@/lib/queries";
import { fetchProviderModels } from "@/lib/api";
import type { LLMConfigUpdate, ProviderModel } from "@/lib/types";

const SCHEDULED_TASKS = [
  { slug: "morning_briefing", label: "Morning Briefing" },
  { slug: "evening_checkin", label: "Evening Check-in" },
  { slug: "week_review", label: "Week Ahead Review" },
  { slug: "background_tasks", label: "Background Tasks" },
  { slug: "heartbeat", label: "Heartbeat Check-in" },
] as const;

const PROVIDERS = [
  { value: "openai", label: "OpenAI" },
  { value: "anthropic", label: "Anthropic" },
  { value: "groq", label: "Groq" },
  { value: "google", label: "Google Gemini" },
  { value: "openrouter", label: "OpenRouter" },
  { value: "xai", label: "xAI" },
] as const;

interface TierModel {
  model_id: string;
  name: string;
  tagline: string;
  intelligence: number;
  input_rate: number;
  output_rate: number;
}

const TIER_MODELS_UI: Record<string, TierModel[]> = {
  starter: [
    { model_id: "openrouter/minimax/minimax-m2.5", name: "MiniMax M2.5", tagline: "Fast and efficient", intelligence: 6, input_rate: 0.3, output_rate: 1.2 },
  ],
  premium: [
    { model_id: "openrouter/minimax/minimax-m2.5", name: "MiniMax M2.5", tagline: "Fastest and most affordable", intelligence: 6, input_rate: 0.3, output_rate: 1.2 },
    { model_id: "anthropic/claude-sonnet-4.6", name: "Claude Sonnet 4.6", tagline: "Great for everyday use", intelligence: 8, input_rate: 3, output_rate: 15 },
    { model_id: "anthropic/claude-opus-4.6", name: "Claude Opus 4.6", tagline: "Best for complex tasks", intelligence: 9, input_rate: 5, output_rate: 25 },
  ],
};

const TIER_DEFAULTS: Record<string, string> = {
  starter: "openrouter/minimax/minimax-m2.5",
  premium: "anthropic/claude-opus-4.6",
};

function formatContextWindow(contextWindow?: number): string {
  if (!contextWindow) return "";
  if (contextWindow >= 1000) return `${Math.round(contextWindow / 1000)}K`;
  return `${contextWindow}`;
}

export default function AIProviderPage() {
  const { data: tenant, isLoading: tenantLoading } = useTenantQuery();
  const { data: config, isLoading: configLoading } = useLLMConfigQuery();
  const updateMutation = useUpdateLLMConfigMutation();
  const preferredModelMutation = usePreferredModelMutation();
  const taskModelMutation = useTaskModelPreferencesMutation();

  const [provider, setProvider] = useState("openai");
  const [apiKey, setApiKey] = useState("");
  const [modelId, setModelId] = useState("");
  const [showKeyInput, setShowKeyInput] = useState(false);
  const [saved, setSaved] = useState(false);
  const [models, setModels] = useState<ProviderModel[]>([]);
  const [isFetchingModels, setIsFetchingModels] = useState(false);
  const [fetchError, setFetchError] = useState("");
  const [isManualModelInput, setIsManualModelInput] = useState(true);

  const hasStoredKey = Boolean(config?.has_key);
  const canFetchModels = hasStoredKey || apiKey.trim().length > 0;
  const tier = tenant?.model_tier ?? "starter";
  const isByok = tier === "byok";
  const tierModels = TIER_MODELS_UI[tier] ?? [];
  const activeModel = tenant?.preferred_model || TIER_DEFAULTS[tier] || "";

  useEffect(() => {
    if (config) {
      setProvider(config.provider || "openai");
      setModelId(config.model_id || "");
      setIsManualModelInput(true);
    }
  }, [config]);

  const handleSelectModel = async (modelId: string) => {
    await preferredModelMutation.mutateAsync(modelId);
  };

  const handleProviderChange = async (newProvider: string) => {
    setProvider(newProvider);
    setModelId("");
    setModels([]);
    setFetchError("");
    setIsManualModelInput(true);

    if (hasStoredKey) {
      setIsFetchingModels(true);
      try {
        const response = await fetchProviderModels(newProvider);
        const resolvedModels = response?.models ?? [];
        setModels(resolvedModels);
        if (resolvedModels.length > 0) {
          setIsManualModelInput(false);
          setModelId(resolvedModels[0].id);
        }
      } catch {
        // Silent — user can type manually
      } finally {
        setIsFetchingModels(false);
      }
    }
  };

  const handleFetchModels = async () => {
    if (!provider || !canFetchModels || isFetchingModels) return;
    setIsFetchingModels(true);
    setFetchError("");
    try {
      const resolvedApiKey = apiKey.trim().length > 0 ? apiKey.trim() : undefined;
      const response = await fetchProviderModels(provider, resolvedApiKey);
      const resolvedModels = response?.models ?? [];
      setModels(resolvedModels);
      setIsManualModelInput(resolvedModels.length === 0);
      if (resolvedModels.length > 0 && !resolvedModels.some((m) => m.id === modelId)) {
        setModelId(resolvedModels[0].id);
      }
    } catch (error) {
      const message = error instanceof Error ? error.message.toLowerCase() : "";
      setFetchError(
        message.includes("invalid") || message.includes("unauth") || message.includes("401")
          ? "Invalid API key"
          : "Could not reach provider"
      );
      setModels([]);
      setIsManualModelInput(true);
    } finally {
      setIsFetchingModels(false);
    }
  };

  const handleSave = async () => {
    setSaved(false);
    const data: LLMConfigUpdate = { provider, model_id: modelId };
    if (apiKey) data.api_key = apiKey;
    await updateMutation.mutateAsync(data);
    setApiKey("");
    setShowKeyInput(false);
    setSaved(true);
    setTimeout(() => setSaved(false), 3000);
  };

  const shouldShowSelect = !isManualModelInput && models.length > 0;
  const modelOptions = useMemo(
    () => models.map((model) => ({
      value: model.id,
      label: model.context_window ? `${model.name} (${formatContextWindow(model.context_window)})` : model.name,
    })),
    [models],
  );

  if (tenantLoading || configLoading) {
    return (
      <div className="space-y-4">
        <SectionCardSkeleton lines={5} />
      </div>
    );
  }

  return (
    <div className="space-y-4">
      {/* Model Picker — Starter & Premium */}
      {!isByok && (
        <SectionCard
          title="AI Provider"
          subtitle={tierModels.length > 1 ? "Choose your default model" : "Your current AI model"}
        >
          <div className="space-y-4">
            <div className={`grid gap-3 ${tierModels.length > 2 ? "sm:grid-cols-3" : tierModels.length > 1 ? "sm:grid-cols-2" : ""}`}>
              {tierModels.map((model) => {
                const isActive = activeModel === model.model_id;
                return (
                  <button
                    key={model.model_id}
                    type="button"
                    onClick={() => tierModels.length > 1 && handleSelectModel(model.model_id)}
                    disabled={preferredModelMutation.isPending || tierModels.length <= 1}
                    className={`rounded-panel border-2 p-4 text-left transition ${
                      isActive
                        ? "border-accent bg-accent/5"
                        : "border-border hover:border-accent/40"
                    } ${tierModels.length <= 1 ? "cursor-default" : ""} ${
                      preferredModelMutation.isPending ? "opacity-60" : ""
                    }`}
                  >
                    <div className="flex items-center justify-between gap-2">
                      <p className="font-medium text-ink">{model.name}</p>
                      {isActive && (
                        <span className="rounded-full bg-accent/10 px-2 py-0.5 text-xs font-medium text-accent">
                          Active
                        </span>
                      )}
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

            {tierModels.length > 1 && (
              <p className="text-xs text-ink-muted">
                Tap a card to switch your default model. A lighter model stretches your monthly budget further.
              </p>
            )}

            <div className="rounded-panel border-2 border-dashed border-accent/30 bg-accent/5 p-5">
              <p className="text-sm font-medium text-ink">Want to use your own model?</p>
              <p className="mt-1 text-sm text-ink-muted">
                Upgrade to the BYOK plan to bring your own API key and choose from OpenAI, Anthropic, Groq, Google Gemini, OpenRouter, or xAI.
              </p>
            </div>
          </div>
        </SectionCard>
      )}

      {/* Per-Task Model Selection — Premium only */}
      {tier === "premium" && (
        <SectionCard title="Scheduled Task Models" subtitle="Choose which model runs each background task">
          <div className="space-y-3">
            {SCHEDULED_TASKS.map((task) => {
              const currentPref = (tenant?.task_model_preferences as Record<string, string> | undefined)?.[task.slug] || "";
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
                    <option value="">Use default ({tierModels.find((m) => m.model_id === activeModel)?.name ?? "Opus"})</option>
                    {tierModels.map((m) => (
                      <option key={m.model_id} value={m.model_id}>{m.name}</option>
                    ))}
                  </select>
                </div>
              );
            })}
            <p className="text-xs text-ink-muted">
              Use a cheaper model for routine tasks to stretch your monthly budget. Changes apply within an hour.
            </p>
          </div>
        </SectionCard>
      )}

      {/* BYOK Configuration */}
      {isByok && (
        <SectionCard title="AI Provider" subtitle="Configure your own LLM provider and API key">
          <div className="space-y-5">
            {/* Provider pills */}
            <div>
              <p className="block font-mono text-[11px] uppercase tracking-[0.14em] text-ink-muted">Provider</p>
              <div className="mt-2 flex flex-wrap gap-2">
                {PROVIDERS.map((p) => (
                  <button
                    key={p.value}
                    type="button"
                    onClick={() => handleProviderChange(p.value)}
                    className={`rounded-full px-4 py-2 text-sm transition ${
                      provider === p.value
                        ? "bg-accent text-white"
                        : "border border-border text-ink-muted hover:border-accent/40"
                    }`}
                  >
                    {p.label}
                  </button>
                ))}
              </div>
            </div>

            {/* API Key — collapsed when stored */}
            <div>
              <p className="block font-mono text-[11px] uppercase tracking-[0.14em] text-ink-muted">API Key</p>
              {hasStoredKey && !showKeyInput ? (
                <div className="mt-1.5 flex items-center gap-3">
                  <span className="font-mono text-sm text-ink-muted">{config?.key_masked}</span>
                  <button
                    type="button"
                    onClick={() => setShowKeyInput(true)}
                    className="text-sm text-accent underline underline-offset-2"
                  >
                    Change
                  </button>
                </div>
              ) : (
                <div className="mt-1.5 flex gap-2">
                  <input
                    type="password"
                    value={apiKey}
                    onChange={(e) => setApiKey(e.target.value)}
                    placeholder={config?.has_key ? "Enter new API key" : "Enter your API key"}
                    className="flex-1 rounded-panel border border-border bg-surface px-3 py-2 text-sm text-ink placeholder:text-ink-faint focus:border-accent focus:outline-none focus:ring-1 focus:ring-accent"
                  />
                  {!hasStoredKey && (
                    <button
                      type="button"
                      onClick={handleFetchModels}
                      disabled={!canFetchModels || isFetchingModels}
                      className="rounded-full border border-border-strong px-4 py-2 text-sm text-ink-faint transition hover:border-accent disabled:cursor-not-allowed disabled:opacity-55"
                    >
                      {isFetchingModels ? "Fetching..." : "Fetch Models"}
                    </button>
                  )}
                </div>
              )}
            </div>

            {/* Model */}
            <div>
              <p className="block font-mono text-[11px] uppercase tracking-[0.14em] text-ink-muted">Model</p>
              {isFetchingModels ? (
                <p className="mt-1.5 text-sm text-ink-muted">Loading models...</p>
              ) : shouldShowSelect ? (
                <>
                  <select
                    value={modelId}
                    onChange={(e) => setModelId(e.target.value)}
                    className="mt-1.5 w-full rounded-panel border border-border bg-surface px-3 py-2 text-sm text-ink focus:border-accent focus:outline-none focus:ring-1 focus:ring-accent"
                  >
                    {modelOptions.map((model) => (
                      <option key={model.value} value={model.value}>{model.label}</option>
                    ))}
                  </select>
                  <button
                    type="button"
                    onClick={() => setIsManualModelInput(true)}
                    className="mt-2 text-xs text-ink-faint underline underline-offset-2"
                  >
                    or enter manually
                  </button>
                </>
              ) : (
                <>
                  <input
                    type="text"
                    value={modelId}
                    onChange={(e) => setModelId(e.target.value)}
                    placeholder="Enter model id"
                    className="mt-1.5 w-full rounded-panel border border-border bg-surface px-3 py-2 text-sm text-ink placeholder:text-ink-faint focus:border-accent focus:outline-none focus:ring-1 focus:ring-accent"
                  />
                  {models.length > 0 && (
                    <button
                      type="button"
                      onClick={() => setIsManualModelInput(false)}
                      className="mt-2 text-xs text-ink-faint underline underline-offset-2"
                    >
                      choose from fetched models
                    </button>
                  )}
                </>
              )}
            </div>

            {/* Save */}
            <div className="flex flex-wrap items-center gap-3">
              <button
                type="button"
                onClick={handleSave}
                disabled={updateMutation.isPending || !provider}
                className="rounded-full bg-accent px-5 py-2.5 text-sm font-medium text-white transition hover:bg-accent/85 disabled:cursor-not-allowed disabled:opacity-55"
              >
                {updateMutation.isPending ? "Saving..." : "Save"}
              </button>
            </div>

            {fetchError && (
              <p className="rounded-panel border border-rose-border bg-rose-bg px-3 py-2 text-sm text-rose-text">
                {fetchError}
              </p>
            )}
            {saved && (
              <p className="rounded-panel border border-signal/30 bg-signal-faint px-3 py-2 text-sm text-signal">
                Configuration saved successfully.
              </p>
            )}
            {updateMutation.isError && (
              <p className="rounded-panel border border-rose-border bg-rose-bg px-3 py-2 text-sm text-rose-text">
                Failed to save configuration. Please try again.
              </p>
            )}

            <p className="text-xs text-ink-faint">
              Your API key is encrypted and stored securely. We never share it.
            </p>
          </div>
        </SectionCard>
      )}
    </div>
  );
}
