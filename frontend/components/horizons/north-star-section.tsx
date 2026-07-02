"use client";

import { useState } from "react";

import { HorizonsSection } from "@/components/horizons/horizons-section";
import {
  useApproveExtractionMutation,
  useCreatePurposeMutation,
  useDismissExtractionMutation,
  useUpdatePurposeMutation,
} from "@/lib/queries";
import type { HorizonsNorthStar } from "@/lib/types";

// Design tokens only (see DESIGN.md): glass-card-horizons surface, accent
// action, ink text scale, 44px touch targets. No hardcoded hex.

function ConfirmedStar({ item }: { item: HorizonsNorthStar }) {
  return (
    <article className="glass-card-horizons border-l-2 border-l-accent p-5 md:p-6">
      <div className="flex items-start gap-3">
        <span className="text-xl text-accent" aria-hidden="true">
          {"✧"}
        </span>
        <div className="min-w-0 flex-1">
          <p className="text-base font-medium leading-relaxed text-ink">{item.statement}</p>
          {item.pillars.length > 0 ? (
            <p className="mt-2 font-mono text-[10px] uppercase tracking-wider text-ink-faint">
              {item.pillars.join(" · ")}
              {item.status === "evolving" ? " · evolving" : null}
            </p>
          ) : item.status === "evolving" ? (
            <p className="mt-2 font-mono text-[10px] uppercase tracking-wider text-ink-faint">evolving</p>
          ) : null}
        </div>
      </div>
    </article>
  );
}

function ProposedStar({ item }: { item: HorizonsNorthStar }) {
  const approveExtraction = useApproveExtractionMutation();
  const dismissExtraction = useDismissExtractionMutation();
  const updatePurpose = useUpdatePurposeMutation();
  const createPurpose = useCreatePurposeMutation();

  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(item.statement);
  const [resolved, setResolved] = useState<"confirmed" | "dismissed" | null>(null);

  const busy =
    approveExtraction.isPending ||
    dismissExtraction.isPending ||
    updatePurpose.isPending ||
    createPurpose.isPending;

  if (resolved === "dismissed") return null;
  if (resolved === "confirmed") {
    return (
      <article className="glass-card-horizons border-l-2 border-l-accent p-5 md:p-6">
        <p className="text-sm text-accent">Set as your North Star. {"✧"}</p>
      </article>
    );
  }

  const confirm = () => {
    if (item.source === "extraction") {
      approveExtraction.mutate(item.id, { onSuccess: () => setResolved("confirmed") });
    } else {
      updatePurpose.mutate(
        { id: item.id, patch: { status: "confirmed" } },
        { onSuccess: () => setResolved("confirmed") },
      );
    }
  };

  const notThis = () => {
    if (item.source === "extraction") {
      dismissExtraction.mutate(item.id, { onSuccess: () => setResolved("dismissed") });
    } else {
      updatePurpose.mutate(
        { id: item.id, patch: { status: "retired" } },
        { onSuccess: () => setResolved("dismissed") },
      );
    }
  };

  const saveEdit = () => {
    const next = draft.trim();
    if (!next) return;
    if (item.source === "extraction") {
      // No edit-before-approve endpoint for hypothesis cards: an edited
      // hypothesis becomes a user-authored (confirmed) purpose, and the
      // original card is dismissed.
      createPurpose.mutate(
        { statement: next, pillars: item.pillars },
        {
          onSuccess: () => {
            dismissExtraction.mutate(item.id);
            setResolved("confirmed");
          },
        },
      );
    } else {
      updatePurpose.mutate(
        { id: item.id, patch: { statement: next } },
        { onSuccess: () => setEditing(false) },
      );
    }
  };

  return (
    <article className="glass-card-horizons border-l-2 border-l-signal p-5 md:p-6">
      <div className="mb-3 flex items-center gap-2">
        <span className="rounded bg-signal/10 px-2 py-0.5 font-mono text-[10px] font-bold uppercase tracking-widest text-signal">
          Possible North Star
        </span>
      </div>

      {editing ? (
        <textarea
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          rows={3}
          className="w-full rounded-lg border border-border bg-surface p-3 text-sm text-ink focus:border-accent focus:outline-none"
          aria-label="Edit North Star statement"
        />
      ) : (
        <p className="text-base font-medium leading-relaxed text-ink">
          &ldquo;{item.statement}&rdquo;
        </p>
      )}

      {item.pillars.length > 0 && !editing ? (
        <p className="mt-2 font-mono text-[10px] uppercase tracking-wider text-ink-faint">
          {item.pillars.join(" · ")}
        </p>
      ) : null}

      <div className="mt-4 flex flex-wrap items-center gap-2">
        {editing ? (
          <>
            <button
              type="button"
              disabled={busy}
              onClick={saveEdit}
              className="min-h-[44px] rounded-full bg-accent px-4 py-1 text-xs font-semibold text-white transition hover:brightness-110 active:scale-95 disabled:opacity-50"
            >
              Save
            </button>
            <button
              type="button"
              disabled={busy}
              onClick={() => {
                setDraft(item.statement);
                setEditing(false);
              }}
              className="min-h-[44px] rounded-full border border-border px-4 py-1 text-xs font-medium text-ink-muted transition hover:bg-surface-hover disabled:opacity-50"
            >
              Cancel
            </button>
          </>
        ) : (
          <>
            <button
              type="button"
              disabled={busy}
              onClick={confirm}
              className="min-h-[44px] rounded-full bg-accent px-4 py-1 text-xs font-semibold text-white transition hover:brightness-110 active:scale-95 disabled:opacity-50"
            >
              Confirm
            </button>
            <button
              type="button"
              disabled={busy}
              onClick={() => setEditing(true)}
              className="min-h-[44px] rounded-full border border-border px-4 py-1 text-xs font-medium text-ink-muted transition hover:bg-surface-hover disabled:opacity-50"
            >
              Edit
            </button>
            <button
              type="button"
              disabled={busy}
              onClick={notThis}
              className="min-h-[44px] rounded-full border border-border px-4 py-1 text-xs font-medium text-ink-muted transition hover:bg-surface-hover disabled:opacity-50"
            >
              Not this
            </button>
          </>
        )}
      </div>
    </article>
  );
}

