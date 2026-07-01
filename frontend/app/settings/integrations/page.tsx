"use client";

import { useSearchParams } from "next/navigation";
import { Suspense, useState } from "react";

import { PendingConfigChip } from "@/components/pending-config-chip";
import { SectionCard } from "@/components/section-card";
import { SectionCardSkeleton } from "@/components/skeleton";
import { StatusPill } from "@/components/status-pill";
import {
  useDisconnectIntegrationMutation,
  useIntegrationsQuery,
  useOAuthAuthorizeMutation,
  useTenantQuery,
  useUpdateFinanceSettingsMutation,
  useUpdateFuelSettingsMutation,
  useUpdateCoreSettingsMutation,
  useFuelProfileQuery,
} from "@/lib/queries";
import { ServiceIcon } from "@/components/service-icon";
import { AppStoreBadge } from "@/components/app-store-badge";
import { ErrorBoundary } from "@/components/error-boundary";

const providers: { key: string; label: string; description?: string }[] = [
  {
    key: "google",
    label: "Google",
    description: "Gmail, Calendar, Drive & Tasks",
  },
  {
    key: "reddit",
    label: "Reddit",
    description: "Browse your feeds and subreddits without the doom-scroll.",
  },
];

function AppCard() {
  return (
    <article className="rounded-panel border border-border bg-surface-elevated p-4">
      <div className="flex items-center gap-2">
        <span className="text-base" aria-hidden="true">◇</span>
        <h3 className="text-base font-medium">NBHD for iPhone</h3>
      </div>
      <p className="mt-2 text-sm text-ink-muted">
        Talk to your assistant on the go — voice notes, photos, and daily
        check-ins, right from your phone.
      </p>
      <div className="mt-4">
        <AppStoreBadge height={44} />
      </div>
    </article>
  );
}

