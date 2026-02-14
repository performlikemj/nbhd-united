"use client";

import Link from "next/link";
import { FormEvent, useEffect, useState } from "react";

import { clearPreviewKey, getPreviewKey, setPreviewKey } from "@/lib/preview";
import { SectionCard } from "@/components/section-card";

export default function ReviewPage() {
  const [previewKey, setPreviewKeyState] = useState("");
  const [message, setMessage] = useState("");

  useEffect(() => {
    setPreviewKeyState(getPreviewKey() ?? "");
  }, []);

  const hasPreviewAccess = Boolean(previewKey);

  const saveReviewKey = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    const form = new FormData(event.currentTarget);
    const nextKey = (form.get("previewKey") as string | null)?.trim();
    if (!nextKey) {
      setMessage("A preview key is required to unlock audit mode.");
      return;
    }
    setPreviewKey(nextKey);
    setPreviewKeyState(nextKey);
    setMessage("Preview key saved locally for this browser session.");
  };

  const clearReviewKey = () => {
    clearPreviewKey();
    setPreviewKeyState("");
    setMessage("Local preview key cleared.");
  };

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

      <SectionCard title="1) Access Control" subtitle="How preview gates behave during review">
        <ul className="list-disc space-y-2 pl-5 text-sm text-ink/75">
          <li>Preview gating is enforced by middleware for most backend routes.</li>
          <li>OAuth callback routes are excluded from server-side preview checks.</li>
          <li>Frontend callback landing pages render on <code>/integrations?connected=...</code> or <code>/integrations?error=...</code>.</li>
        </ul>
      </SectionCard>

      <SectionCard title="2) Unlock Audit Session" subtitle="Unlock app flow without changing callback URLs">
        <form className="space-y-3" onSubmit={saveReviewKey}>
          <label className="block text-sm font-medium text-ink/85" htmlFor="previewKey">
            Preview key
          </label>
          <input
            id="previewKey"
            name="previewKey"
            type="password"
            required
            placeholder="Paste preview key for this environment"
            className="w-full rounded-panel border border-ink/20 px-3 py-2 text-sm outline-none ring-0 transition focus:border-ink/40"
          />
          <div className="flex flex-wrap gap-2">
            <button
              type="submit"
              className="rounded-full border border-ink/20 px-4 py-2 text-sm hover:bg-ink/5"
            >
              Save preview key
            </button>
            <button
              type="button"
              onClick={clearReviewKey}
              className="rounded-full border border-ink/20 px-4 py-2 text-sm hover:bg-rose-50"
            >
              Clear preview key
            </button>
          </div>
        </form>
        {message ? <p className="mt-3 rounded-panel border border-ink/10 bg-ink/5 p-2 text-sm text-ink/80">{message}</p> : null}
        {hasPreviewAccess ? (
          <p className="mt-3 rounded-panel border border-emerald-200 bg-emerald-50 p-3 text-sm text-emerald-900">
            Preview mode active for this browser.
          </p>
        ) : null}
      </SectionCard>

      <SectionCard title="3) Reviewer Checklist" subtitle="Required review evidence">
        <ul className="list-disc space-y-2 pl-5 text-sm text-ink/75">
          <li>Confirm policy matrix in `apps/orchestrator/tool_policy.py`.</li>
          <li>Confirm middleware exemptions in `config/middleware.py`.</li>
          <li>Confirm runtime auth contract and header behavior in `runtime/openclaw/plugins/nbhd-google-tools/index.js`.</li>
          <li>Confirm callback-result render path in `frontend/components/app-shell.tsx`.</li>
          <li>Run `apps/orchestrator/test_azure_client.py` and `apps/integrations` tests.</li>
        </ul>
        <div className="mt-4 rounded-panel border border-ink/10 bg-ink/5 p-3 text-xs">
          <p className="font-mono text-ink/75">Relevant audit artifact: `docs/stripe-audit-policies.md`.</p>
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
          If you want a temporary full walkthrough account, ask operations for a scoped reviewer onboarding link so you can reach authenticated areas.
        </p>
      </div>
    </div>
  );
}
