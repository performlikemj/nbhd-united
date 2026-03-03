"use client";

import { RefObject } from "react";
import { SERVICE_ICONS } from "@/components/service-icon";

/* ------------------------------------------------------------------ */
/*  Chip data                                                          */
/* ------------------------------------------------------------------ */

export interface CapabilityChip {
  id: string;
  icon: string;
  iconUrl?: string;
  tag: string;          // text inserted as [tag]
  group: "integration" | "skill" | "builtin";
  requiresConnection?: boolean;
  provider?: string;    // integration provider key for connection check
}

const INTEGRATION_CHIPS: CapabilityChip[] = [
  { id: "gmail", icon: "📧", iconUrl: SERVICE_ICONS["gmail"], tag: "Gmail", group: "integration", requiresConnection: true, provider: "gmail" },
  { id: "google-calendar", icon: "📅", iconUrl: SERVICE_ICONS["google-calendar"], tag: "Google Calendar", group: "integration", requiresConnection: true, provider: "google-calendar" },
  { id: "reddit", icon: "🔴", iconUrl: SERVICE_ICONS["reddit"], tag: "Reddit", group: "integration", requiresConnection: true, provider: "reddit" },
];

const SKILL_CHIPS: CapabilityChip[] = [
  { id: "daily-journal", icon: "📝", tag: "Daily Journal", group: "skill" },
  { id: "weekly-review", icon: "📊", tag: "Weekly Review", group: "skill" },
  { id: "pkm", icon: "🧠", tag: "PKM", group: "skill" },
];

const BUILTIN_CHIPS: CapabilityChip[] = [
  { id: "web-search", icon: "🌐", tag: "Web Search", group: "builtin" },
  { id: "weather", icon: "🌤️", tag: "Weather", group: "builtin" },
  { id: "news", icon: "📰", tag: "News", group: "builtin" },
  { id: "memory", icon: "💡", tag: "Memory", group: "builtin" },
];

const CHIP_GROUPS: { label: string; chips: CapabilityChip[] }[] = [
  { label: "Integrations", chips: INTEGRATION_CHIPS },
  { label: "Skills", chips: SKILL_CHIPS },
  { label: "Built-in", chips: BUILTIN_CHIPS },
];

/* ------------------------------------------------------------------ */
/*  Insert / toggle utility                                            */
/* ------------------------------------------------------------------ */

export function insertChipTag(
  textarea: HTMLTextAreaElement | null,
  tag: string,
  message: string,
  setMessage: (msg: string) => void,
) {
  const tagStr = `[${tag}]`;

  // If tag already exists, remove it (toggle off)
  if (message.includes(tagStr)) {
    setMessage(message.replaceAll(tagStr, "").replace(/ {2,}/g, " ").trim());
    return;
  }

  // If no textarea ref (shouldn't happen), just append
  if (!textarea) {
    const separator = message.trim() ? " " : "";
    setMessage(message.trim() + separator + tagStr);
    return;
  }

  // Insert at cursor position
  const start = textarea.selectionStart;
  const end = textarea.selectionEnd;
  const before = message.slice(0, start);
  const after = message.slice(end);
  const needsSpaceBefore = before.length > 0 && !before.endsWith(" ") && !before.endsWith("\n");
  const needsSpaceAfter = after.length > 0 && !after.startsWith(" ") && !after.startsWith("\n");
  const inserted = (needsSpaceBefore ? " " : "") + tagStr + (needsSpaceAfter ? " " : "");
  const newMessage = before + inserted + after;

  setMessage(newMessage);

  // Restore cursor after the inserted tag
  requestAnimationFrame(() => {
    const newPos = start + inserted.length;
    textarea.focus();
    textarea.setSelectionRange(newPos, newPos);
  });
}

/* ------------------------------------------------------------------ */
/*  Component                                                          */
/* ------------------------------------------------------------------ */

interface CapabilityChipsProps {
  message: string;
  textareaRef: RefObject<HTMLTextAreaElement | null>;
  onInsertTag: (tag: string) => void;
  connectedProviders: Set<string>;
}

export function CapabilityChips({
  message,
  textareaRef,
  onInsertTag,
  connectedProviders,
}: CapabilityChipsProps) {
  return (
    <div className="space-y-2">
      <p className="text-xs font-medium text-ink-muted">Add to prompt</p>

      {CHIP_GROUPS.map((group) => (
        <div key={group.label} className="flex flex-wrap items-center gap-2">
          <span className="text-xs text-ink-faint w-20 shrink-0">{group.label}</span>
          {group.chips.map((chip) => {
            const isActive = message.includes(`[${chip.tag}]`);
            const isDisconnected = chip.requiresConnection && !connectedProviders.has(chip.provider!);

            return (
              <button
                key={chip.id}
                type="button"
                disabled={isDisconnected}
                onClick={() => !isDisconnected && onInsertTag(chip.tag)}
                className={[
                  "inline-flex items-center gap-1.5 rounded-full border px-3 py-1.5 text-sm transition",
                  isDisconnected
                    ? "border-border text-ink-faint opacity-50 cursor-not-allowed"
                    : isActive
                      ? "border-accent bg-accent/10 text-accent"
                      : "border-border text-ink-muted hover:border-border-strong hover:text-ink",
                ].join(" ")}
                title={isDisconnected ? `Connect ${chip.tag} in Settings → Integrations` : chip.tag}
              >
                {chip.iconUrl ? (
                  // eslint-disable-next-line @next/next/no-img-element
                  <img
                    src={chip.iconUrl}
                    alt=""
                    className="h-4 w-4 rounded-sm object-contain"
                    onError={(e) => { (e.target as HTMLImageElement).style.display = "none"; }}
                  />
                ) : (
                  <span className="text-sm leading-none">{chip.icon}</span>
                )}
                {chip.tag}
                {isDisconnected && (
                  <a
                    href="/settings/integrations"
                    className="text-xs text-accent hover:underline"
                    onClick={(e) => e.stopPropagation()}
                  >
                    connect
                  </a>
                )}
              </button>
            );
          })}
        </div>
      ))}
    </div>
  );
}
