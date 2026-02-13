"use client";

import { useState } from "react";

import { PersonaSelector } from "@/components/persona-selector";
import { SectionCard } from "@/components/section-card";
import { SectionCardSkeleton } from "@/components/skeleton";
import { StatusPill } from "@/components/status-pill";
import {
  useMeQuery,
  usePersonasQuery,
  usePreferencesQuery,
  useUpdatePreferencesMutation,
} from "@/lib/queries";

export default function SettingsPage() {
  const { data: me, isLoading } = useMeQuery();
  const { data: personas } = usePersonasQuery();
  const { data: prefs } = usePreferencesQuery();
  const updatePrefs = useUpdatePreferencesMutation();
  const [editing, setEditing] = useState(false);
  const [selected, setSelected] = useState("");

  const currentPersona = prefs?.agent_persona ?? "neighbor";
  const currentPersonaLabel = personas?.find((p) => p.key === currentPersona);

  const handleStartEdit = () => {
    setSelected(currentPersona);
    setEditing(true);
  };

  const handleSave = async () => {
    if (selected && selected !== currentPersona) {
      await updatePrefs.mutateAsync({ agent_persona: selected });
    }
    setEditing(false);
  };

  if (isLoading) {
    return (
      <div className="space-y-4">
        <SectionCardSkeleton lines={4} />
      </div>
    );
  }

  return (
    <div className="space-y-4">
      <SectionCard title="Account" subtitle="Your profile and authentication details">
        {me ? (
          <dl className="grid gap-3 text-sm sm:grid-cols-2">
            <div className="rounded-panel border border-ink/15 bg-white p-4">
              <dt className="font-mono text-[11px] uppercase tracking-[0.14em] text-ink/60">
                Display Name
              </dt>
              <dd className="mt-1 text-base font-medium text-ink">
                {me.display_name || "Not set"}
              </dd>
            </div>
            <div className="rounded-panel border border-ink/15 bg-white p-4">
              <dt className="font-mono text-[11px] uppercase tracking-[0.14em] text-ink/60">
                Email
              </dt>
              <dd className="mt-1 text-base text-ink">{me.email}</dd>
            </div>
            <div className="rounded-panel border border-ink/15 bg-white p-4">
              <dt className="font-mono text-[11px] uppercase tracking-[0.14em] text-ink/60">
                Username
              </dt>
              <dd className="mt-1 text-base text-ink">{me.username}</dd>
            </div>
            <div className="rounded-panel border border-ink/15 bg-white p-4">
              <dt className="font-mono text-[11px] uppercase tracking-[0.14em] text-ink/60">
                Language
              </dt>
              <dd className="mt-1 text-base text-ink">{me.language || "en"}</dd>
            </div>
            <div className="rounded-panel border border-ink/15 bg-white p-4">
              <dt className="font-mono text-[11px] uppercase tracking-[0.14em] text-ink/60">
                Telegram
              </dt>
              <dd className="mt-1">
                {me.telegram_username ? (
                  <span className="text-base text-ink">@{me.telegram_username}</span>
                ) : (
                  <StatusPill status="pending" />
                )}
              </dd>
            </div>
            <div className="rounded-panel border border-ink/15 bg-white p-4">
              <dt className="font-mono text-[11px] uppercase tracking-[0.14em] text-ink/60">
                Tenant
              </dt>
              <dd className="mt-1">
                {me.tenant ? (
                  <StatusPill status={me.tenant.status} />
                ) : (
                  <span className="text-sm text-ink/65">No tenant provisioned</span>
                )}
              </dd>
            </div>
          </dl>
        ) : (
          <p className="text-sm text-ink/70">Could not load account details.</p>
        )}
      </SectionCard>

      <SectionCard title="Agent Persona" subtitle="Your assistant's personality and communication style">
        {!editing ? (
          <div className="flex items-center justify-between">
            <div className="flex items-center gap-3">
              {currentPersonaLabel && (
                <>
                  <span className="text-2xl">{currentPersonaLabel.emoji}</span>
                  <div>
                    <p className="font-medium text-ink">{currentPersonaLabel.label}</p>
                    <p className="text-sm text-ink/60">{currentPersonaLabel.description}</p>
                  </div>
                </>
              )}
              {!currentPersonaLabel && (
                <p className="text-sm text-ink/60">No persona selected</p>
              )}
            </div>
            <button
              type="button"
              onClick={handleStartEdit}
              className="rounded-full border border-ink/15 px-4 py-1.5 text-sm text-ink/75 transition hover:border-ink/30 hover:text-ink"
            >
              Change
            </button>
          </div>
        ) : (
          <div className="space-y-4">
            {personas && (
              <PersonaSelector
                personas={personas}
                selected={selected}
                onSelect={setSelected}
              />
            )}
            <div className="flex items-center gap-3">
              <button
                type="button"
                onClick={handleSave}
                disabled={updatePrefs.isPending}
                className="rounded-full bg-accent px-5 py-2 text-sm font-medium text-white transition hover:bg-accent/85 disabled:opacity-55"
              >
                {updatePrefs.isPending ? "Saving..." : "Save"}
              </button>
              <button
                type="button"
                onClick={() => setEditing(false)}
                className="rounded-full border border-ink/15 px-4 py-2 text-sm text-ink/75 transition hover:border-ink/30"
              >
                Cancel
              </button>
            </div>
            <p className="text-xs text-ink/45">
              Changes take effect on the next container restart or reprovision.
            </p>
          </div>
        )}
      </SectionCard>
    </div>
  );
}
