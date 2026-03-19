"use client";

import { useMemo, useState } from "react";

import { SectionCard } from "@/components/section-card";
import { SectionCardSkeleton } from "@/components/skeleton";
import { IntelligenceMeter } from "@/components/intelligence-meter";
import { useDeleteLLMConfigMutation, useLLMConfigQuery, usePreferredModelMutation, useTaskModelPreferencesMutation, useTenantQuery, useUpdateLLMConfigMutation } from "@/lib/queries";
import { fetchProviderModels } from "@/lib/api";
import type { LLMConfig, LLMConfigUpdate, ProviderModel } from "@/lib/types";

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
    { model_id: "openrouter/minimax/minimax-m2.7", name: "MiniMax M2.7", tagline: "Fast and efficient", intelligence: 6, input_rate: 0.3, output_rate: 1.2 },
  ],
  premium: [
    { model_id: "openrouter/minimax/minimax-m2.7", name: "MiniMax M2.7", tagline: "Fastest and most affordable", intelligence: 6, input_rate: 0.3, output_rate: 1.2 },
    { model_id: "anthropic/claude-sonnet-4.6", name: "Claude Sonnet 4.6", tagline: "Great for everyday use", intelligence: 8, input_rate: 3, output_rate: 15 },
    { model_id: "anthropic/claude-opus-4.6", name: "Claude Opus 4.6", tagline: "Best for complex tasks", intelligence: 9, input_rate: 5, output_rate: 25 },
  ],
};

const TIER_DEFAULTS: Record<string, string> = {
  starter: "openrouter/minimax/minimax-m2.7",
  premium: "anthropic/claude-opus-4.6",
};

function formatContextWindow(contextWindow?: number): string {
  if (!contextWindow) return "";
  if (contextWindow >= 1000) return `${Math.round(contextWindow / 1000)}K`;
  return `${contextWindow}`;
}

function getProviderLabel(value: string): string {
  return PROVIDERS.find((p) => p.value === value)?.label ?? value;
}

