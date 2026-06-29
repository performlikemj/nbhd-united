"use client";

/**
 * Read-only preview of a workout's detail_json, used in the orphan-draft
 * recovery dialog so MJ can verify what they're about to commit before
 * clicking "Save as a new workout".
 *
 * Why a dedicated file: workout-detail.tsx's editors are tightly coupled
 * to the editing state machine (per-set metric toggles, weight-unit
 * conversion, in-place reshape). Lifting their read-mode JSX wholesale
 * would either drag the whole edit machinery into the recovery panel or
 * force a deeper refactor that's out of scope for this fix.
 *
 * Tradeoff: there is now a parallel read-mode path. To keep them from
 * drifting, this file mirrors the storage shape (`detail_json` keys) that
 * workout-detail.tsx writes, never the editing internals. If the storage
 * shape changes, both files need an update.
 *
 * Compactness budget: at most ~6 visible top-level items. Long sessions
 * collapse to "+ N more" rather than turning the dialog into a scroll
 * trap. Per-set rows inside a single exercise/skill are also capped.
 */

import type { WorkoutCategory } from "@/lib/types";

import { kgToDisplay, useWeightUnit } from "./use-weight-unit";
import { elevationLabel, kmToDisplay, metersToDisplay, paceToDisplay, useDistanceUnit } from "./use-distance-unit";

const MAX_TOP_LEVEL_ITEMS = 6;
const MAX_SETS_PER_ITEM = 6;

type RawSet = {
  type?: "weighted_reps" | "bodyweight_reps" | "hold_time";
  reps?: number;
  weight?: number;
  hold_s?: number;
  pr?: boolean;
};

type SetMetric = "weighted_reps" | "bodyweight_reps" | "hold_time";

function setMetric(s: RawSet): SetMetric {
  if (s.type === "weighted_reps" || s.type === "bodyweight_reps" || s.type === "hold_time") {
    return s.type;
  }
  if (s.hold_s != null) return "hold_time";
  if (typeof s.weight === "number" && s.weight > 0) return "weighted_reps";
  return "bodyweight_reps";
}

interface WorkoutDetailReadOnlyProps {
  detail: Record<string, unknown> | null | undefined;
  category: WorkoutCategory | string;
}

/**
 * Top-level dispatcher. Falls through to a generic "no details captured"
 * line if the category renderer finds nothing useful — the orphan-draft
 * spec calls this out explicitly: never silently render an empty section.
 */
export function WorkoutDetailReadOnly({ detail, category }: WorkoutDetailReadOnlyProps) {
  const safeDetail = (detail ?? {}) as Record<string, unknown>;

  let body: React.ReactNode = null;
  switch (category) {
    case "strength":
      body = <StrengthReadOnly detail={safeDetail} />;
      break;
    case "cardio":
      body = <CardioStatsReadOnly detail={safeDetail} />;
      break;
    case "hiit":
      body = (
        <StatsReadOnly
          detail={safeDetail}
          fields={[
            ["rounds", "ROUNDS", ""],
            ["work_s", "WORK", "sec"],
            ["rest_s", "REST", "sec"],
            ["peak_hr", "HEART RATE · PEAK", "bpm"],
            ["avg_hr", "HEART RATE · AVG", "bpm"],
            ["calories", "KCAL", ""],
          ]}
        />
      );
      break;
    case "calisthenics":
      body = <CalisthenicsReadOnly detail={safeDetail} />;
      break;
    case "mobility":
      body = <MobilityReadOnly detail={safeDetail} />;
      break;
    default:
      // sport / other / unknown — render whatever shape happens to be
      // there. Most of these drafts only carry activity + duration, which
      // is already shown by the parent DraftSummary. Treat anything else
      // as freeform notes.
      body = <FreeformReadOnly detail={safeDetail} />;
  }

  return body ?? <EmptyDetails />;
}

function EmptyDetails() {
  return (
    <div className="rounded-lg border border-border border-dashed bg-surface-elevated/40 px-4 py-3 text-sm text-ink-faint">
      No exercise details captured in this draft.
    </div>
  );
}

