"use client";

import { useState } from "react";
import { MarkdownRenderer } from "@/components/markdown-renderer";
import type { NoteTemplateSection } from "@/lib/types";

interface DailyNoteSectionProps {
  section: NoteTemplateSection;
  onSave: (slug: string, content: string) => Promise<void>;
}

export function DailyNoteSection({ section, onSave }: DailyNoteSectionProps) {
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(section.content);
  const [saving, setSaving] = useState(false);

  const isAgent = section.source === "agent";
  const borderColor = isAgent ? "border-l-signal/30" : "border-l-accent/30";

  const handleSave = async () => {
    setSaving(true);
    try {
      await onSave(section.slug, draft);
      setEditing(false);
    } finally {
      setSaving(false);
    }
  };

  return (
    <section
      className={`rounded-panel border border-ink/10 border-l-4 ${borderColor} bg-white p-5`}
    >
      <div className="mb-3 flex items-center justify-between">
        <h3 className="text-lg font-semibold text-ink">{section.title}</h3>
        {!editing && (
          <button
            type="button"
            onClick={() => {
              setDraft(section.content);
              setEditing(true);
            }}
            className="text-sm text-ink/40 hover:text-ink/70"
          >
            Edit
          </button>
        )}
      </div>
      {editing ? (
        <div className="space-y-3">
          <textarea
            className="w-full rounded-panel border border-ink/15 bg-white px-3 py-2 font-mono text-sm"
            rows={Math.max(6, draft.split("\n").length + 2)}
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
          />
          <div className="flex gap-2">
            <button
              type="button"
              onClick={handleSave}
              disabled={saving}
              className="rounded-full bg-accent px-4 py-1.5 text-sm font-medium text-white transition hover:bg-accent/85 disabled:opacity-55"
            >
              {saving ? "Saving..." : "Save"}
            </button>
            <button
              type="button"
              onClick={() => setEditing(false)}
              className="rounded-full border border-ink/20 px-4 py-1.5 text-sm hover:border-ink/40"
            >
              Cancel
            </button>
          </div>
        </div>
      ) : section.content ? (
        <MarkdownRenderer content={section.content} />
      ) : (
        <p className="text-sm italic text-ink/40">No content yet.</p>
      )}
    </section>
  );
}
