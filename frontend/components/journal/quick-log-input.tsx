"use client";

import { FormEvent, useState } from "react";

interface QuickLogInputProps {
  onSubmit: (content: string) => Promise<void>;
  isPending: boolean;
}

export function QuickLogInput({ onSubmit, isPending }: QuickLogInputProps) {
  const [content, setContent] = useState("");

  const handleSubmit = async (e: FormEvent) => {
    e.preventDefault();
    if (!content.trim()) return;
    await onSubmit(content.trim());
    setContent("");
  };

  return (
    <form onSubmit={handleSubmit} className="flex gap-2">
      <input
        type="text"
        placeholder="Quick log entry..."
        value={content}
        onChange={(e) => setContent(e.target.value)}
        className="min-h-[44px] flex-1 rounded-panel border border-ink/15 bg-white px-3 py-2 text-sm"
      />
      <button
        type="submit"
        disabled={isPending || !content.trim()}
        className="min-h-[44px] rounded-full bg-accent px-4 py-2 text-sm font-medium text-white transition hover:bg-accent/85 disabled:opacity-55"
      >
        {isPending ? "..." : "Log"}
      </button>
    </form>
  );
}
