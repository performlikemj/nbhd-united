"use client";

import { useSearchParams } from "next/navigation";
import { Suspense, useState } from "react";

import { SectionCard } from "@/components/section-card";
import { SectionCardSkeleton } from "@/components/skeleton";
import { StatusPill } from "@/components/status-pill";
import {
  useDisconnectIntegrationMutation,
  useGenerateTelegramLinkMutation,
  useGenerateLineLinkMutation,
  useIntegrationsQuery,
  useLineStatusQuery,
  useOAuthAuthorizeMutation,
  useTelegramStatusQuery,
  useTenantQuery,
  useUnlinkLineMutation,
  useUnlinkTelegramMutation,
  useUpdateFinanceSettingsMutation,
  useUpdateFuelSettingsMutation,
  useFuelProfileQuery,
} from "@/lib/queries";
import type { TelegramLinkResponse, LineLinkResponse } from "@/lib/api";
import { ServiceIcon } from "@/components/service-icon";
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

function TelegramCard() {
  const [linkData, setLinkData] = useState<TelegramLinkResponse | null>(null);
  // Always fetch status — not just after generating a link
  const { data: status } = useTelegramStatusQuery(true);
  const generateLink = useGenerateTelegramLinkMutation();
  const unlinkMutation = useUnlinkTelegramMutation();

  const linked = status?.linked ?? false;

  const handleConnect = async () => {
    try {
      const data = await generateLink.mutateAsync();
      setLinkData(data);
    } catch {
      // handled by mutation
    }
  };

  return (
    <article className="rounded-panel border border-border bg-surface-elevated p-4">
      <div className="flex items-center justify-between gap-2">
        <div className="flex items-center gap-2">
          <ServiceIcon provider="telegram" />
          <h3 className="text-base font-medium">Telegram</h3>
        </div>
        <StatusPill status={linked ? "active" : "pending"} />
      </div>

      {linked ? (
        <>
          <p className="mt-2 text-sm text-ink-muted">
            {status?.telegram_username ? `Connected as @${status.telegram_username}` : "Connected"}
          </p>
          <div className="mt-4">
            <button
              className="rounded-full border border-border-strong px-3 py-1.5 text-sm hover:border-border-strong disabled:cursor-not-allowed disabled:opacity-45"
              type="button"
              disabled={unlinkMutation.isPending}
              onClick={() => unlinkMutation.mutate()}
            >
              {unlinkMutation.isPending ? "Unlinking..." : "Unlink"}
            </button>
          </div>
        </>
      ) : (
        <>
          <p className="mt-2 text-sm text-ink-muted">Not connected yet.</p>

          {!linkData && (
            <div className="mt-4">
              <button
                className="rounded-full border border-border-strong px-3 py-1.5 text-sm hover:border-border-strong disabled:cursor-not-allowed disabled:opacity-45"
                type="button"
                disabled={generateLink.isPending}
                onClick={handleConnect}
              >
                {generateLink.isPending ? "Generating..." : "Connect"}
              </button>
            </div>
          )}

          {linkData && (
            <div className="mt-3 space-y-3">
              <p className="text-sm text-ink-muted">Scan the QR code or tap the link:</p>
              <div className="flex items-start gap-4">
                {/* eslint-disable-next-line @next/next/no-img-element */}
                <img
                  src={linkData.qr_code}
                  alt="Telegram QR Code"
                  className="h-32 w-32 rounded-panel border border-border"
                />
                <a
                  href={linkData.deep_link}
                  target="_blank"
                  rel="noopener noreferrer"
                  className="inline-block rounded-full bg-[#0088cc] px-4 py-2 text-sm text-white transition hover:bg-[#0077b5]"
                >
                  Open in Telegram
                </a>
              </div>
            </div>
          )}
        </>
      )}
    </article>
  );
}