function AddYourOwn() {
  const createPurpose = useCreatePurposeMutation();
  const [open, setOpen] = useState(false);
  const [draft, setDraft] = useState("");

  const save = () => {
    const next = draft.trim();
    if (!next) return;
    createPurpose.mutate(
      { statement: next },
      {
        onSuccess: () => {
          setDraft("");
          setOpen(false);
        },
      },
    );
  };

  if (!open) {
    return (
      <button
        type="button"
        onClick={() => setOpen(true)}
        className="min-h-[44px] w-full rounded-lg border border-dashed border-border px-4 py-3 text-sm font-medium text-ink-muted transition hover:border-accent hover:text-ink"
      >
        + Add your own North Star
      </button>
    );
  }

  return (
    <div className="glass-card-horizons p-5 md:p-6">
      <textarea
        value={draft}
        onChange={(e) => setDraft(e.target.value)}
        rows={3}
        placeholder="Where do you want your life to go? (e.g. build a life where my work funds real time with the people I love)"
        className="w-full rounded-lg border border-border bg-surface p-3 text-sm text-ink placeholder:text-ink-faint focus:border-accent focus:outline-none"
        aria-label="New North Star statement"
      />
      <div className="mt-3 flex items-center gap-2">
        <button
          type="button"
          disabled={createPurpose.isPending || !draft.trim()}
          onClick={save}
          className="min-h-[44px] rounded-full bg-accent px-4 py-1 text-xs font-semibold text-white transition hover:brightness-110 active:scale-95 disabled:opacity-50"
        >
          {createPurpose.isPending ? "Saving..." : "Save"}
        </button>
        <button
          type="button"
          disabled={createPurpose.isPending}
          onClick={() => {
            setDraft("");
            setOpen(false);
          }}
          className="min-h-[44px] rounded-full border border-border px-4 py-1 text-xs font-medium text-ink-muted transition hover:bg-surface-hover disabled:opacity-50"
        >
          Cancel
        </button>
      </div>
    </div>
  );
}

export function NorthStarSection({
  items,
  delay = 0,
}: {
  items: HorizonsNorthStar[];
  delay?: number;
}) {
  // Retired purposes never reach the payload. Confirmed/evolving show as
  // statement cards; proposed (from either source) get Confirm / Edit / Not
  // this. "Add your own" is always available.
  const confirmed = items.filter((i) => i.status === "confirmed" || i.status === "evolving");
  const proposed = items.filter((i) => i.status === "proposed");

  return (
    <HorizonsSection
      title="North Star"
      subtitle="The direction above your goals — your why. Confirm one, add your own, or edit anytime."
      delay={delay}
    >
      <div className="space-y-4">
        {confirmed.map((item) => (
          <ConfirmedStar key={`${item.source}:${item.id}`} item={item} />
        ))}
        {proposed.map((item) => (
          <ProposedStar key={`${item.source}:${item.id}`} item={item} />
        ))}
        <AddYourOwn />
      </div>
    </HorizonsSection>
  );
}
