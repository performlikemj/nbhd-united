import { HorizonsTopicSignal } from "@/lib/types";

import { IconGoals } from "@/components/icons/constellation";

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

function isCalibrated(signal: HorizonsTopicSignal): boolean {
  return signal.sample_size >= 4 && signal.confirmed + signal.refuted >= 3;
}

function registerPreferenceLabel(signal: HorizonsTopicSignal): string | null {
  if (signal.register_offset === 0 || !signal.register_scope) return null;
  const direction = signal.register_offset > 0 ? "more direct" : "gentler";
  return `Your preference: ${direction} · this ${signal.register_scope}`;
}

function TopicChip({ signal }: { signal: HorizonsTopicSignal }) {
  const calibrated = isCalibrated(signal);
  const preference = registerPreferenceLabel(signal);

  return (
    <li
      className="inline-flex min-h-[44px] flex-col justify-center rounded-2xl border border-border bg-surface/60 px-3.5 py-2.5"
      title={describeData(signal)}
    >
      <div className="flex items-center gap-2.5">
        <h3 className="text-sm font-medium text-ink">
          {signal.topic_display_name}
        </h3>
        <span className="inline-flex items-center gap-1.5 text-[11px] text-ink-muted">
          <span
            className={`h-1.5 w-1.5 rounded-full ${calibrated ? "bg-signal" : "bg-ink-faint"}`}
            aria-hidden="true"
          />
          {calibrated ? "Calibrated" : "Learning"}
        </span>
        {signal.has_goal ? (
          <span className="text-signal-text" title="Linked goal">
            <IconGoals className="h-3.5 w-3.5" />
            <span className="sr-only">Linked goal</span>
          </span>
        ) : null}
      </div>
      {preference ? (
        <p className="mt-1 font-mono text-[10px] text-accent">{preference}</p>
      ) : null}
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
    <section
      className="animate-reveal min-w-0 motion-reduce:animate-none"
      style={{ animationDelay: `${delay}ms` }}
    >
      <header className="mb-4">
        <h2 className="font-headline text-xl font-medium tracking-tight text-ink">
          Topics I&apos;ve learned
        </h2>
        <p className="mt-1 max-w-2xl text-sm text-ink-muted">
          How my understanding and voice are taking shape.
        </p>
      </header>
      <div className="glass-card-horizons p-4 sm:p-5">
        <ul className="flex flex-wrap gap-2.5">
          {signals.map((signal) => (
            <TopicChip key={`${signal.pillar}:${signal.topic_slug}`} signal={signal} />
          ))}
        </ul>
      </div>
    </section>
  );
}

// Re-exported helper for tests.
export const __test = { describeData, isCalibrated, registerPreferenceLabel };
