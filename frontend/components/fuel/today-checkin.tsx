"use client";

import { useMemo, useState } from "react";

import {
  useBodyWeightQuery,
  useCreateBodyWeightMutation,
  useCreateRestingHRMutation,
  useCreateSleepMutation,
  useRestingHRQuery,
  useSleepQuery,
  useUpdateBodyWeightMutation,
  useUpdateRestingHRMutation,
  useUpdateSleepMutation,
} from "@/lib/queries";
import { displayToKg, kgToDisplay, useWeightUnit } from "./use-weight-unit";

function todayIso() {
  return new Date().toISOString().slice(0, 10);
}

function formatDateLabel(iso: string) {
  const d = new Date(iso + "T00:00:00");
  return d.toLocaleDateString("en-US", { weekday: "short", month: "short", day: "numeric" });
}

export function TodayCheckIn() {
  const [logDate, setLogDate] = useState(todayIso);
  const [forceEdit, setForceEdit] = useState(false);
  const [showHR, setShowHR] = useState(false);
  const [dateOpen, setDateOpen] = useState(false);

  const { unit } = useWeightUnit();
  const { data: bwEntries, isPending: bwLoading } = useBodyWeightQuery();
  const { data: sleepEntries, isPending: sleepLoading } = useSleepQuery();
  const { data: hrEntries, isPending: hrLoading } = useRestingHRQuery();

  const createBW = useCreateBodyWeightMutation();
  const updateBW = useUpdateBodyWeightMutation();
  const createSleep = useCreateSleepMutation();
  const updateSleep = useUpdateSleepMutation();
  const createHR = useCreateRestingHRMutation();
  const updateHR = useUpdateRestingHRMutation();

  const todayBW = useMemo(() => bwEntries?.find((e) => e.date === logDate), [bwEntries, logDate]);
  const todaySleep = useMemo(() => sleepEntries?.find((e) => e.date === logDate), [sleepEntries, logDate]);
  const todayHR = useMemo(() => hrEntries?.find((e) => e.date === logDate), [hrEntries, logDate]);

  const isToday = logDate === todayIso();
  const anyLoading = bwLoading || sleepLoading || hrLoading;
  const allLogged = !!todayBW && !!todaySleep;
  const collapsed = !forceEdit && allLogged && isToday;

  // Form state — pre-populated from logged entries when present.
  const [weight, setWeight] = useState("");
  const [hours, setHours] = useState("");
  const [quality, setQuality] = useState<number | null>(null);
  const [bpm, setBpm] = useState("");

  // Render-phase sync: when the underlying entry (or display unit) changes,
  // reset the input to the entry's value. Cheaper than useEffect + setState
  // and avoids the cascading-render lint rule.
  const bwKey = todayBW ? `${todayBW.id}|${todayBW.weight_kg}|${unit}` : `none|${logDate}`;
  const [bwBaseline, setBwBaseline] = useState(bwKey);
  if (bwKey !== bwBaseline) {
    setBwBaseline(bwKey);
    setWeight(todayBW ? kgToDisplay(parseFloat(todayBW.weight_kg), unit).toFixed(1) : "");
  }

  const sleepKey = todaySleep
    ? `${todaySleep.id}|${todaySleep.duration_hours}|${todaySleep.quality ?? "_"}`
    : `none|${logDate}`;
  const [sleepBaseline, setSleepBaseline] = useState(sleepKey);
  if (sleepKey !== sleepBaseline) {
    setSleepBaseline(sleepKey);
    setHours(todaySleep ? parseFloat(todaySleep.duration_hours).toFixed(1) : "");
    setQuality(todaySleep?.quality ?? null);
  }

  const hrKey = todayHR ? `${todayHR.id}|${todayHR.bpm}` : `none|${logDate}`;
  const [hrBaseline, setHrBaseline] = useState(hrKey);
  if (hrKey !== hrBaseline) {
    setHrBaseline(hrKey);
    setBpm(todayHR ? String(todayHR.bpm) : "");
  }

  // HR row shown if logged today, explicitly opened, or has typed value.
  const hrVisible = showHR || !!todayHR || bpm.length > 0;

  const submitting =
    createBW.isPending ||
    updateBW.isPending ||
    createSleep.isPending ||
    updateSleep.isPending ||
    createHR.isPending ||
    updateHR.isPending;

  // Detect dirty state — only enable submit if user actually changed something.
  const weightDirty = (() => {
    if (!weight.trim()) return false;
    const parsed = parseFloat(weight);
    if (!Number.isFinite(parsed)) return false;
    if (!todayBW) return true;
    const currentKg = parseFloat(todayBW.weight_kg);
    const newKg = displayToKg(parsed, unit);
    return Math.abs(newKg - currentKg) >= 0.005;
  })();

  const hoursDirty = (() => {
    if (!hours.trim()) return false;
    const parsed = parseFloat(hours);
    if (!Number.isFinite(parsed)) return false;
    if (!todaySleep) return true;
    return Math.abs(parsed - parseFloat(todaySleep.duration_hours)) >= 0.05;
  })();

  const qualityDirty =
    !!todaySleep && quality !== (todaySleep.quality ?? null) && (hours.trim().length > 0);

  const bpmDirty = (() => {
    if (!bpm.trim()) return false;
    const parsed = parseInt(bpm, 10);
    if (!Number.isFinite(parsed)) return false;
    if (!todayHR) return true;
    return parsed !== todayHR.bpm;
  })();

  const canSubmit = weightDirty || hoursDirty || qualityDirty || bpmDirty;

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    const jobs: Promise<unknown>[] = [];

    if (weight.trim()) {
      const parsed = parseFloat(weight);
      if (Number.isFinite(parsed)) {
        const newKg = displayToKg(parsed, unit);
        if (todayBW) {
          if (Math.abs(newKg - parseFloat(todayBW.weight_kg)) >= 0.005) {
            jobs.push(updateBW.mutateAsync({ id: todayBW.id, data: { weight_kg: newKg } }));
          }
        } else {
          jobs.push(createBW.mutateAsync({ date: logDate, weight_kg: newKg }));
        }
      }
    }

    if (hours.trim()) {
      const parsed = parseFloat(hours);
      if (Number.isFinite(parsed)) {
        if (todaySleep) {
          const patch: { duration_hours?: number; quality?: number | null } = {};
          if (Math.abs(parsed - parseFloat(todaySleep.duration_hours)) >= 0.05) {
            patch.duration_hours = parsed;
          }
          if (quality !== (todaySleep.quality ?? null)) {
            patch.quality = quality;
          }
          if (Object.keys(patch).length > 0) {
            jobs.push(updateSleep.mutateAsync({ id: todaySleep.id, data: patch }));
          }
        } else {
          jobs.push(
            createSleep.mutateAsync({
              date: logDate,
              duration_hours: parsed,
              ...(quality ? { quality } : {}),
            }),
          );
        }
      }
    }

    if (bpm.trim()) {
      const parsed = parseInt(bpm, 10);
      if (Number.isFinite(parsed)) {
        if (todayHR) {
          if (parsed !== todayHR.bpm) {
            jobs.push(updateHR.mutateAsync({ id: todayHR.id, data: { bpm: parsed } }));
          }
        } else {
          jobs.push(createHR.mutateAsync({ date: logDate, bpm: parsed }));
        }
      }
    }

    if (jobs.length === 0) {
      setForceEdit(false);
      return;
    }

    const results = await Promise.allSettled(jobs);
    const allSucceeded = results.every((r) => r.status === "fulfilled");
    if (allSucceeded) {
      setForceEdit(false);
    }
    // On partial failure: keep the form expanded so the user can see which
    // fields still need saving. The global mutation onError toast already
    // signals the failure; leaving the form open lets the user retry.
  };

  // -- COLLAPSED STATE ---------------------------------------------------
  if (collapsed) {
    const w = todayBW ? kgToDisplay(parseFloat(todayBW.weight_kg), unit) : null;
    const s = todaySleep ? parseFloat(todaySleep.duration_hours) : null;
    return (
      <section
        aria-label="Today's check-in"
        className="rounded-panel border border-border bg-surface-elevated/60 px-4 py-3 mb-5 flex items-center justify-between gap-3"
      >
        <div className="flex items-center gap-2 min-w-0 flex-wrap">
          <span className="font-mono text-[10px] font-bold uppercase tracking-[0.18em] text-emerald-text bg-emerald-bg rounded-full px-2 py-1 shrink-0">
            Today ✓
          </span>
          <span className="text-sm text-ink">
            <span className="font-mono">{w?.toFixed(1)}</span>
            <span className="text-ink-faint"> {unit}</span>
          </span>
          <span className="text-ink-faint">·</span>
          <span className="text-sm text-ink">
            <span className="font-mono">{s?.toFixed(1)}</span>
            <span className="text-ink-faint">h</span>
            {todaySleep?.quality != null && (
              <>
                <span className="text-ink-faint"> · </span>
                <span className="font-mono">{"●".repeat(todaySleep.quality)}{"○".repeat(5 - todaySleep.quality)}</span>
              </>
            )}
          </span>
          {todayHR && (
            <>
              <span className="text-ink-faint">·</span>
              <span className="text-sm text-ink">
                <span className="font-mono">{todayHR.bpm}</span>
                <span className="text-ink-faint"> bpm</span>
              </span>
            </>
          )}
        </div>
        <button
          type="button"
          onClick={() => setForceEdit(true)}
          className="rounded-full min-h-[36px] px-3 text-xs font-semibold text-ink-muted hover:text-ink hover:bg-surface-hover transition shrink-0"
        >
          Edit
        </button>
      </section>
    );
  }

  // -- EXPANDED FORM -----------------------------------------------------
  return (
    <section
      aria-label="Daily check-in"
      className="rounded-panel border border-border bg-surface-elevated p-4 sm:p-5 mb-5 sm:mb-6 animate-reveal-1"
    >
      <header className="flex items-center justify-between mb-4">
        <div className="flex items-center gap-2 min-w-0">
          <span className="font-mono text-[10px] font-bold uppercase tracking-[0.2em] text-accent">
            Check-in
          </span>
          <span className="text-ink-faint text-[10px]">·</span>
          <span className="font-mono text-[10px] uppercase tracking-[0.14em] text-ink-muted">
            {isToday ? "Today" : formatDateLabel(logDate)}
          </span>
        </div>
        <div className="relative">
          <button
            type="button"
            onClick={() => setDateOpen((v) => !v)}
            aria-expanded={dateOpen}
            aria-controls="checkin-date"
            className="rounded-full min-h-[36px] px-3 text-[11px] font-mono uppercase tracking-wider text-ink-muted hover:text-ink hover:bg-surface-hover transition flex items-center gap-1.5"
          >
            <svg viewBox="0 0 24 24" className="h-3 w-3" fill="none" stroke="currentColor" strokeWidth="2" aria-hidden="true">
              <rect x="3" y="4" width="18" height="18" rx="2" />
              <path d="M16 2v4M8 2v4M3 10h18" />
            </svg>
            {isToday ? "Today" : formatDateLabel(logDate)}
          </button>
          {dateOpen && (
            <div className="absolute right-0 mt-1 z-10 rounded-panel border border-border bg-surface-elevated p-2 shadow-panel">
              <input
                id="checkin-date"
                type="date"
                value={logDate}
                max={todayIso()}
                onChange={(e) => {
                  setLogDate(e.target.value);
                  setDateOpen(false);
                }}
                className="rounded-lg border border-border bg-surface px-3 min-h-[40px] font-mono text-sm text-ink focus:outline-none focus:border-accent"
              />
            </div>
          )}
        </div>
      </header>

      <form onSubmit={handleSubmit} className="space-y-3">
        <div className="grid grid-cols-2 gap-3">
          {/* WEIGHT */}
          <FieldShell label="Weight" suffix={unit} logged={!!todayBW}>
            <input
              type="text"
              inputMode="decimal"
              value={weight}
              onChange={(e) => {
                if (/^\d*\.?\d*$/.test(e.target.value)) setWeight(e.target.value);
              }}
              aria-label={`Weight in ${unit}`}
              placeholder="—"
              className="w-full bg-transparent font-mono text-2xl text-ink focus:outline-none placeholder:text-ink-faint"
              autoComplete="off"
            />
          </FieldShell>

          {/* SLEEP */}
          <FieldShell label="Sleep" suffix="h" logged={!!todaySleep}>
            <input
              type="text"
              inputMode="decimal"
              value={hours}
              onChange={(e) => {
                if (/^\d*\.?\d*$/.test(e.target.value)) setHours(e.target.value);
              }}
              aria-label="Hours of sleep"
              placeholder="—"
              className="w-full bg-transparent font-mono text-2xl text-ink focus:outline-none placeholder:text-ink-faint"
              autoComplete="off"
            />
          </FieldShell>
        </div>

        {/* Quality dots — visible whenever there are sleep hours (typed or logged) */}
        {(hours.trim().length > 0 || todaySleep) && (
          <div className="flex items-center gap-2 pt-1">
            <span className="font-mono text-[10px] uppercase tracking-[0.14em] text-ink-faint w-12 shrink-0">
              Quality
            </span>
            <div className="flex gap-0.5">
              {[1, 2, 3, 4, 5].map((n) => {
                const active = quality != null && n <= quality;
                return (
                  <button
                    key={n}
                    type="button"
                    onClick={() => setQuality(quality === n ? null : n)}
                    aria-label={`Sleep quality ${n} of 5`}
                    aria-pressed={active}
                    className="inline-flex items-center justify-center min-h-[36px] min-w-[36px] rounded-full transition group"
                  >
                    <span
                      className={`block h-3.5 w-3.5 rounded-full border-2 transition ${
                        active
                          ? "bg-accent border-accent"
                          : "border-border group-hover:border-ink-muted"
                      }`}
                    />
                  </button>
                );
              })}
            </div>
            {quality != null && (
              <button
                type="button"
                onClick={() => setQuality(null)}
                className="ml-auto font-mono text-[10px] uppercase tracking-wider text-ink-faint hover:text-ink-muted transition"
              >
                Clear
              </button>
            )}
          </div>
        )}

        {/* HR — collapsed by default */}
        {hrVisible ? (
          <FieldShell label="Resting HR" suffix="bpm" logged={!!todayHR} compact>
            <input
              type="text"
              inputMode="numeric"
              value={bpm}
              onChange={(e) => {
                if (/^\d*$/.test(e.target.value)) setBpm(e.target.value);
              }}
              aria-label="Resting heart rate in bpm"
              placeholder="—"
              className="w-full bg-transparent font-mono text-xl text-ink focus:outline-none placeholder:text-ink-faint"
              autoComplete="off"
            />
          </FieldShell>
        ) : (
          <button
            type="button"
            onClick={() => setShowHR(true)}
            className="font-mono text-[11px] uppercase tracking-[0.14em] text-ink-faint hover:text-ink-muted transition"
          >
            + Resting HR
          </button>
        )}

        {/* Submit row */}
        <div className="pt-1 flex items-center justify-between gap-3">
          <p className="text-[11px] text-ink-faint min-w-0">
            {anyLoading
              ? "Loading today…"
              : todayBW || todaySleep || todayHR
                ? canSubmit
                  ? "Tap save to update."
                  : "Tap a field to edit."
                : "Tap a field to log."}
          </p>
          <div className="flex items-center gap-2 shrink-0">
            {forceEdit && allLogged && (
              <button
                type="button"
                onClick={() => setForceEdit(false)}
                className="rounded-full min-h-[40px] px-3 text-xs font-semibold text-ink-muted hover:text-ink hover:bg-surface-hover transition"
              >
                Done
              </button>
            )}
            <button
              type="submit"
              disabled={submitting || !canSubmit}
              className="glow-purple rounded-full bg-accent text-white min-h-[40px] px-5 text-sm font-semibold hover:brightness-110 active:scale-[0.98] transition disabled:opacity-40 disabled:cursor-not-allowed"
            >
              {submitting
                ? "Saving…"
                : todayBW || todaySleep || todayHR
                  ? "Save"
                  : "Log"}
            </button>
          </div>
        </div>
      </form>
    </section>
  );
}

function FieldShell({
  label,
  suffix,
  logged,
  compact,
  children,
}: {
  label: string;
  suffix: string;
  logged: boolean;
  compact?: boolean;
  children: React.ReactNode;
}) {
  return (
    <div
      className={`rounded-xl border bg-surface px-3 ${compact ? "py-2" : "py-2.5"} transition ${
        logged ? "border-emerald-text/40" : "border-border focus-within:border-accent"
      }`}
    >
      <div className="flex items-center justify-between gap-2">
        <span className="font-mono text-[10px] uppercase tracking-[0.16em] text-ink-faint">{label}</span>
        {logged ? (
          <span className="font-mono text-[10px] uppercase tracking-[0.14em] text-emerald-text">
            Logged ✓
          </span>
        ) : (
          <span className="font-mono text-[10px] text-ink-faint">{suffix}</span>
        )}
      </div>
      <div className="mt-0.5">{children}</div>
    </div>
  );
}