function LineCard() {
  const [linkData, setLinkData] = useState<LineLinkResponse | null>(null);
  const { data: status } = useLineStatusQuery(true);
  const generateLink = useGenerateLineLinkMutation();
  const unlinkMutation = useUnlinkLineMutation();

  const linked = status?.linked ?? false;

  const handleConnect = async () => {
    try {
      const data = await generateLink.mutateAsync();
      setLinkData(data);
    } catch {
      // handled by mutation
    }
  };

  return (
    <article className="rounded-panel border border-border bg-surface-elevated p-4">
      <div className="flex items-center justify-between gap-2">
        <div className="flex items-center gap-2">
          <ServiceIcon provider="line" />
          <h3 className="text-base font-medium">LINE</h3>
        </div>
        <StatusPill status={linked ? "active" : "pending"} />
      </div>

      {linked ? (
        <>
          <p className="mt-2 text-sm text-ink-muted">
            {status?.line_display_name ? `Connected as ${status.line_display_name}` : "Connected"}
          </p>
          <div className="mt-4">
            <button
              className="rounded-full border border-border-strong px-3 py-1.5 text-sm hover:border-border-strong disabled:cursor-not-allowed disabled:opacity-45"
              type="button"
              disabled={unlinkMutation.isPending}
              onClick={() => unlinkMutation.mutate()}
            >
              {unlinkMutation.isPending ? "Unlinking..." : "Unlink"}
            </button>
          </div>
        </>
      ) : (
        <>
          <p className="mt-2 text-sm text-ink-muted">Not connected yet.</p>

          {!linkData && (
            <div className="mt-4">
              <button
                className="rounded-full border border-[#06C755] px-3 py-1.5 text-sm text-[#06C755] hover:bg-[#06C755]/10 disabled:cursor-not-allowed disabled:opacity-45"
                type="button"
                disabled={generateLink.isPending}
                onClick={handleConnect}
              >
                {generateLink.isPending ? "Generating..." : "Connect LINE"}
              </button>
            </div>
          )}

          {linkData && (
            <div className="mt-3 space-y-3">
              <div className="rounded-panel border border-[#06C755]/20 bg-[#06C755]/5 p-3">
                <p className="text-sm font-medium text-ink">How to connect:</p>
                <ol className="mt-1.5 list-inside list-decimal space-y-1 text-sm text-ink-muted">
                  <li>Tap <strong>&quot;Open in LINE&quot;</strong> below (or scan the QR code)</li>
                  <li>LINE will open with a message ready to send</li>
                  <li>Tap <strong>Send</strong> — that&apos;s it!</li>
                </ol>
              </div>
              <div className="flex items-start gap-4">
                {/* eslint-disable-next-line @next/next/no-img-element */}
                <img
                  src={linkData.qr_code}
                  alt="LINE QR Code"
                  className="h-32 w-32 rounded-panel border border-border"
                />
                <div className="flex flex-col gap-2">
                  <a
                    href={linkData.deep_link}
                    target="_blank"
                    rel="noopener noreferrer"
                    className="inline-block rounded-full bg-[#06C755] px-4 py-2 text-center text-sm text-white transition hover:bg-[#05b04d]"
                  >
                    Open in LINE
                  </a>
                  <p className="text-xs text-ink-muted">Link expires in 15 minutes</p>
                </div>
              </div>
            </div>
          )}
        </>
      )}
    </article>
  );
}