function GravityCard() {
  const { data: tenant } = useTenantQuery();
  const mutation = useUpdateFinanceSettingsMutation();
  const enabled = tenant?.finance_enabled ?? false;
  const [restarting, setRestarting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Gravity is paused platform-wide for privacy — don't offer the toggle at
  // all while unavailable. (Hooks above run unconditionally; guard goes here.)
  if (tenant && tenant.gravity_available === false) {
    return null;
  }

  const handleToggle = async () => {
    setError(null);
    try {
      const result = await mutation.mutateAsync({ finance_enabled: !enabled });
      if (result.restart_required) {
        // The plugin allow-list flipped — the running session won't see the
        // change until the container restarts. Restart immediately rather
        // than asking again; the user already chose to flip the toggle.
        setRestarting(true);
        try {
          const { restartFinanceAssistant } = await import("@/lib/api");
          await restartFinanceAssistant();
        } catch {
          setError(
            "Saved, but couldn't restart your assistant. Toggle off and back on to retry.",
          );
        } finally {
          setRestarting(false);
        }
      }
    } catch {
      setError("Couldn't update Gravity. Please try again.");
    }
  };

  const busy = mutation.isPending || restarting;

  return (
    <article
      className={`rounded-panel border p-4 transition-colors ${
        enabled ? "border-accent/25 bg-accent/5" : "border-border bg-surface-elevated"
      }`}
    >
      <div className="flex items-center justify-between gap-3">
        <div>
          <div className="flex items-center gap-2">
            <span className="text-base" aria-hidden="true">◆</span>
            <h3 className="text-base font-medium">Gravity</h3>
          </div>
          <p className="mt-1 text-sm text-ink-muted">
            Budget tracking, debt payoff strategies, and financial progress
            — powered by your AI assistant.
          </p>
          {restarting && (
            <p className="mt-2 text-xs text-amber-text" role="status">
              Configuring your assistant... this takes about a minute.
            </p>
          )}
          {error && (
            <p className="mt-2 text-xs text-rose-text" role="alert">
              {error}
            </p>
          )}
        </div>
        <button
          type="button"
          role="switch"
          aria-checked={enabled}
          aria-label={enabled ? "Disable Gravity" : "Enable Gravity"}
          onClick={handleToggle}
          disabled={busy}
          className={`relative inline-flex h-6 w-11 shrink-0 cursor-pointer rounded-full border-2 border-transparent transition-colors ${
            enabled ? "bg-accent" : "bg-border"
          } ${busy ? "opacity-50" : ""}`}
        >
          <span
            className={`pointer-events-none inline-block h-5 w-5 transform rounded-full bg-white shadow transition-transform ${
              enabled ? "translate-x-5" : "translate-x-0"
            }`}
          />
        </button>
      </div>
    </article>
  );
}

function FuelProfileStatus() {
  const { data: profile } = useFuelProfileQuery();
  if (!profile) return null;

  const statusText: Record<string, string> = {
    pending: "Your assistant will guide you through profile setup next time you chat.",
    in_progress: "Profile setup in progress \u2014 continue chatting with your assistant to complete it.",
    completed: `${profile.fitness_level ? profile.fitness_level.charAt(0).toUpperCase() + profile.fitness_level.slice(1) : "Profile set up"} \u00b7 ${profile.goals.length} goal${profile.goals.length !== 1 ? "s" : ""} \u00b7 ${profile.days_per_week ?? "?"} days/wk`,
    declined: "Using general workouts \u2014 chat with your assistant to set up a profile anytime.",
  };

  return (
    <p className="mt-2 text-xs text-ink-muted">
      {statusText[profile.onboarding_status] ?? statusText.pending}
    </p>
  );
}

function FuelCard() {
  const { data: tenant } = useTenantQuery();
  const mutation = useUpdateFuelSettingsMutation();
  const enabled = tenant?.fuel_enabled ?? false;
  const [restarting, setRestarting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const handleToggle = async () => {
    setError(null);
    try {
      const result = await mutation.mutateAsync({ fuel_enabled: !enabled });
      if (result.restart_required) {
        // Plugin allow-list flipped — restart immediately so the running
        // session sees the new state. Disable also flips the allow-list
        // (plugin must be unloaded) so the same path applies.
        setRestarting(true);
        try {
          const { restartFuelAssistant } = await import("@/lib/api");
          await restartFuelAssistant();
        } catch {
          setError(
            "Saved, but couldn't restart your assistant. Toggle off and back on to retry.",
          );
        } finally {
          setRestarting(false);
        }
      }
    } catch {
      setError("Couldn't update Fuel. Please try again.");
    }
  };

  const busy = mutation.isPending || restarting;

  return (
    <article
      className={`rounded-panel border p-4 transition-colors ${
        enabled ? "border-accent/25 bg-accent/5" : "border-border bg-surface-elevated"
      }`}
    >
      <div className="flex items-center justify-between gap-3">
        <div>
          <div className="flex items-center gap-2">
            <span className="text-base" aria-hidden="true">▲</span>
            <h3 className="text-base font-medium">Fuel</h3>
          </div>
          <p className="mt-1 text-sm text-ink-muted">
            Workout tracking, fitness logging, and progress trends
            — powered by your AI assistant.
          </p>
          {enabled && !restarting && !error && <FuelProfileStatus />}
          {restarting && (
            <p className="mt-2 text-xs text-amber-text" role="status">
              Configuring your assistant... this takes about a minute.
            </p>
          )}
          {error && (
            <p className="mt-2 text-xs text-rose-text" role="alert">
              {error}
            </p>
          )}
        </div>
        <button
          type="button"
          role="switch"
          aria-checked={enabled}
          aria-label={enabled ? "Disable Fuel" : "Enable Fuel"}
          onClick={handleToggle}
          disabled={busy}
          className={`relative inline-flex h-6 w-11 shrink-0 cursor-pointer rounded-full border-2 border-transparent transition-colors ${
            enabled ? "bg-accent" : "bg-border"
          } ${busy ? "opacity-50" : ""}`}
        >
          <span
            className={`pointer-events-none inline-block h-5 w-5 transform rounded-full bg-white shadow transition-transform ${
              enabled ? "translate-x-5" : "translate-x-0"
            }`}
          />
        </button>
      </div>
    </article>
  );
}

function CoreCard() {
  const { data: tenant } = useTenantQuery();
  const mutation = useUpdateCoreSettingsMutation();
  const enabled = tenant?.core_enabled ?? false;
  const [restarting, setRestarting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const handleToggle = async () => {
    setError(null);
    try {
      const result = await mutation.mutateAsync({ core_enabled: !enabled });
      if (result.restart_required) {
        // Plugin allow-list flipped — restart so the running session picks up
        // the change (same path on enable and disable).
        setRestarting(true);
        try {
          const { restartCoreAssistant } = await import("@/lib/api");
          await restartCoreAssistant();
        } catch {
          setError(
            "Saved, but couldn't restart your assistant. Toggle off and back on to retry.",
          );
        } finally {
          setRestarting(false);
        }
      }
    } catch {
      setError("Couldn't update Core. Please try again.");
    }
  };

  const busy = mutation.isPending || restarting;

  return (
    <article
      className={`rounded-panel border p-4 transition-colors ${
        enabled ? "border-accent/25 bg-accent/5" : "border-border bg-surface-elevated"
      }`}
    >
      <div className="flex items-center justify-between gap-3">
        <div>
          <div className="flex items-center gap-2">
            <span className="text-base" aria-hidden="true">◎</span>
            <h3 className="text-base font-medium">Core</h3>
          </div>
          <p className="mt-1 text-sm text-ink-muted">
            On-demand guided meditations — your assistant composes a quiet ten
            minutes from your week, then voices it aloud.
          </p>
          {enabled && !restarting && !error && (
            <p className="mt-2 text-xs text-ink-muted">
              Open the Core tab and press the orb whenever you want a sit.
            </p>
          )}
          {restarting && (
            <p className="mt-2 text-xs text-amber-text" role="status">
              Configuring your assistant... this takes about a minute.
            </p>
          )}
          {error && (
            <p className="mt-2 text-xs text-rose-text" role="alert">
              {error}
            </p>
          )}
        </div>
        <button
          type="button"
          role="switch"
          aria-checked={enabled}
          aria-label={enabled ? "Disable Core" : "Enable Core"}
          onClick={handleToggle}
          disabled={busy}
          className={`relative inline-flex h-6 w-11 shrink-0 cursor-pointer rounded-full border-2 border-transparent transition-colors ${
            enabled ? "bg-accent" : "bg-border"
          } ${busy ? "opacity-50" : ""}`}
        >
          <span
            className={`pointer-events-none inline-block h-5 w-5 transform rounded-full bg-white shadow transition-transform ${
              enabled ? "translate-x-5" : "translate-x-0"
            }`}
          />
        </button>
      </div>
    </article>
  );
}

function IntegrationsContent() {
  const searchParams = useSearchParams();
  const { data, isLoading, error } = useIntegrationsQuery();
  const disconnect = useDisconnectIntegrationMutation();
  const authorize = useOAuthAuthorizeMutation();
  const [connectingProvider, setConnectingProvider] = useState<string | null>(null);
  const [connectError, setConnectError] = useState<string | null>(null);

  const connectedProvider = searchParams.get("connected");
  const oauthError = searchParams.get("error");

  const handleConnect = async (provider: string) => {
    setConnectingProvider(provider);
    setConnectError(null);
    try {
      const result = await authorize.mutateAsync(provider);
      window.location.assign(result.url);
    } catch (err) {
      setConnectError(err instanceof Error ? err.message : "Could not start connection. Please try again.");
    } finally {
      setConnectingProvider(null);
    }
  };

  if (isLoading) {
    return <SectionCardSkeleton lines={4} />;
  }

  return (
    <SectionCard
      title="Integrations"
      subtitle="OAuth tokens are stored in tenant-scoped Azure Key Vault secrets"
    >
      <PendingConfigChip />

      {connectedProvider && (
        <p className="mb-4 rounded-panel border border-emerald-text/20 bg-emerald-bg p-3 text-sm text-emerald-text">
          Successfully connected {connectedProvider}.
        </p>
      )}

      {oauthError && (
        <p className="mb-4 rounded-panel border border-rose-border bg-rose-bg p-3 text-sm text-rose-text">
          OAuth error: {oauthError}
        </p>
      )}

      {error && (
        <p className="rounded-panel border border-rose-border bg-rose-bg p-3 text-sm text-rose-text">
          Could not fetch integrations. Please refresh and try again.
        </p>
      )}

      <AppCard />
      <ErrorBoundary fallback={<p className="rounded-panel border border-rose-border bg-rose-bg p-3 text-sm text-rose-text">Could not load Gravity settings.</p>}>
        <GravityCard />
      </ErrorBoundary>
      <ErrorBoundary fallback={<p className="rounded-panel border border-rose-border bg-rose-bg p-3 text-sm text-rose-text">Could not load Fuel settings.</p>}>
        <FuelCard />
      </ErrorBoundary>
      <ErrorBoundary fallback={<p className="rounded-panel border border-rose-border bg-rose-bg p-3 text-sm text-rose-text">Could not load Core settings.</p>}>
        <CoreCard />
      </ErrorBoundary>

      {connectError && (
        <p className="mt-3 rounded-panel border border-rose-border bg-rose-bg p-3 text-sm text-rose-text">
          {connectError}
        </p>
      )}

      <div className="mt-4 grid gap-3 md:grid-cols-2">
        {providers.map((provider) => {
          const integration = data?.find((item) => item.provider === provider.key);
          const isActive = integration?.status === "active";
          const needsReconnect =
            integration?.status === "revoked" ||
            integration?.status === "error" ||
            integration?.status === "expired";
          const isConnected = Boolean(integration);

          // Description: reflect actual status, not just record presence
          const description = isActive
            ? (integration?.provider_email || "Connected")
            : needsReconnect
            ? "Reconnection required"
            : (provider.description ?? "Not connected yet.");

          // Badge: pass raw status so StatusPill renders its per-status tone
          // (revoked=slate, expired=amber, error=rose — deliberately distinct).
          // needsReconnect is used only for description/button, not the badge.
          const badgeStatus = isActive
            ? "active"
            : isConnected
            ? (integration?.status ?? "pending")
            : "pending";

          return (
            <article key={provider.key} className="rounded-panel border border-border bg-surface-elevated p-4">
              <div className="flex items-center justify-between gap-2">
                <div className="flex items-center gap-2">
                  <ServiceIcon provider={provider.key} />
                  <h3 className="text-base font-medium">{provider.label}</h3>
                </div>
                <StatusPill status={badgeStatus} />
              </div>

              <p className="mt-2 text-sm text-ink-muted">{description}</p>

              <div className="mt-4 flex gap-2">
                {/* Show Reconnect for revoked/error/expired, Connect for not connected */}
                {(!isActive) && (
                  <button
                    className="rounded-full border border-border-strong px-3 py-1.5 text-sm hover:border-border-strong disabled:cursor-not-allowed disabled:opacity-45"
                    type="button"
                    disabled={connectingProvider !== null}
                    onClick={() => handleConnect(provider.key)}
                  >
                    {connectingProvider === provider.key
                      ? "Redirecting..."
                      : needsReconnect
                      ? "Reconnect"
                      : "Connect"}
                  </button>
                )}
                {isConnected && (
                  <button
                    className="rounded-full border border-border-strong px-3 py-1.5 text-sm hover:border-border-strong disabled:cursor-not-allowed disabled:opacity-45"
                    type="button"
                    disabled={disconnect.isPending && disconnect.variables === integration!.id}
                    onClick={() => disconnect.mutate(integration!.id)}
                  >
                    {disconnect.isPending && disconnect.variables === integration!.id
                      ? "Disconnecting..."
                      : "Disconnect"}
                  </button>
                )}
              </div>
            </article>
          );
        })}
      </div>
    </SectionCard>
  );
}

export default function SettingsIntegrationsPage() {
  return (
    <div className="space-y-4">
      <Suspense fallback={<SectionCardSkeleton lines={4} />}>
        <IntegrationsContent />
      </Suspense>
    </div>
  );
}
