"use client";

import Link from "next/link";

import { SectionCard } from "@/components/section-card";
import { SectionCardSkeleton, StatCardSkeleton } from "@/components/skeleton";
import { StatCard } from "@/components/stat-card";
import { StatusPill } from "@/components/status-pill";
import { useDashboardQuery } from "@/lib/queries";

export default function HomePage() {
  const { data: dashboard, isLoading, error } = useDashboardQuery();

  const totalTokens = dashboard
    ? (dashboard.usage.total_input_tokens + dashboard.usage.total_output_tokens).toLocaleString()
    : "0";

  return (
    <div className="space-y-6">
      <section className="rounded-panel border border-ink/10 bg-white/90 p-6 shadow-panel animate-reveal">
        <p className="font-mono text-xs uppercase tracking-[0.24em] text-ink/70">Control Plane</p>
        <h2 className="mt-2 text-3xl font-semibold">Subscriber Workspace</h2>
        <p className="mt-3 max-w-2xl text-sm text-ink/70">
          Manage Telegram onboarding, Stripe subscription state, OAuth connections, and monthly usage from a single surface.
        </p>
      </section>

      {isLoading ? (
        <>
          <div className="grid gap-4 md:grid-cols-4">
            <StatCardSkeleton />
            <StatCardSkeleton />
            <StatCardSkeleton />
            <StatCardSkeleton />
          </div>
          <div className="grid gap-4 md:grid-cols-2">
            <SectionCardSkeleton lines={4} />
            <SectionCardSkeleton lines={4} />
          </div>
        </>
      ) : error ? (
        <p className="rounded-panel border border-rose-200 bg-rose-50 p-4 text-sm text-rose-900">
          Tenant profile is not available yet. Complete onboarding to activate your dashboard.
        </p>
      ) : dashboard ? (
        <>
          <div className="grid gap-4 md:grid-cols-4">
            <StatCard label="Status" value={dashboard.tenant.status.toUpperCase()} hint="Provisioning tracks OpenClaw container readiness." />
            <StatCard label="Model Tier" value={dashboard.tenant.model_tier.toUpperCase()} hint="Basic includes Sonnet. Plus unlocks Opus." tone="signal" />
            <StatCard label="Total Tokens" value={totalTokens} />
            <StatCard label="Total Cost" value={`$${Number(dashboard.usage.total_cost).toFixed(2)}`} />
          </div>

          <div className="grid gap-4 md:grid-cols-2">
            <SectionCard title="Tenant Snapshot" subtitle="Current runtime and usage summary" delay={100}>
              <dl className="grid grid-cols-1 gap-3 text-sm sm:grid-cols-2">
                <div>
                  <dt className="font-mono text-[11px] uppercase tracking-[0.14em] text-ink/60">Messages Today</dt>
                  <dd className="mt-1 text-base text-ink">{dashboard.usage.messages_today.toLocaleString()}</dd>
                </div>
                <div>
                  <dt className="font-mono text-[11px] uppercase tracking-[0.14em] text-ink/60">Messages This Month</dt>
                  <dd className="mt-1 text-base text-ink">{dashboard.usage.messages_this_month.toLocaleString()}</dd>
                </div>
                <div>
                  <dt className="font-mono text-[11px] uppercase tracking-[0.14em] text-ink/60">Integrations</dt>
                  <dd className="mt-1 text-base text-ink">{dashboard.connections.length} connected</dd>
                </div>
                <div>
                  <dt className="font-mono text-[11px] uppercase tracking-[0.14em] text-ink/60">State</dt>
                  <dd className="mt-1"><StatusPill status={dashboard.tenant.status} /></dd>
                </div>
              </dl>
            </SectionCard>

            <SectionCard title="Quick Actions" subtitle="Primary subscriber operations" delay={180}>
              <div className="grid gap-3 sm:grid-cols-2">
                <Link className="rounded-panel border border-ink/15 bg-white px-4 py-3 text-sm hover:border-ink/30" href="/onboarding">
                  Continue onboarding
                </Link>
                <Link className="rounded-panel border border-ink/15 bg-white px-4 py-3 text-sm hover:border-ink/30" href="/integrations">
                  Manage integrations
                </Link>
                <Link className="rounded-panel border border-ink/15 bg-white px-4 py-3 text-sm hover:border-ink/30" href="/usage">
                  Review usage and budget
                </Link>
                <Link className="rounded-panel border border-ink/15 bg-white px-4 py-3 text-sm hover:border-ink/30" href="/billing">
                  Open billing controls
                </Link>
                <Link className="rounded-panel border border-ink/15 bg-white px-4 py-3 text-sm hover:border-ink/30" href="/automations">
                  Configure automations
                </Link>
              </div>
            </SectionCard>
          </div>
        </>
      ) : null}
    </div>
  );
}