function GravityCard() {
  const { data: tenant } = useTenantQuery();
  const mutation = useUpdateFinanceSettingsMutation();
  const enabled = tenant?.finance_enabled ?? false;

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
        </div>
        <button
          type="button"
          role="switch"
          aria-checked={enabled}
          aria-label={enabled ? "Disable Gravity" : "Enable Gravity"}
          onClick={() => mutation.mutate({ finance_enabled: !enabled })}
          disabled={mutation.isPending}
          className={`relative inline-flex h-6 w-11 shrink-0 cursor-pointer rounded-full border-2 border-transparent transition-colors ${
            enabled ? "bg-accent" : "bg-border"
          } ${mutation.isPending ? "opacity-50" : ""}`}
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
  const [showRestartPrompt, setShowRestartPrompt] = useState(false);
  const [restarting, setRestarting] = useState(false);

  const handleToggle = async () => {
    if (enabled) {
      mutation.mutate({ fuel_enabled: false });
      return;
    }
    mutation.mutate(
      { fuel_enabled: true },
      {
        onSuccess: (data) => {
          if (data.restart_required) {
            setShowRestartPrompt(true);
          }
        },
      },
    );
  };

  const handleRestart = async () => {
    setRestarting(true);
    try {
      const { restartFuelAssistant } = await import("@/lib/api");
      await restartFuelAssistant();
    } catch {
      // Restart failed — user can retry
    } finally {
      setRestarting(false);
      setShowRestartPrompt(false);
    }
  };

  const handleDecline = () => {
    mutation.mutate({ fuel_enabled: false });
    setShowRestartPrompt(false);
  };

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
          {enabled && !showRestartPrompt && <FuelProfileStatus />}
        </div>
        {!showRestartPrompt && (
          <button
            type="button"
            role="switch"
            aria-checked={enabled}
            aria-label={enabled ? "Disable Fuel" : "Enable Fuel"}
            onClick={handleToggle}
            disabled={mutation.isPending || restarting}
            className={`relative inline-flex h-6 w-11 shrink-0 cursor-pointer rounded-full border-2 border-transparent transition-colors ${
              enabled ? "bg-accent" : "bg-border"
            } ${mutation.isPending || restarting ? "opacity-50" : ""}`}
          >
            <span
              className={`pointer-events-none inline-block h-5 w-5 transform rounded-full bg-white shadow transition-transform ${
                enabled ? "translate-x-5" : "translate-x-0"
              }`}
            />
          </button>
        )}
      </div>
      {showRestartPrompt && (
        <div className="mt-3 rounded-xl border border-amber-border bg-amber-bg p-3">
          <p className="text-sm text-amber-text">
            Enabling Fuel requires restarting your assistant. Active conversations will be briefly interrupted.
          </p>
          <div className="mt-2 flex gap-2">
            <button
              type="button"
              onClick={handleRestart}
              disabled={restarting}
              className="rounded-lg bg-accent px-3 py-1.5 text-xs font-medium text-white transition-all hover:brightness-110 disabled:opacity-50"
            >
              {restarting ? "Restarting..." : "Restart now"}
            </button>
            <button
              type="button"
              onClick={handleDecline}
              disabled={restarting}
              className="rounded-lg border border-border px-3 py-1.5 text-xs font-medium text-ink-muted transition-all hover:bg-surface-hover disabled:opacity-50"
            >
              Not now
            </button>
          </div>
        </div>
      )}
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

      <ErrorBoundary fallback={<p className="rounded-panel border border-rose-border bg-rose-bg p-3 text-sm text-rose-text">Could not load Telegram settings.</p>}>
        <TelegramCard />
      </ErrorBoundary>
      <ErrorBoundary fallback={<p className="rounded-panel border border-rose-border bg-rose-bg p-3 text-sm text-rose-text">Could not load LINE settings.</p>}>
        <LineCard />
      </ErrorBoundary>
      <ErrorBoundary fallback={<p className="rounded-panel border border-rose-border bg-rose-bg p-3 text-sm text-rose-text">Could not load Gravity settings.</p>}>
        <GravityCard />
      </ErrorBoundary>
      <ErrorBoundary fallback={<p className="rounded-panel border border-rose-border bg-rose-bg p-3 text-sm text-rose-text">Could not load Fuel settings.</p>}>
        <FuelCard />
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
          const isRevoked = integration?.status === "revoked" || integration?.status === "error";
          const isConnected = Boolean(integration);

          // Description: reflect actual status, not just record presence
          const description = isActive
            ? (integration?.provider_email || "Connected")
            : isRevoked
            ? "Reconnection required"
            : (provider.description ?? "Not connected yet.");

          // Badge: revoked shows as error, no integration = pending
          const badgeStatus = isActive
            ? "active"
            : isRevoked
            ? "error"
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
                {/* Show Reconnect for revoked/error, Connect for not connected */}
                {(!isActive) && (
                  <button
                    className="rounded-full border border-border-strong px-3 py-1.5 text-sm hover:border-border-strong disabled:cursor-not-allowed disabled:opacity-45"
                    type="button"
                    disabled={connectingProvider !== null}
                    onClick={() => handleConnect(provider.key)}
                  >
                    {connectingProvider === provider.key
                      ? "Redirecting..."
                      : isRevoked
                      ? "Reconnect"
                      : "Connect"}
                  </button>
                )}
                {isConnected && (
                  <button
                    className="rounded-full border border-border-strong px-3 py-1.5 text-sm hover:border-border-strong disabled:cursor-not-allowed disabled:opacity-45"
                    type="button"
                    disabled={disconnect.isPending}
                    onClick={() => disconnect.mutate(integration!.id)}
                  >
                    {disconnect.isPending ? "Disconnecting..." : "Disconnect"}
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
