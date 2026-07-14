"use client";

import { useSearchParams } from "next/navigation";
import { Suspense, useState } from "react";

import { PendingConfigChip } from "@/components/pending-config-chip";
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
  useUpdateCoreSettingsMutation,
  useFuelProfileQuery,
} from "@/lib/queries";
import type { TelegramLinkResponse, LineLinkResponse } from "@/lib/api";
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

// ── Companion messaging channels ─────────────────────────────────────────────
// Telegram and LINE stay supported as optional companion surfaces. The iOS app
// is the primary experience; these let a subscriber also reach their assistant
// from a chat client they already keep open. Delivery preference is automatic
// (app-first when installed), so there is deliberately no channel toggle here.

function TelegramGlyph() {
  return (
    <svg
      width={20}
      height={20}
      viewBox="0 0 24 24"
      fill="currentColor"
      aria-hidden="true"
      className="shrink-0 text-[#229ED9]"
    >
      <path d="M9.78 18.65l.28-4.23 7.68-6.92c.34-.31-.07-.46-.52-.19L7.74 13.3 3.64 12c-.88-.25-.89-.86.2-1.3l15.97-6.16c.73-.33 1.43.18 1.15 1.3l-2.72 12.81c-.19.91-.74 1.13-1.5.71l-4.14-3.06-1.99 1.93c-.23.23-.42.42-.83.42z" />
    </svg>
  );
}

function LineGlyph() {
  return (
    <svg
      width={20}
      height={20}
      viewBox="0 0 24 24"
      fill="currentColor"
      aria-hidden="true"
      className="shrink-0 text-[#06C755]"
    >
      <path d="M12 2.75c-5.24 0-9.5 3.44-9.5 7.68 0 3.8 3.38 6.98 7.94 7.58.31.07.73.2.84.47.09.24.06.62.03.87l-.13.81c-.04.24-.19.94.82.51 1.02-.43 5.48-3.23 7.48-5.53 1.38-1.51 2.02-3.05 2.02-5.19 0-4.24-4.26-7.68-9.52-7.68zM8.2 12.62H6.31c-.27 0-.5-.22-.5-.5V8.34c0-.28.23-.5.5-.5s.5.22.5.5v3.28H8.2c.28 0 .5.22.5.5s-.22.5-.5.5zm1.94-.5c0 .28-.22.5-.5.5s-.5-.22-.5-.5V8.34c0-.28.22-.5.5-.5s.5.22.5.5v3.78zm4.42 0c0 .21-.13.4-.34.47a.51.51 0 0 1-.55-.16l-1.93-2.63v2.32c0 .28-.22.5-.5.5s-.5-.22-.5-.5V8.34c0-.21.14-.4.34-.47.2-.07.43 0 .55.17l1.94 2.63V8.34c0-.28.22-.5.5-.5s.49.22.49.5v3.78zm3-2.39c.28 0 .5.22.5.5s-.22.5-.5.5h-1.39v.89h1.39c.28 0 .5.23.5.5 0 .28-.22.5-.5.5h-1.89c-.27 0-.5-.22-.5-.5V8.34c0-.28.23-.5.5-.5h1.89c.28 0 .5.22.5.5s-.22.5-.5.5h-1.39v.89h1.39z" />
    </svg>
  );
}

