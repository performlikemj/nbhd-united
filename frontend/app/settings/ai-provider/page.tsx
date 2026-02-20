"use client";

import { useEffect, useMemo, useState } from "react";

import { SectionCard } from "@/components/section-card";
import { SectionCardSkeleton } from "@/components/skeleton";
import { useLLMConfigQuery, useTenantQuery, useUpdateLLMConfigMutation } from "@/lib/queries";
import { fetchProviderModels } from "@/lib/api";
import type { LLMConfigUpdate, ProviderModel } from "@/lib/types";

const PROVIDERS = [
  { value: "openai", label: "OpenAI" },
  { value: "anthropic", label: "Anthropic" },
  { value: "groq", label: "Groq" },
  { value: "google", label: "Google Gemini" },
  { value: "openrouter", label: "OpenRouter" },
  { value: "xai", label: "xAI" },
] as const;

const TIER_MODEL_INFO: Record<string, string> = {
  starter: "Kimi K2.5 — 50 messages/day",
  premium: "Claude Sonnet 4.5 + Opus access — 200 messages/day",
  basic: "Claude Sonnet — included with your plan",
  plus: "Claude Opus — included with your plan",
};

function formatContextWindow(contextWindow?: number): string {
  if (!contextWindow) return "";
  if (contextWindow >= 1000) {
    return `${Math.round(contextWindow / 1000)}K`;
  }
  return `${contextWindow}`;
}

function formatFetchError(error: unknown): "Invalid API key" | "Could not reach provider" {
  const message = error instanceof Error ? error.message.toLowerCase() : "";
  if (message.includes("invalid") || message.includes("unauth") || message.includes("401") || message.includes("403")) {
    return "Invalid API key";
  }
  return "Could not reach provider";
}

