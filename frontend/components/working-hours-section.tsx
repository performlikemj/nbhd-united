"use client";

import { useState } from "react";

import { SectionCard } from "@/components/section-card";
import { SectionCardSkeleton } from "@/components/skeleton";
import {
  useUpdateWorkingHoursMutation,
  useWorkingHoursQuery,
} from "@/lib/queries";

const WORKING_HOUR_BLOCKS = [
  { label: "Morning", start: 6, range: "6:00 AM – 12:00 PM" },
  { label: "Daytime", start: 10, range: "10:00 AM – 4:00 PM" },
  { label: "Afternoon", start: 12, range: "12:00 PM – 6:00 PM" },
  { label: "Evening", start: 18, range: "6:00 PM – 12:00 AM" },
  { label: "Night", start: 0, range: "12:00 AM – 6:00 AM" },
] as const;

function blockForStartHour(hour: number) {
  return WORKING_HOUR_BLOCKS.find((b) => b.start === hour) ?? WORKING_HOUR_BLOCKS[0];
}

export function WorkingHoursSection({ timezone }: { timezone?: string }) {
  const { data: wh, isLoading } = useWorkingHoursQuery();
  const updateWH = useUpdateWorkingHoursMutation();

  const [editing, setEditing] = useState(false);
  const [enabled, setEnabled] = useState(true);
  const [startHour, setStartHour] = useState(6);
  const [message, setMessage] = useState("");

  const handleStartEdit = () => {
    if (wh) {
      setEnabled(wh.enabled);
      setStartHour(wh.start_hour);
    }
    setEditing(true);
  };

  const handleSave = async () => {
    setMessage("");
    try {
      await updateWH.mutateAsync({ enabled, start_hour: startHour });
      setEditing(false);
      setMessage("Saved! Changes take effect within 15 minutes.");
      window.setTimeout(() => setMessage(""), 4000);
    } catch {
      setMessage("Failed to save. Please try again.");
      window.setTimeout(() => setMessage(""), 4000);
    }
  };

  if (isLoading) {
    return <SectionCardSkeleton lines={2} />;
  }

  const currentBlock = wh ? blockForStartHour(wh.start_hour) : WORKING_HOUR_BLOCKS[0];

  return (
    <SectionCard
      title="Working Hours"
      subtitle="When your assistant can proactively reach out to you"
      delay={150}
    >
      {!editing ? (
        <div className="flex items-center justify-between">
          <div>
            <p className="text-base font-medium text-ink">
              {wh?.enabled ? currentBlock.range : "Off"}
            </p>
            {wh?.enabled && (
              <p className="mt-0.5 text-sm text-ink-muted">
                {currentBlock.label} · {timezone ?? "UTC"}
              </p>
            )}
            {!wh?.enabled && (
              <p className="mt-0.5 text-sm text-ink-muted">
                Your assistant won&apos;t message you proactively
              </p>
            )}
          </div>
          <button
            type="button"
            onClick={handleStartEdit}
            className="rounded-full border border-border px-4 py-1.5 text-sm text-ink-muted transition hover:border-border-strong hover:text-ink min-h-[44px]"
          >
            Edit
          </button>
        </div>
      ) : (
        <div className="space-y-4">
          <div className="flex items-center gap-3">
            <button
              type="button"
              onClick={() => setEnabled(true)}
              className={`rounded-full px-4 py-1.5 text-sm transition min-h-[44px] ${
                enabled
                  ? "bg-accent text-white"
                  : "border border-border text-ink-muted hover:border-border-strong"
              }`}
            >
              On
            </button>
            <button
              type="button"
              onClick={() => setEnabled(false)}
              className={`rounded-full px-4 py-1.5 text-sm transition min-h-[44px] ${
                !enabled
                  ? "bg-accent text-white"
                  : "border border-border text-ink-muted hover:border-border-strong"
              }`}
            >
              Off
            </button>
          </div>

          {enabled && (
            <div className="space-y-2">
              <p className="font-mono text-[11px] uppercase tracking-[0.14em] text-ink-muted">
                Time block
              </p>
              <div className="flex flex-wrap gap-2">
                {WORKING_HOUR_BLOCKS.map((block) => (
                  <button
                    key={block.start}
                    type="button"
                    onClick={() => setStartHour(block.start)}
                    className={`rounded-full px-4 py-2 text-sm transition min-h-[44px] ${
                      startHour === block.start
                        ? "bg-accent text-white"
                        : "border border-border text-ink-muted hover:border-border-strong hover:text-ink"
                    }`}
                  >
                    <span className="font-medium">{block.label}</span>
                    <span className="ml-1.5 opacity-75">{block.range}</span>
                  </button>
                ))}
              </div>
              {timezone && (
                <p className="text-xs text-ink-faint">Times are in {timezone}</p>
              )}
            </div>
          )}

          <div className="flex flex-wrap items-center gap-3">
            <button
              type="button"
              onClick={handleSave}
              disabled={updateWH.isPending}
              className="rounded-full bg-accent px-5 py-2 text-sm font-medium text-white transition hover:bg-accent/85 disabled:opacity-55 min-h-[44px]"
            >
              {updateWH.isPending ? "Saving..." : "Save"}
            </button>
            <button
              type="button"
              onClick={() => setEditing(false)}
              className="rounded-full border border-border px-4 py-1.5 text-sm transition hover:border-border-strong min-h-[44px]"
            >
              Cancel
            </button>
          </div>
        </div>
      )}

      {message && (
        <div className={`mt-3 rounded-panel border px-3 py-2 text-sm ${
          message.startsWith("Saved")
            ? "border-signal/30 bg-signal-faint text-signal"
            : "border-rose-border bg-rose-bg text-rose-text"
        }`}>
          {message}
        </div>
      )}
    </SectionCard>
  );
}