function TelegramCard() {
  const [linkData, setLinkData] = useState<TelegramLinkResponse | null>(null);
  const [confirmingUnlink, setConfirmingUnlink] = useState(false);
  // Always fetch status — not just after generating a link. Fast 3s polling
  // only while a pairing QR/deep-link is on screen; 15s otherwise.
  const { data: status } = useTelegramStatusQuery(true, !!linkData);
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
          <TelegramGlyph />
          <h3 className="text-base font-medium">Telegram</h3>
        </div>
        <StatusPill status={linked ? "active" : "pending"} />
      </div>

      {linked ? (
        <>
          <p className="mt-2 text-sm text-ink-muted">
            {status?.telegram_username ? `Connected as @${status.telegram_username}` : "Connected"}
          </p>
          {confirmingUnlink ? (
            <div className="mt-4 flex flex-wrap items-center gap-2">
              <span className="text-sm text-ink-muted">Unlink Telegram?</span>
              <button
                className="rounded-full border border-rose-border px-3 py-1.5 text-sm text-rose-text hover:bg-rose-bg disabled:cursor-not-allowed disabled:opacity-45 min-h-[44px]"
                type="button"
                disabled={unlinkMutation.isPending}
                onClick={() => {
                  unlinkMutation.mutate();
                  setConfirmingUnlink(false);
                }}
              >
                {unlinkMutation.isPending ? "Unlinking..." : "Confirm unlink"}
              </button>
              <button
                className="rounded-full border border-border-strong px-3 py-1.5 text-sm hover:border-border-strong min-h-[44px]"
                type="button"
                onClick={() => setConfirmingUnlink(false)}
              >
                Cancel
              </button>
            </div>
          ) : (
            <div className="mt-4">
              <button
                className="rounded-full border border-border-strong px-3 py-1.5 text-sm hover:border-border-strong disabled:cursor-not-allowed disabled:opacity-45 min-h-[44px]"
                type="button"
                onClick={() => setConfirmingUnlink(true)}
              >
                Unlink
              </button>
            </div>
          )}
        </>
      ) : (
        <>
          <p className="mt-2 text-sm text-ink-muted">
            Optional. Chat with your assistant from Telegram if you already keep it open.
          </p>

          {!linkData && (
            <div className="mt-4">
              <button
                className="rounded-full border border-border-strong px-3 py-1.5 text-sm hover:border-border-strong disabled:cursor-not-allowed disabled:opacity-45 min-h-[44px]"
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
                  className="inline-flex min-h-[44px] items-center rounded-full bg-[#229ED9] px-4 py-2 text-sm text-white transition hover:brightness-110"
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
  const [confirmingUnlink, setConfirmingUnlink] = useState(false);
  // 3s polling only while the pairing QR/deep-link is visible; 15s otherwise.
  const { data: status } = useLineStatusQuery(true, !!linkData);
  const generateLink = useGenerateLineLinkMutation();
  const unlinkMutation = useUnlinkLineMutation();

  const linked = status?.linked ?? false;
  // Fleet-wide LINE Push monthly quota. When exhausted the platform can't
  // deliver to LINE, so we don't invite new links until it resets — greyed
  // out with a note rather than silently failing after they connect.
  const quotaExhausted = status?.quota?.exhausted ?? false;

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
          <LineGlyph />
          <h3 className="text-base font-medium">LINE</h3>
        </div>
        <StatusPill status={linked ? "active" : "pending"} />
      </div>

      {linked ? (
        <>
          <p className="mt-2 text-sm text-ink-muted">
            {status?.line_display_name ? `Connected as ${status.line_display_name}` : "Connected"}
          </p>
          {confirmingUnlink ? (
            <div className="mt-4 flex flex-wrap items-center gap-2">
              <span className="text-sm text-ink-muted">Unlink LINE?</span>
              <button
                className="rounded-full border border-rose-border px-3 py-1.5 text-sm text-rose-text hover:bg-rose-bg disabled:cursor-not-allowed disabled:opacity-45 min-h-[44px]"
                type="button"
                disabled={unlinkMutation.isPending}
                onClick={() => {
                  unlinkMutation.mutate();
                  setConfirmingUnlink(false);
                }}
              >
                {unlinkMutation.isPending ? "Unlinking..." : "Confirm unlink"}
              </button>
              <button
                className="rounded-full border border-border-strong px-3 py-1.5 text-sm hover:border-border-strong min-h-[44px]"
                type="button"
                onClick={() => setConfirmingUnlink(false)}
              >
                Cancel
              </button>
            </div>
          ) : (
            <div className="mt-4">
              <button
                className="rounded-full border border-border-strong px-3 py-1.5 text-sm hover:border-border-strong disabled:cursor-not-allowed disabled:opacity-45 min-h-[44px]"
                type="button"
                onClick={() => setConfirmingUnlink(true)}
              >
                Unlink
              </button>
            </div>
          )}
        </>
      ) : quotaExhausted ? (
        <>
          <p className="mt-2 text-sm text-ink-muted">
            Optional. Chat with your assistant from LINE if you already keep it open.
          </p>
          <div className="mt-3 rounded-panel border border-amber-border bg-amber-bg p-3">
            <p className="text-sm text-amber-text">
              LINE&rsquo;s monthly messaging allowance is used up across the platform.
              You&rsquo;ll be able to connect LINE again at the start of next month.
            </p>
          </div>
          <div className="mt-4">
            <button
              className="rounded-full border border-border px-3 py-1.5 text-sm text-ink-faint cursor-not-allowed opacity-45 min-h-[44px]"
              type="button"
              disabled
              aria-disabled="true"
              title="LINE monthly messaging allowance reached. Connectable again at the start of next month."
            >
              Connect LINE
            </button>
          </div>
        </>
      ) : (
        <>
          <p className="mt-2 text-sm text-ink-muted">
            Optional. Chat with your assistant from LINE if you already keep it open.
          </p>

          {!linkData && (
            <div className="mt-4">
              <button
                className="rounded-full border border-[#06C755] px-3 py-1.5 text-sm text-[#06C755] hover:bg-[#06C755]/10 disabled:cursor-not-allowed disabled:opacity-45 min-h-[44px]"
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
                    className="inline-flex min-h-[44px] items-center justify-center rounded-full bg-[#06C755] px-4 py-2 text-center text-sm text-white transition hover:brightness-110"
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
            minutes from your journal, goals, and recent activity, then voices it
            aloud.
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

      <p className="mb-3 mt-4 text-xs text-ink-faint">
        The app is the best way to reach your assistant. You can also connect a
        companion chat channel below.
      </p>
      <div className="space-y-3">
        <ErrorBoundary fallback={<p className="rounded-panel border border-rose-border bg-rose-bg p-3 text-sm text-rose-text">Could not load Telegram settings.</p>}>
          <TelegramCard />
        </ErrorBoundary>
        <ErrorBoundary fallback={<p className="rounded-panel border border-rose-border bg-rose-bg p-3 text-sm text-rose-text">Could not load LINE settings.</p>}>
          <LineCard />
        </ErrorBoundary>
      </div>

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