export default function AIProviderPage() {
  const { data: tenant, isLoading: tenantLoading } = useTenantQuery();
  const { data: config, isLoading: configLoading } = useLLMConfigQuery();
  const updateMutation = useUpdateLLMConfigMutation();

  const [provider, setProvider] = useState("openai");
  const [apiKey, setApiKey] = useState("");
  const [modelId, setModelId] = useState("");
  const [saved, setSaved] = useState(false);
  const [models, setModels] = useState<ProviderModel[]>([]);
  const [isFetchingModels, setIsFetchingModels] = useState(false);
  const [fetchError, setFetchError] = useState("");
  const [isManualModelInput, setIsManualModelInput] = useState(true);

  const hasStoredKey = Boolean(config?.has_key);
  const canFetchModels = hasStoredKey || apiKey.trim().length > 0;

  const tier = tenant?.model_tier ?? "basic";
  const isByok = tier === "byok";

  useEffect(() => {
    if (config) {
      setProvider(config.provider || "openai");
      setModelId(config.model_id || "");
      setIsManualModelInput(true);
    }
  }, [config]);

  const handleSave = async () => {
    setSaved(false);
    const data: LLMConfigUpdate = { provider, model_id: modelId };
    if (apiKey) data.api_key = apiKey;
    await updateMutation.mutateAsync(data);
    setApiKey("");
    setSaved(true);
    setTimeout(() => setSaved(false), 3000);
  };

  const fetchModels = async () => {
    if (!provider || !canFetchModels || isFetchingModels) return;

    setIsFetchingModels(true);
    setFetchError("");
    try {
      const resolvedApiKey = apiKey.trim().length > 0 ? apiKey.trim() : undefined;
      const response = await fetchProviderModels(provider, resolvedApiKey);
      const resolvedModels = response?.models ?? [];
      setModels(resolvedModels);
      setIsManualModelInput(false);
      if (resolvedModels.length > 0 && !resolvedModels.some((model) => model.id === modelId)) {
        setModelId(resolvedModels[0].id);
      }
      if (resolvedModels.length === 0) {
        setIsManualModelInput(true);
      }
    } catch (error) {
      setFetchError(formatFetchError(error));
      setModels([]);
      setIsManualModelInput(true);
    } finally {
      setIsFetchingModels(false);
    }
  };

  const shouldShowSelect = !isManualModelInput && models.length > 0;
  const modelOptions = useMemo(
    () =>
      models.map((model) => ({
        value: model.id,
        label: model.context_window
          ? `${model.name} (${formatContextWindow(model.context_window)})`
          : model.name,
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
      <SectionCard
        title="AI Provider"
        subtitle={isByok ? "Configure your own LLM provider and API key" : "Your current AI model"}
      >
        {!isByok ? (
          <div className="space-y-4">
            <div className="rounded-panel border border-border bg-surface-elevated p-4">
              <dt className="font-mono text-[11px] uppercase tracking-[0.14em] text-ink-muted">Current Model</dt>
              <dd className="mt-1 text-sm text-ink">{TIER_MODEL_INFO[tier] ?? tier}</dd>
            </div>
            <div className="rounded-panel border-2 border-dashed border-accent/30 bg-accent/5 p-5">
              <p className="text-sm font-medium text-ink">Want to use your own model?</p>
              <p className="mt-1 text-sm text-ink-muted">
                Upgrade to the BYOK plan to bring your own API key and choose from OpenAI, Anthropic, Groq, Google Gemini, OpenRouter, or xAI.
              </p>
            </div>
          </div>
        ) : (
          <div className="space-y-5">
            {/* Provider */}
            <div>
              <label htmlFor="provider" className="block font-mono text-[11px] uppercase tracking-[0.14em] text-ink-muted">
                Provider
              </label>
              <select
                id="provider"
                value={provider}
                onChange={(e) => {
                  setProvider(e.target.value);
                  setModelId("");
                  setModels([]);
                  setIsManualModelInput(true);
                  setFetchError("");
                }}
                className="mt-1.5 w-full rounded-panel border border-border bg-surface px-3 py-2 text-sm text-ink focus:border-accent focus:outline-none focus:ring-1 focus:ring-accent"
              >
                {PROVIDERS.map((p) => (
                  <option key={p.value} value={p.value}>{p.label}</option>
                ))}
              </select>
            </div>

            {/* API Key */}
            <div>
              <label htmlFor="api-key" className="block font-mono text-[11px] uppercase tracking-[0.14em] text-ink-muted">
                API Key
              </label>
              <input
                id="api-key"
                type="password"
                value={apiKey}
                onChange={(e) => setApiKey(e.target.value)}
                placeholder={config?.has_key ? config.key_masked : "Enter your API key"}
                className="mt-1.5 w-full rounded-panel border border-border bg-surface px-3 py-2 text-sm text-ink placeholder:text-ink-faint focus:border-accent focus:outline-none focus:ring-1 focus:ring-accent"
              />
            </div>

            <div className="flex items-center gap-3">
              <button
                type="button"
                onClick={fetchModels}
                disabled={!canFetchModels || isFetchingModels}
                className="rounded-full border border-border-strong px-5 py-2.5 text-sm text-ink-faint transition hover:border-border-strong disabled:cursor-not-allowed disabled:opacity-55"
              >
                {isFetchingModels ? (
                  <span className="inline-flex items-center gap-2">
                    <span className="inline-block h-3.5 w-3.5 animate-spin rounded-full border-2 border-ink-faint border-t-transparent" />
                    Fetching...
                  </span>
                ) : (
                  "Fetch Models"
                )}
              </button>
            </div>

            {/* Model */}
            <div>
              <label htmlFor="model-id" className="block font-mono text-[11px] uppercase tracking-[0.14em] text-ink-muted">
                Model
              </label>

              {shouldShowSelect ? (
                <>
                  <select
                    id="model-id"
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
                    id="model-id"
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

            {/* Actions */}
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
        )}
      </SectionCard>
    </div>
  );
}
