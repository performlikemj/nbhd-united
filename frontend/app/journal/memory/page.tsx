"use client";

import { useEffect, useState } from "react";

import { SectionCard } from "@/components/section-card";
import { SectionCardSkeleton } from "@/components/skeleton";
import { useMemoryQuery, useUpdateMemoryMutation } from "@/lib/queries";

function getErrorMessage(error: unknown): string {
  if (error instanceof Error) return error.message;
  return "Request failed.";
}

export default function MemoryPage() {
  const { data, isLoading, error } = useMemoryQuery();
  const updateMutation = useUpdateMemoryMutation();

  const [markdown, setMarkdown] = useState("");
  const [initialized, setInitialized] = useState(false);

  useEffect(() => {
    if (data && !initialized) {
      setMarkdown(data.markdown);
      setInitialized(true);
    }
  }, [data, initialized]);

  const isDirty = initialized && markdown !== (data?.markdown ?? "");

  const handleSave = async () => {
    await updateMutation.mutateAsync(markdown);
  };

  if (isLoading) {
    return <SectionCardSkeleton lines={8} />;
  }

  if (error) {
    return (
      <SectionCard title="Long-Term Memory">
        <p className="rounded-panel border border-rose-200 bg-rose-50 p-3 text-sm text-rose-900">
          Could not load memory.
        </p>
      </SectionCard>
    );
  }

  return (
    <SectionCard
      title="Long-Term Memory"
      subtitle="Markdown notes your agent remembers across conversations"
    >
      <textarea
        className="w-full rounded-panel border border-ink/15 bg-white px-4 py-3 font-mono text-sm leading-relaxed"
        rows={16}
        placeholder="Write anything your agent should remember long-term..."
        value={markdown}
        onChange={(e) => setMarkdown(e.target.value)}
      />

      <div className="mt-3 flex flex-wrap items-center gap-3">
        <button
          type="button"
          onClick={handleSave}
          disabled={updateMutation.isPending || !isDirty}
          className="rounded-full bg-accent px-5 py-2 text-sm font-medium text-white transition hover:bg-accent/85 disabled:opacity-55"
        >
          {updateMutation.isPending ? "Saving..." : "Save"}
        </button>

        {isDirty ? (
          <span className="text-xs text-amber-600">Unsaved changes</span>
        ) : null}

        {data?.updated_at ? (
          <span className="text-xs text-ink/45">
            Last saved{" "}
            {new Date(data.updated_at).toLocaleDateString(undefined, {
              month: "short",
              day: "numeric",
              hour: "numeric",
              minute: "2-digit",
            })}
          </span>
        ) : null}
      </div>

      {updateMutation.isError ? (
        <p className="mt-3 rounded-panel border border-rose-200 bg-rose-50 p-3 text-sm text-rose-900">
          {getErrorMessage(updateMutation.error)}
        </p>
      ) : null}
    </SectionCard>
  );
}
