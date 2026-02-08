"use client";

import { useSearchParams } from "next/navigation";
import { Suspense } from "react";

import { SectionCard } from "@/components/section-card";
import { SectionCardSkeleton } from "@/components/skeleton";
import { StatusPill } from "@/components/status-pill";
import {
  useDisconnectIntegrationMutation,
  useIntegrationsQuery,
  useOAuthAuthorizeMutation,
} from "@/lib/queries";

const providers = [
  { key: "gmail", label: "Gmail" },
  { key: "google-calendar", label: "Google Calendar" },
  { key: "sautai", label: "Sautai" },
];

function IntegrationsContent() {
  const searchParams = useSearchParams();
  const { data, isLoading, error } = useIntegrationsQuery();
  const disconnect = useDisconnectIntegrationMutation();
  const authorize = useOAuthAuthorizeMutation();

  const connectedProvider = searchParams.get("connected");
  const oauthError = searchParams.get("error");

  const handleConnect = async (provider: string) => {
    try {
      const result = await authorize.mutateAsync(provider);
      window.location.assign(result.url);
    } catch {
      // Error shown via mutation state
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
        <p className="mb-4 rounded-panel border border-emerald-200 bg-emerald-50 p-3 text-sm text-emerald-900">
          Successfully connected {connectedProvider}.
        </p>
      )}

      {oauthError && (
        <p className="mb-4 rounded-panel border border-rose-200 bg-rose-50 p-3 text-sm text-rose-900">
          OAuth error: {oauthError}
        </p>
      )}

      {error ? (
        <p className="rounded-panel border border-rose-200 bg-rose-50 p-3 text-sm text-rose-900">
          Could not fetch integrations. Confirm authentication/session wiring.
        </p>
      ) : null}

      <div className="grid gap-3 md:grid-cols-2">
        {providers.map((provider) => {
          const integration = data?.find((item) => item.provider === provider.key);
          const connected = Boolean(integration);

          return (
            <article key={provider.key} className="rounded-panel border border-ink/15 bg-white p-4">
              <div className="flex items-center justify-between gap-2">
                <h3 className="text-base font-medium">{provider.label}</h3>
                <StatusPill status={integration?.status ?? "pending"} />
              </div>

              <p className="mt-2 text-sm text-ink/70">
                {connected
                  ? integration?.provider_email || "Connected"
                  : "Not connected yet."}
              </p>

              <div className="mt-4 flex gap-2">
                <button
                  className="rounded-full border border-ink/20 px-3 py-1.5 text-sm hover:border-ink/40 disabled:cursor-not-allowed disabled:opacity-45"
                  type="button"
                  disabled={connected || authorize.isPending}
                  onClick={() => handleConnect(provider.key)}
                >
                  {authorize.isPending ? "Redirecting..." : "Connect"}
                </button>
                <button
                  className="rounded-full border border-ink/20 px-3 py-1.5 text-sm hover:border-ink/40 disabled:cursor-not-allowed disabled:opacity-45"
                  type="button"
                  disabled={!integration || disconnect.isPending}
                  onClick={() => {
                    if (integration) {
                      disconnect.mutate(integration.id);
                    }
                  }}
                >
                  Disconnect
                </button>
              </div>
            </article>
          );
        })}
      </div>
    </SectionCard>
  );
}

export default function IntegrationsPage() {
  return (
    <div className="space-y-4">
      <Suspense fallback={<SectionCardSkeleton lines={4} />}>
        <IntegrationsContent />
      </Suspense>
    </div>
  );
}
