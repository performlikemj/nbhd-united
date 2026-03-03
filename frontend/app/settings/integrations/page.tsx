"use client";

import { useSearchParams } from "next/navigation";
import { Suspense, useState } from "react";

import { SectionCard } from "@/components/section-card";
import { SectionCardSkeleton } from "@/components/skeleton";
import { StatusPill } from "@/components/status-pill";
import {
  useDisconnectIntegrationMutation,
  useGenerateTelegramLinkMutation,
  useIntegrationsQuery,
  useOAuthAuthorizeMutation,
  useTelegramStatusQuery,
  useUnlinkTelegramMutation,
} from "@/lib/queries";
import type { TelegramLinkResponse } from "@/lib/api";
import { ServiceIcon } from "@/components/service-icon";

const providers: { key: string; label: string; description?: string }[] = [
  { key: "gmail", label: "Gmail" },
  { key: "google-calendar", label: "Google Calendar" },
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

      <TelegramCard />

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
