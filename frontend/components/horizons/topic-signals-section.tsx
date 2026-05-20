import { HorizonsTopicSignal } from "@/lib/types";

import { HorizonsSection } from "./horizons-section";

function StatPill({ children }: { children: React.ReactNode }) {
  return (
    <span className="inline-flex items-center rounded-full border border-border bg-surface/60 px-2.5 py-1 text-xs text-ink-muted">
      {children}
    </span>
  );
}

function GoalPill() {
  return (
    <span className="inline-flex items-center rounded-full border border-signal-faint bg-signal-faint px-2.5 py-1 text-xs text-signal-text">
      Linked goal
    </span>
  );
}

function OverrideBadge({
  offset,
  scope,
}: {
  offset: number;
  scope: "topic" | "pillar";
}) {
  if (offset === 0) return null;
  const direction = offset > 0 ? "+1" : "-1";
  const label =
    offset > 0
      ? "you asked me to be more direct"
      : "you asked me to ease off";
  const scopeLabel = scope === "topic" ? "this topic" : "this pillar";
  return (
    <span className="inline-flex items-center rounded-full border border-accent/30 bg-accent/10 px-2.5 py-1 text-xs text-accent">
      {direction} on {scopeLabel} — {label}
    </span>
  );
}

function describeData(signal: HorizonsTopicSignal): string {
  const parts: string[] = [];
  if (signal.sample_size > 0) {
    parts.push(`${signal.sample_size}w of data`);
  } else {
    parts.push("no data yet");
  }
  if (signal.confirmed > 0 || signal.refuted > 0) {
    const responses: string[] = [];
    if (signal.confirmed > 0) responses.push(`confirmed ${signal.confirmed}`);
    if (signal.refuted > 0) responses.push(`corrected ${signal.refuted}`);
    parts.push(responses.join(", "));
  } else {
    parts.push("nothing confirmed yet");
  }
  return parts.join(" · ");
}

function TopicRow({ signal }: { signal: HorizonsTopicSignal }) {
  return (
    <li className="flex flex-col gap-2 border-t border-border py-4 first:border-t-0 first:pt-0">
      <div className="flex flex-wrap items-baseline gap-x-3 gap-y-1">
        <h3 className="font-headline text-base font-semibold text-ink">
          {signal.topic_display_name}
        </h3>
        <span className="font-mono text-[10px] uppercase tracking-[0.14em] text-ink-faint">
          {signal.pillar}
        </span>
      </div>
      <p className="text-sm text-ink-muted">{describeData(signal)}</p>
      <div className="flex flex-wrap gap-2">
        {signal.has_goal ? <GoalPill /> : null}
        {signal.register_offset !== 0 && signal.register_scope ? (
          <OverrideBadge
            offset={signal.register_offset}
            scope={signal.register_scope}
          />
        ) : null}
      </div>
    </li>
  );
}

export function TopicSignalsSection({
  signals,
  delay = 450,
}: {
  signals: HorizonsTopicSignal[];
  delay?: number;
}) {
  if (!signals || signals.length === 0) {
    return null;
  }
  return (
    <HorizonsSection
      title="Topics I've learned"
      subtitle="The meta-state behind my voice. To shift it, tell me — e.g. 'just be direct about dining' or 'ease off subscriptions'."
      delay={delay}
    >
      <ul className="flex flex-col">
        {signals.map((signal) => (
          <TopicRow key={`${signal.pillar}:${signal.topic_slug}`} signal={signal} />
        ))}
      </ul>
    </HorizonsSection>
  );
}

// Re-exported helper for tests.
export const __test = { describeData };