function SectionHeader({ label }: { label: string }) {
  return (
    <div className="font-mono text-[10px] uppercase tracking-[0.2em] text-ink-faint">
      {label}
    </div>
  );
}

/* ---- Strength ---- */

function StrengthReadOnly({ detail }: { detail: Record<string, unknown> }) {
  const exercises =
    (detail.exercises as { name?: string; sets?: RawSet[] }[] | undefined) ?? [];
  const { unit } = useWeightUnit();
  const dw = (kg: number) => kgToDisplay(kg, unit);

  if (exercises.length === 0) return <EmptyDetails />;

  const visible = exercises.slice(0, MAX_TOP_LEVEL_ITEMS);
  const overflow = exercises.length - visible.length;

  return (
    <div className="space-y-2">
      <SectionHeader label="EXERCISES" />
      <ul className="space-y-2">
        {visible.map((ex, i) => {
          const sets = Array.isArray(ex.sets) ? ex.sets : [];
          const setsVisible = sets.slice(0, MAX_SETS_PER_ITEM);
          const setsOverflow = sets.length - setsVisible.length;
          return (
            <li
              key={i}
              className="rounded-lg border border-border bg-surface-elevated px-3 py-2.5"
            >
              <div className="flex items-baseline justify-between gap-2">
                <div className="text-sm font-medium text-ink truncate">
                  {ex.name?.trim() || `Exercise ${i + 1}`}
                </div>
                {sets.length > 0 && (
                  <div className="font-mono text-[10px] text-ink-faint shrink-0">
                    {sets.length} {sets.length === 1 ? "set" : "sets"}
                  </div>
                )}
              </div>
              {setsVisible.length > 0 && (
                <ul className="mt-1.5 space-y-0.5">
                  {setsVisible.map((s, j) => (
                    <li key={j} className="font-mono text-xs text-ink-muted">
                      <span className="text-ink-faint">{j + 1}.</span>{" "}
                      {formatStrengthSet(s, dw, unit)}
                      {s.pr && (
                        <span className="ml-1.5 text-[9px] font-bold uppercase tracking-wider text-accent">
                          PR
                        </span>
                      )}
                    </li>
                  ))}
                  {setsOverflow > 0 && (
                    <li className="font-mono text-[10px] text-ink-faint">
                      + {setsOverflow} more set{setsOverflow > 1 ? "s" : ""}
                    </li>
                  )}
                </ul>
              )}
            </li>
          );
        })}
      </ul>
      {overflow > 0 && (
        <div className="font-mono text-[10px] uppercase tracking-[0.15em] text-ink-faint">
          + {overflow} more exercise{overflow > 1 ? "s" : ""}
        </div>
      )}
    </div>
  );
}

function formatStrengthSet(
  s: RawSet,
  dw: (kg: number) => number,
  unit: string,
): string {
  const m = setMetric(s);
  if (m === "hold_time") {
    return `${s.hold_s ?? 0}s hold`;
  }
  if (m === "weighted_reps") {
    const reps = s.reps ?? 0;
    const weight = typeof s.weight === "number" ? s.weight : 0;
    const display = weight > 0 ? `${Math.round(dw(weight) * 10) / 10} ${unit}` : "BW";
    return `${reps} × ${display}`;
  }
  // bodyweight_reps
  return `${s.reps ?? 0} reps (BW)`;
}

/* ---- Calisthenics ---- */