export default function AIProviderPage() {
  const { data: tenant, isLoading: tenantLoading } = useTenantQuery();
  const { data: configs, isLoading: configLoading } = useLLMConfigQuery();
  const updateMutation = useUpdateLLMConfigMutation();
  const deleteMutation = useDeleteLLMConfigMutation();
  const preferredModelMutation = usePreferredModelMutation();
  const taskModelMutation = useTaskModelPreferencesMutation();

  const [provider, setProvider] = useState("openai");
  const [apiKey, setApiKey] = useState("");
  const [modelId, setModelId] = useState("");
  const [saved, setSaved] = useState(false);
  const [models, setModels] = useState<ProviderModel[]>([]);
  const [isFetchingModels, setIsFetchingModels] = useState(false);
  const [fetchError, setFetchError] = useState("");
  const [isManualModelInput, setIsManualModelInput] = useState(true);
  const [editingProvider, setEditingProvider] = useState<string | null>(null);

  const configList: LLMConfig[] = useMemo(() => configs ?? [], [configs]);
  const tier = tenant?.model_tier ?? "starter";
  const isByok = tier === "byok";
  const tierModels = TIER_MODELS_UI[tier] ?? [];
  const activeModel = tenant?.preferred_model || TIER_DEFAULTS[tier] || "";

  // For BYOK: build model list from configured providers
  const byokModels = useMemo(() =>
    configList.filter((c) => c.model_id).map((c) => ({
      model_id: c.model_id,
      name: `${c.model_id} (${getProviderLabel(c.provider)})`,
    })),
    [configList],
  );

  // Combined model list for per-task selection
  const taskModelOptions = isByok ? byokModels : tierModels.map((m) => ({ model_id: m.model_id, name: m.name }));

  const handleSelectModel = async (id: string) => {
    await preferredModelMutation.mutateAsync(id);
  };

  const handleFetchModels = async (targetProvider?: string) => {
    const p = targetProvider ?? provider;
    if (!p || isFetchingModels) return;
    setIsFetchingModels(true);
    setFetchError("");
    try {
      const resolvedApiKey = apiKey.trim().length > 0 ? apiKey.trim() : undefined;
      const response = await fetchProviderModels(p, resolvedApiKey);
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
    setEditingProvider(null);
    setSaved(true);
    setTimeout(() => setSaved(false), 3000);
  };

  const handleDeleteProvider = async (p: string) => {
    await deleteMutation.mutateAsync(p);
  };

  const startEditingProvider = (p: string) => {
    const existing = configList.find((c) => c.provider === p);
    setProvider(p);
    setModelId(existing?.model_id ?? "");
    setApiKey("");
    setModels([]);
    setFetchError("");
    setIsManualModelInput(true);
    setEditingProvider(p);
  };

  const startAddingProvider = () => {
    const usedProviders = new Set(configList.map((c) => c.provider));
    const available = PROVIDERS.find((p) => !usedProviders.has(p.value));
    setProvider(available?.value ?? "openai");
    setModelId("");
    setApiKey("");
    setModels([]);
    setFetchError("");
    setIsManualModelInput(true);
    setEditingProvider("__new__");
  };

  const shouldShowSelect = !isManualModelInput && models.length > 0;
  const modelOptions = useMemo(
    () => models.map((model) => ({
      value: model.id,
      label: model.context_window ? `${model.name} (${formatContextWindow(model.context_window)})` : model.name,
    })),
    [models],
  );

  // Providers already configured
  const usedProviders = new Set(configList.map((c) => c.provider));

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

      {/* Per-Task Model Selection — Premium & BYOK */}
      {(tier === "premium" || (isByok && taskModelOptions.length > 0)) && (
        <SectionCard title="Scheduled Task Models" subtitle="Choose which model runs each background task">
          <div className="space-y-3">
            {SCHEDULED_TASKS.map((task) => {
              const currentPref = (tenant?.task_model_preferences as Record<string, string> | undefined)?.[task.slug] || "";
              const defaultName = isByok
                ? (byokModels.find((m) => m.model_id === activeModel)?.name ?? "default")
                : (tierModels.find((m) => m.model_id === activeModel)?.name ?? "default");
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
                    {taskModelOptions.map((m) => (
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
      )}

      {/* BYOK Configuration — Multi-provider */}
      {isByok && (
        <SectionCard title="API Keys" subtitle="Add keys for one or more providers">
          <div className="space-y-4">
            {/* Configured providers list */}
            {configList.length > 0 && (
              <div className="space-y-2">
                {configList.map((c) => (
                  <div key={c.provider} className="flex items-center justify-between gap-3 rounded-panel border border-border p-3">
                    <div className="min-w-0">
                      <p className="text-sm font-medium text-ink">{getProviderLabel(c.provider)}</p>
                      <p className="mt-0.5 truncate font-mono text-xs text-ink-muted">
                        {c.model_id || "No model set"} · {c.has_key ? c.key_masked : "No key"}
                      </p>
                    </div>
                    <div className="flex shrink-0 gap-2">
                      <button
                        type="button"
                        onClick={() => startEditingProvider(c.provider)}
                        className="text-xs text-accent underline underline-offset-2"
                      >
                        Edit
                      </button>
                      <button
                        type="button"
                        onClick={() => handleDeleteProvider(c.provider)}
                        disabled={deleteMutation.isPending}
                        className="text-xs text-rose-text underline underline-offset-2 disabled:opacity-50"
                      >
                        Remove
                      </button>
                    </div>
                  </div>
                ))}
              </div>
            )}

            {/* Add/Edit form */}
            {editingProvider !== null ? (
              <div className="space-y-4 rounded-panel border border-accent/25 bg-accent/5 p-4">
                <div>
                  <p className="block font-mono text-[11px] uppercase tracking-[0.14em] text-ink-muted">Provider</p>
                  <div className="mt-2 flex flex-wrap gap-2">
                    {PROVIDERS.map((p) => {
                      const isUsed = usedProviders.has(p.value) && p.value !== editingProvider;
                      return (
                        <button
                          key={p.value}
                          type="button"
                          onClick={() => !isUsed && setProvider(p.value)}
                          disabled={isUsed}
                          className={`rounded-full px-4 py-2 text-sm transition ${
                            provider === p.value
                              ? "bg-accent text-white"
                              : isUsed
                                ? "border border-border text-ink-faint opacity-40 cursor-not-allowed"
                                : "border border-border text-ink-muted hover:border-accent/40"
                          }`}
                        >
                          {p.label}
                        </button>
                      );
                    })}
                  </div>
                </div>

                <div>
                  <p className="block font-mono text-[11px] uppercase tracking-[0.14em] text-ink-muted">API Key</p>
                  <div className="mt-1.5 flex gap-2">
                    <input
                      type="password"
                      value={apiKey}
                      onChange={(e) => setApiKey(e.target.value)}
                      placeholder="Enter API key"
                      className="flex-1 rounded-panel border border-border bg-surface px-3 py-2 text-sm text-ink placeholder:text-ink-faint focus:border-accent focus:outline-none focus:ring-1 focus:ring-accent"
                    />
                    <button
                      type="button"
                      onClick={() => handleFetchModels()}
                      disabled={(!apiKey.trim() && !usedProviders.has(provider)) || isFetchingModels}
                      className="rounded-full border border-border-strong px-4 py-2 text-sm text-ink-faint transition hover:border-accent disabled:cursor-not-allowed disabled:opacity-55"
                    >
                      {isFetchingModels ? "Fetching..." : "Fetch Models"}
                    </button>
                  </div>
                </div>

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

                <div className="flex flex-wrap items-center gap-3">
                  <button
                    type="button"
                    onClick={handleSave}
                    disabled={updateMutation.isPending || !provider}
                    className="rounded-full bg-accent px-5 py-2.5 text-sm font-medium text-white transition hover:bg-accent/85 disabled:cursor-not-allowed disabled:opacity-55"
                  >
                    {updateMutation.isPending ? "Saving..." : "Save"}
                  </button>
                  <button
                    type="button"
                    onClick={() => setEditingProvider(null)}
                    className="text-sm text-ink-muted underline underline-offset-2"
                  >
                    Cancel
                  </button>
                </div>

                {fetchError && (
                  <p className="rounded-panel border border-rose-border bg-rose-bg px-3 py-2 text-sm text-rose-text">
                    {fetchError}
                  </p>
                )}
              </div>
            ) : (
              <button
                type="button"
                onClick={startAddingProvider}
                disabled={usedProviders.size >= PROVIDERS.length}
                className="w-full rounded-panel border-2 border-dashed border-accent/30 bg-accent/5 p-4 text-sm font-medium text-accent transition hover:bg-accent/10 disabled:cursor-not-allowed disabled:opacity-50"
              >
                + Add provider
              </button>
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
              Your API keys are encrypted and stored securely. We never share them.
            </p>
          </div>
        </SectionCard>
      )}
    </div>
  );
}
