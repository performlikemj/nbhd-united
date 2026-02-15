"use client";

import Link from "next/link";

import { SectionCard } from "@/components/section-card";

export default function ReviewPage() {
  return (
    <div className="space-y-5">
      <SectionCard
        title="Stripe Review Access"
        subtitle="Use this page to validate policy controls and callback flows"
      >
        <p className="text-sm text-ink/75">
          This page is intentionally read-only and audit-focused. It does not expose any secrets.
        </p>
      </SectionCard>

      <SectionCard title="1) Access Model" subtitle="How registration and access controls work">
        <ul className="list-disc space-y-2 pl-5 text-sm text-ink/75">
          <li>Registration requires an invite code, validated server-side against a configured secret.</li>
          <li>All pages are publicly browsable; authenticated pages redirect to the login screen.</li>
          <li>OAuth callback routes are excluded from authentication requirements.</li>
          <li>Frontend callback landing pages render on <code>/integrations?connected=...</code> or <code>/integrations?error=...</code>.</li>
        </ul>
      </SectionCard>

      <SectionCard title="2) Reviewer Onboarding" subtitle="How to access authenticated areas">
        <ul className="list-disc space-y-2 pl-5 text-sm text-ink/75">
          <li>
            Navigate to{" "}
            <Link href="/signup" className="text-ink underline hover:text-ink/80">/signup</Link>{" "}
            and enter the invite code provided by the administrator.
          </li>
          <li>After registration, all authenticated pages (dashboard, billing, integrations, etc.) are accessible.</li>
          <li>
            Policy pages are always accessible without an account:{" "}
            <Link href="/legal/privacy" className="text-ink underline hover:text-ink/80">Privacy</Link>,{" "}
            <Link href="/legal/terms" className="text-ink underline hover:text-ink/80">Terms</Link>,{" "}
            <Link href="/legal/refund" className="text-ink underline hover:text-ink/80">Refund</Link>.
          </li>
        </ul>
      </SectionCard>

      <SectionCard title="3) Reviewer Checklist" subtitle="Required review evidence">
        <ul className="list-disc space-y-2 pl-5 text-sm text-ink/75">
          <li>Confirm policy matrix in <code>apps/orchestrator/tool_policy.py</code>.</li>
          <li>Confirm invite-code signup gate in <code>apps/tenants/auth_views.py</code>.</li>
          <li>Confirm runtime auth contract and header behavior in <code>runtime/openclaw/plugins/nbhd-google-tools/index.js</code>.</li>
          <li>Confirm callback-result render path in <code>frontend/components/app-shell.tsx</code>.</li>
          <li>Run <code>apps/orchestrator/test_azure_client.py</code> and <code>apps/integrations</code> tests.</li>
        </ul>
        <div className="mt-4 rounded-panel border border-ink/10 bg-ink/5 p-3 text-xs">
          <p className="font-mono text-ink/75">Relevant audit artifact: <code>docs/stripe-audit-policies.md</code>.</p>
        </div>
      </SectionCard>

      <SectionCard title="4) Reviewer Navigation" subtitle="Quick links">
        <div className="space-y-2 text-sm">
          <p className="text-ink/75">
            Callback-result URLs:
          </p>
          <ul className="list-disc space-y-1 pl-5 text-sm text-ink/75">
            <li>
              <Link href="/integrations?connected=demo" className="text-ink underline hover:text-ink/80">
                /integrations?connected=demo
              </Link>
            </li>
            <li>
              <Link href="/integrations?error=missing_scope" className="text-ink underline hover:text-ink/80">
                /integrations?error=missing_scope
              </Link>
            </li>
            <li>
              <Link href="/integrations" className="text-ink underline hover:text-ink/80">
                /integrations
              </Link>
            </li>
          </ul>
        </div>
      </SectionCard>

      <div className="space-y-3 rounded-panel border border-ink/10 bg-white p-4">
        <p className="text-xs uppercase tracking-[0.24em] text-ink/60">Audit support</p>
        <p className="text-sm text-ink/75">
          If you need a reviewer account, ask operations for an invite code and register at{" "}
          <Link href="/signup" className="text-ink underline hover:text-ink/80">/signup</Link>.
        </p>
      </div>
    </div>
  );
}