function CalisthenicsReadOnly({ detail }: { detail: Record<string, unknown> }) {
  const skills =
    (detail.skills as { name?: string; sets?: RawSet[] }[] | undefined) ?? [];

  if (skills.length === 0) return <EmptyDetails />;

  const visible = skills.slice(0, MAX_TOP_LEVEL_ITEMS);
  const overflow = skills.length - visible.length;

  return (
    <div className="space-y-2">
      <SectionHeader label="SKILLS" />
      <ul className="space-y-2">
        {visible.map((sk, i) => {
          const sets = Array.isArray(sk.sets) ? sk.sets : [];
          const setsVisible = sets.slice(0, MAX_SETS_PER_ITEM);
          const setsOverflow = sets.length - setsVisible.length;
          return (
            <li
              key={i}
              className="rounded-lg border border-border bg-surface-elevated px-3 py-2.5"
            >
              <div className="flex items-baseline justify-between gap-2">
                <div className="text-sm font-medium text-ink truncate">
                  {sk.name?.trim() || `Skill ${i + 1}`}
                </div>
                {sets.length > 0 && (
                  <div className="font-mono text-[10px] text-ink-faint shrink-0">
                    {sets.length} {sets.length === 1 ? "set" : "sets"}
                  </div>
                )}
              </div>
              {setsVisible.length > 0 && (
                <ul className="mt-1.5 space-y-0.5">
                  {setsVisible.map((s, j) => (
                    <li key={j} className="font-mono text-xs text-ink-muted">
                      <span className="text-ink-faint">{j + 1}.</span>{" "}
                      {formatCalisthenicsSet(s)}
                      {s.pr && (
                        <span className="ml-1.5 text-[9px] font-bold uppercase tracking-wider text-accent">
                          PR
                        </span>
                      )}
                    </li>
                  ))}
                  {setsOverflow > 0 && (
                    <li className="font-mono text-[10px] text-ink-faint">
                      + {setsOverflow} more set{setsOverflow > 1 ? "s" : ""}
                    </li>
                  )}
                </ul>
              )}
            </li>
          );
        })}
      </ul>
      {overflow > 0 && (
        <div className="font-mono text-[10px] uppercase tracking-[0.15em] text-ink-faint">
          + {overflow} more skill{overflow > 1 ? "s" : ""}
        </div>
      )}
    </div>
  );
}

function formatCalisthenicsSet(s: RawSet): string {
  const m = setMetric(s);
  // Calisthenics never has weighted_reps in storage — the editor folds it
  // down to bodyweight_reps. Be defensive anyway in case a draft was
  // captured mid-flight before that reshape ran.
  if (m === "hold_time") {
    return `${s.hold_s ?? 0}s hold`;
  }
  return `${s.reps ?? 0} reps`;
}

/* ---- Cardio ---- */

function CardioStatsReadOnly({ detail }: { detail: Record<string, unknown> }) {
  const { unit } = useDistanceUnit();
  const elevUnit = elevationLabel(unit);

  const storedKm = typeof detail.distance_km === "number" ? (detail.distance_km as number) : null;
  const distanceDisplay = storedKm != null ? kmToDisplay(storedKm, unit) : null;

  const storedElevM = typeof detail.elevation === "number" ? (detail.elevation as number) : null;
  const elevationDisplay = storedElevM != null ? metersToDisplay(storedElevM, unit) : null;

  // Pace is stored canonical per-km; convert to the user's unit (not just relabel).
  const paceDisplay = paceToDisplay(typeof detail.pace === "string" ? detail.pace : null, unit);

  const avgHr = typeof detail.avg_hr === "number" ? (detail.avg_hr as number) : null;
  const avgPower = typeof detail.avg_power === "number" ? (detail.avg_power as number) : null;

  const rows: { label: string; value: string | number | null; unit: string }[] = [
    { label: "DISTANCE", value: distanceDisplay, unit },
    { label: "PACE", value: paceDisplay, unit: `/${unit}` },
    { label: "HEART RATE", value: avgHr, unit: "bpm" },
    { label: "ELEVATION", value: elevationDisplay, unit: elevUnit },
    { label: "POWER", value: avgPower, unit: "w" },
  ];

  const populated = rows.filter((r) => r.value != null && r.value !== "");
  if (populated.length === 0) return <EmptyDetails />;

  return (
    <div className="space-y-2">
      <SectionHeader label="STATS" />
      <ReadOnlyStatGrid rows={populated} />
    </div>
  );
}

/* ---- Generic stats grid (HIIT) ---- */

