"use client";

import { useState } from "react";

import { SectionCard } from "@/components/section-card";
import { SectionCardSkeleton } from "@/components/skeleton";
import {
  useUpdateWorkingHoursMutation,
  useWorkingHoursQuery,
} from "@/lib/queries";

export function FeatureTipsSection() {
  const { data: wh, isLoading } = useWorkingHoursQuery();
  const updateWH = useUpdateWorkingHoursMutation();
  const [message, setMessage] = useState("");

  if (isLoading) {
    return <SectionCardSkeleton lines={1} />;
  }

  const enabled = wh?.feature_tips ?? true;

  const handleToggle = async () => {
    setMessage("");
    try {
      await updateWH.mutateAsync({ feature_tips: !enabled });
      setMessage(
        !enabled
          ? "Enabled! Your assistant will occasionally suggest features."
          : "Disabled. Your assistant won\u2019t suggest features unless you ask."
      );
      window.setTimeout(() => setMessage(""), 4000);
    } catch {
      setMessage("Failed to save. Please try again.");
      window.setTimeout(() => setMessage(""), 4000);
    }
  };

  return (
    <SectionCard
      title="Feature Tips"
      subtitle="Let your assistant suggest features you haven't tried yet"
      delay={200}
    >
      <div className="flex items-center justify-between gap-4">
        <p className="text-sm text-ink-muted">
          {enabled
            ? "Your assistant may suggest one new feature per week"
            : "Your assistant won\u2019t suggest features unless you ask"}
        </p>
        <button
          type="button"
          role="switch"
          aria-checked={enabled}
          aria-label={enabled ? "Disable Feature Tips" : "Enable Feature Tips"}
          onClick={handleToggle}
          disabled={updateWH.isPending}
          className={`relative inline-flex h-6 w-11 shrink-0 cursor-pointer rounded-full border-2 border-transparent transition-colors ${
            enabled ? "bg-accent" : "bg-border"
          } ${updateWH.isPending ? "opacity-50" : ""}`}
        >
          <span
            className={`pointer-events-none inline-block h-5 w-5 transform rounded-full bg-white shadow transition-transform ${
              enabled ? "translate-x-5" : "translate-x-0"
            }`}
          />
        </button>
      </div>

      {message && (
        <div
          className={`mt-3 rounded-panel border px-3 py-2 text-sm ${
            message.startsWith("Failed")
              ? "border-rose-border bg-rose-bg text-rose-text"
              : "border-signal/30 bg-signal-faint text-signal"
          }`}
        >
          {message}
        </div>
      )}
    </SectionCard>
  );
}
