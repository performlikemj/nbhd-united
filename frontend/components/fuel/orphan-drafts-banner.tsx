"use client";

import { useEffect, useState } from "react";

import { listOrphans, type OrphanDraft } from "@/lib/orphan-drafts";

import { OrphanRecoveryPanel } from "./orphan-recovery-panel";

interface OrphanDraftsBannerProps {
  tenantId: string | null | undefined;
}

/**
 * Top-of-Fuel-page banner: shows pending orphan drafts (saved-locally
 * work from a drawer that 404'd) and offers per-draft recovery.
 *
 * Subscribes to a `nbhd_fuel_orphan_drafts:updated` window event so the
 * banner re-renders whenever the drawer stashes a new draft in this
 * same tab. Cross-tab updates come in through the storage event.
 */
export function OrphanDraftsBanner({ tenantId }: OrphanDraftsBannerProps) {
  const [drafts, setDrafts] = useState<OrphanDraft[]>([]);
  const [openId, setOpenId] = useState<string | null>(null);

  useEffect(() => {
    if (!tenantId) return;

    const refresh = () => setDrafts(listOrphans(tenantId));
    refresh();

    const onStorage = (e: StorageEvent) => {
      if (e.key === "nbhd_fuel_orphan_drafts_v1") refresh();
    };
    const onLocal = () => refresh();
    window.addEventListener("storage", onStorage);
    window.addEventListener("nbhd:orphan-drafts-changed", onLocal);
    return () => {
      window.removeEventListener("storage", onStorage);
      window.removeEventListener("nbhd:orphan-drafts-changed", onLocal);
    };
  }, [tenantId]);

  if (!tenantId) return null;
  if (drafts.length === 0) return null;

  const top = drafts[0];
  const extras = drafts.length - 1;
  const openDraft = openId ? drafts.find((d) => d.stashId === openId) : null;

  return (
    <>
      <div
        role="status"
        aria-live="polite"
        className="rounded-panel border border-status-amber-border bg-status-amber-bg/30 backdrop-blur-md px-4 py-3 sm:px-5 sm:py-4"
      >
        <div className="flex items-start gap-3 flex-col sm:flex-row sm:items-center sm:justify-between">
          <div className="min-w-0">
            <div className="font-mono text-[10px] uppercase tracking-[0.22em] text-status-amber-text">
              Unsaved workout draft
            </div>
            <div className="mt-1 text-sm text-ink">
              We kept what you entered for <span className="font-semibold">{top.activity || "an untitled workout"}</span>
              {" "}on {top.date} — the original session was removed before it could save.
              {extras > 0 && <> Plus {extras} other draft{extras > 1 ? "s" : ""}.</>}
            </div>
          </div>
          <div className="flex items-center gap-2 shrink-0">
            <button
              type="button"
              onClick={() => setOpenId(top.stashId)}
              className="glow-purple rounded-full bg-accent px-4 py-2.5 text-sm font-semibold text-white transition hover:brightness-110 active:scale-[0.98] min-h-[44px]"
            >
              Recover
            </button>
          </div>
        </div>
        {extras > 0 && (
          <details className="mt-3 text-sm text-ink-muted">
            <summary className="cursor-pointer hover:text-ink">Show all {drafts.length} drafts</summary>
            <ul className="mt-2 space-y-1.5">
              {drafts.map((d) => (
                <li key={d.stashId} className="flex items-center justify-between gap-2">
                  <span className="truncate">
                    <span className="font-mono text-[10px] text-ink-faint">{d.date}</span>{" "}
                    <span className="text-ink">{d.activity || "Untitled"}</span>
                    <span className="text-ink-faint"> · {d.category}</span>
                  </span>
                  <button
                    type="button"
                    onClick={() => setOpenId(d.stashId)}
                    className="text-[11px] font-bold uppercase tracking-wider text-accent hover:text-ink transition shrink-0"
                  >
                    OPEN
                  </button>
                </li>
              ))}
            </ul>
          </details>
        )}
      </div>

      {openDraft && (
        <OrphanRecoveryPanel
          tenantId={tenantId}
          draft={openDraft}
          onClose={() => setOpenId(null)}
        />
      )}
    </>
  );
}