function StatsReadOnly({
  detail,
  fields,
}: {
  detail: Record<string, unknown>;
  fields: [string, string, string][];
}) {
  const rows = fields
    .map(([key, label, unit]) => {
      const raw = detail[key];
      const value =
        typeof raw === "number" || typeof raw === "string" ? raw : null;
      return { label, value, unit };
    })
    .filter((r) => r.value != null && r.value !== "");

  if (rows.length === 0) return <EmptyDetails />;

  return (
    <div className="space-y-2">
      <SectionHeader label="STATS" />
      <ReadOnlyStatGrid rows={rows} />
    </div>
  );
}

function ReadOnlyStatGrid({
  rows,
}: {
  rows: { label: string; value: string | number | null; unit: string }[];
}) {
  return (
    <div className="grid grid-cols-2 sm:grid-cols-3 gap-2">
      {rows.map((r) => (
        <div
          key={r.label}
          className="rounded-lg border border-border bg-surface-elevated px-3 py-2"
        >
          <div className="text-[8px] font-bold uppercase tracking-[0.2em] text-ink-faint">
            {r.label}
          </div>
          <div className="mt-0.5 font-mono text-sm text-ink">
            {String(r.value)}
            {r.unit && (
              <span className="ml-1 text-[10px] font-sans text-ink-faint">
                {r.unit}
              </span>
            )}
          </div>
        </div>
      ))}
    </div>
  );
}

/* ---- Mobility ---- */

function MobilityReadOnly({ detail }: { detail: Record<string, unknown> }) {
  const blocks = Array.isArray(detail.blocks)
    ? (detail.blocks as unknown[]).map((b) => String(b ?? "")).filter((b) => b.trim().length > 0)
    : [];

  if (blocks.length === 0) return <EmptyDetails />;

  const visible = blocks.slice(0, MAX_TOP_LEVEL_ITEMS * 2);
  const overflow = blocks.length - visible.length;

  return (
    <div className="space-y-2">
      <SectionHeader label="BLOCKS" />
      <div className="flex flex-wrap gap-1.5">
        {visible.map((b, i) => (
          <span
            key={i}
            className="rounded-md border border-border bg-surface-elevated px-2.5 py-1 text-xs text-ink-muted"
          >
            {b}
          </span>
        ))}
        {overflow > 0 && (
          <span className="font-mono text-[10px] uppercase tracking-[0.15em] text-ink-faint self-center">
            + {overflow} more
          </span>
        )}
      </div>
    </div>
  );
}

/* ---- Freeform (sport, other, unknown) ---- */

/**
 * Drafts from `sport` / `other` typically only carry the top-line activity
 * name + duration that the parent DraftSummary already shows. Surface any
 * other populated string/number leaves so a user-typed note in an
 * idiosyncratic key (`venue`, `partner`, `route`) doesn't disappear.
 */
function FreeformReadOnly({ detail }: { detail: Record<string, unknown> }) {
  const entries = Object.entries(detail).filter(([, v]) => {
    if (v == null) return false;
    if (typeof v === "string") return v.trim().length > 0;
    if (typeof v === "number") return Number.isFinite(v);
    if (typeof v === "boolean") return v;
    return false;
  });

  if (entries.length === 0) return <EmptyDetails />;

  const visible = entries.slice(0, MAX_TOP_LEVEL_ITEMS);
  const overflow = entries.length - visible.length;

  return (
    <div className="space-y-2">
      <SectionHeader label="DETAILS" />
      <dl className="rounded-lg border border-border bg-surface-elevated px-3 py-2 space-y-1">
        {visible.map(([k, v]) => (
          <div key={k} className="flex items-baseline gap-2 text-xs">
            <dt className="font-mono text-[10px] uppercase tracking-[0.15em] text-ink-faint shrink-0">
              {k.replace(/_/g, " ")}
            </dt>
            <dd className="font-mono text-ink-muted truncate">{String(v)}</dd>
          </div>
        ))}
        {overflow > 0 && (
          <div className="font-mono text-[10px] uppercase tracking-[0.15em] text-ink-faint">
            + {overflow} more
          </div>
        )}
      </dl>
    </div>
  );
}
