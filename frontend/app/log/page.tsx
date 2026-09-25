"use client";

import clsx from "clsx";
import { useQueryClient } from "@tanstack/react-query";
import { useRouter } from "next/navigation";
import { useEffect, useMemo, useRef, useState } from "react";

import { OpenSkyPageHeader } from "@/components/open-sky/primitives";
import { createBodyWeight, createSleep, updateBodyWeight, updateSleep } from "@/lib/api";
import { getErrorMessage } from "@/lib/errors";
import { useBodyWeightQuery, useMeQuery, useSleepQuery, useTenantQuery } from "@/lib/queries";
import type { BodyWeightEntry, SleepEntry } from "@/lib/types";

type Range = 7 | 30 | 90;
type Field = "weight" | "sleep" | "quality" | "note";

interface Row {
  date: string;
  weight?: BodyWeightEntry;
  sleep?: SleepEntry;
}

function dayKey(date: Date, timeZone: string): string {
  return new Intl.DateTimeFormat("en-CA", { timeZone, year: "numeric", month: "2-digit", day: "2-digit" }).format(date);
}

function hoursLabel(h: number): string {
  const whole = Math.floor(h);
  const mins = Math.round((h - whole) * 60);
  return `${whole}h ${String(mins).padStart(2, "0")}m`;
}

function csvCell(v: string): string {
  return /[",\n]/.test(v) ? `"${v.replace(/"/g, '""')}"` : v;
}

/** One inline-editable cell: click → input with accent focus; Enter saves, Esc cancels. */
function EditableCell({
  value,
  display,
  label,
  inputMode = "decimal",
  onSave,
  error,
  align = "right",
}: {
  value: string;
  display: React.ReactNode;
  label: string;
  inputMode?: "decimal" | "numeric" | "text";
  onSave: (next: string) => void;
  error?: string;
  align?: "right" | "left";
}) {
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(value);
  const inputRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    if (editing) inputRef.current?.select();
  }, [editing]);

  if (editing) {
    return (
      <input
        ref={inputRef}
        aria-label={label}
        inputMode={inputMode}
        value={draft}
        onChange={(e) => setDraft(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === "Enter") {
            e.preventDefault();
            setEditing(false);
            if (draft.trim() !== value) onSave(draft.trim());
          } else if (e.key === "Escape") {
            e.preventDefault();
            setEditing(false);
            setDraft(value);
          }
        }}
        onBlur={() => {
          setEditing(false);
          setDraft(value);
        }}
        className={clsx(
          "os-num h-10 w-full rounded-lg border border-os-accent bg-transparent px-2 text-[0.9375rem] text-white outline-none",
          align === "right" ? "text-right" : "text-left",
        )}
      />
    );
  }
  return (
    <button
      type="button"
      onClick={() => {
        setDraft(value);
        setEditing(true);
      }}
      aria-label={`${label}: ${value || "empty"}. Edit`}
      className={clsx(
        "os-focus os-num flex h-10 w-full items-center rounded-lg px-2 text-[0.9375rem] hover:bg-os-accent-soft",
        align === "right" ? "justify-end" : "justify-start",
        error ? "text-os-danger" : "text-os-ink",
      )}
      title={error}
    >
      {display}
    </button>
  );
}

export default function BodyLogPage() {
  const router = useRouter();
  const qc = useQueryClient();
  const { data: tenant, isLoading: tenantLoading } = useTenantQuery();
  const { data: me } = useMeQuery();
  const sleepQuery = useSleepQuery();
  const weightQuery = useBodyWeightQuery();
  const [range, setRange] = useState<Range>(30);
  const [errors, setErrors] = useState<Record<string, string>>({});
  const [adding, setAdding] = useState(false);
  const [draft, setDraft] = useState({ date: "", weight: "", sleep: "" });
  const [addError, setAddError] = useState<string | null>(null);

  useEffect(() => {
    if (!tenantLoading && tenant && !tenant.web_redesign) router.replace("/fuel");
  }, [tenant, tenantLoading, router]);

  const timeZone = me?.timezone || Intl.DateTimeFormat().resolvedOptions().timeZone;
  const [now] = useState(() => new Date());
  const cutoff = dayKey(new Date(now.getTime() - (range - 1) * 86_400_000), timeZone);

  const rows: Row[] = useMemo(() => {
    const map = new Map<string, Row>();
    for (const w of weightQuery.data ?? []) {
      if (w.date < cutoff) continue;
      map.set(w.date, { ...(map.get(w.date) ?? { date: w.date }), weight: w });
    }
    for (const s of sleepQuery.data ?? []) {
      if (s.date < cutoff) continue;
      map.set(s.date, { ...(map.get(s.date) ?? { date: s.date }), sleep: s });
    }
    return [...map.values()].sort((a, b) => b.date.localeCompare(a.date));
  }, [weightQuery.data, sleepQuery.data, cutoff]);

  const sleepVals = rows.map((r) => (r.sleep ? Number(r.sleep.duration_hours) : null)).filter((v): v is number => v != null);
  const weightVals = rows.map((r) => (r.weight ? Number(r.weight.weight_kg) : null)).filter((v): v is number => v != null);
  const avgSleep = sleepVals.length ? sleepVals.reduce((a, b) => a + b, 0) / sleepVals.length : null;
  const avgWeight = weightVals.length ? weightVals.reduce((a, b) => a + b, 0) / weightVals.length : null;

  const cellKey = (date: string, field: Field) => `${date}:${field}`;

  /** Optimistic write with rollback + inline error. `apply` edits the cached list. */
  async function save<T extends { id: string }>(
    queryKey: string[],
    key: string,
    apply: (list: T[]) => T[],
    request: () => Promise<unknown>,
  ) {
    await qc.cancelQueries({ queryKey });
    const previous = qc.getQueryData<T[]>(queryKey);
    if (previous) qc.setQueryData<T[]>(queryKey, apply(previous));
    setErrors((e) => {
      const next = { ...e };
      delete next[key];
      return next;
    });
    try {
      await request();
    } catch (err) {
      if (previous) qc.setQueryData<T[]>(queryKey, previous);
      setErrors((e) => ({ ...e, [key]: getErrorMessage(err) }));
    } finally {
      void qc.invalidateQueries({ queryKey });
    }
  }

  function saveWeight(row: Row, text: string) {
    const kg = Number(text.replace(",", "."));
    const key = cellKey(row.date, "weight");
    if (!Number.isFinite(kg) || kg <= 0) {
      setErrors((e) => ({ ...e, [key]: "Enter a weight in kg, like 71.6" }));
      return;
    }
    if (row.weight) {
      const id = row.weight.id;
      void save<BodyWeightEntry>(["fuel-body-weight"], key, (l) => l.map((w) => (w.id === id ? { ...w, weight_kg: kg.toFixed(1) } : w)), () =>
        updateBodyWeight(id, { weight_kg: kg }),
      );
    } else {
      void save<BodyWeightEntry>(
        ["fuel-body-weight"],
        key,
        (l) => [{ id: `pending-${row.date}`, date: row.date, weight_kg: kg.toFixed(1), created_at: new Date().toISOString() }, ...l],
        () => createBodyWeight({ date: row.date, weight_kg: kg }),
      );
    }
  }

  function saveSleep(row: Row, field: "sleep" | "quality" | "note", text: string) {
    const key = cellKey(row.date, field);
    const data: { duration_hours?: number; quality?: number | null; notes?: string } = {};
    if (field === "sleep") {
      const h = Number(text.replace(",", "."));
      if (!Number.isFinite(h) || h <= 0 || h > 24) {
        setErrors((e) => ({ ...e, [key]: "Enter hours of sleep, like 7.5" }));
        return;
      }
      data.duration_hours = h;
    } else if (field === "quality") {
      const q = text === "" ? null : Number(text);
      if (q != null && (!Number.isInteger(q) || q < 1 || q > 5)) {
        setErrors((e) => ({ ...e, [key]: "Quality is 1 to 5" }));
        return;
      }
      data.quality = q;
    } else {
      data.notes = text;
    }
    if (row.sleep) {
      const id = row.sleep.id;
      void save<SleepEntry>(
        ["fuel-sleep"],
        key,
        (l) =>
          l.map((s) =>
            s.id === id
              ? {
                  ...s,
                  ...(data.duration_hours !== undefined ? { duration_hours: data.duration_hours.toFixed(2) } : {}),
                  ...(data.quality !== undefined ? { quality: data.quality } : {}),
                  ...(data.notes !== undefined ? { notes: data.notes } : {}),
                }
              : s,
          ),
        () => updateSleep(id, data),
      );
    } else if (data.duration_hours !== undefined) {
      const hours = data.duration_hours;
      void save<SleepEntry>(
        ["fuel-sleep"],
        key,
        (l) => [
          { id: `pending-${row.date}`, date: row.date, duration_hours: hours.toFixed(2), quality: null, notes: "", created_at: new Date().toISOString() },
          ...l,
        ],
        () => createSleep({ date: row.date, duration_hours: hours }),
      );
    } else {
      setErrors((e) => ({ ...e, [key]: "Add the hours of sleep first" }));
    }
  }

  async function addEntry() {
    setAddError(null);
    const date = draft.date || dayKey(now, timeZone);
    const kg = draft.weight ? Number(draft.weight.replace(",", ".")) : null;
    const h = draft.sleep ? Number(draft.sleep.replace(",", ".")) : null;
    if (kg == null && h == null) {
      setAddError("Add a weight, hours of sleep, or both.");
      return;
    }
    if ((kg != null && (!Number.isFinite(kg) || kg <= 0)) || (h != null && (!Number.isFinite(h) || h <= 0 || h > 24))) {
      setAddError("Check the numbers: weight in kg, sleep in hours.");
      return;
    }
    try {
      if (kg != null) await createBodyWeight({ date, weight_kg: kg });
      if (h != null) await createSleep({ date, duration_hours: h });
      setDraft({ date: "", weight: "", sleep: "" });
      setAdding(false);
    } catch (err) {
      setAddError(getErrorMessage(err));
    } finally {
      void qc.invalidateQueries({ queryKey: ["fuel-body-weight"] });
      void qc.invalidateQueries({ queryKey: ["fuel-sleep"] });
    }
  }

  function exportCsv() {
    const header = ["date", "weight_kg", "sleep_hours", "sleep_quality", "note"];
    const lines = rows.map((r) =>
      [
        r.date,
        r.weight ? Number(r.weight.weight_kg).toFixed(1) : "",
        r.sleep ? Number(r.sleep.duration_hours).toFixed(2) : "",
        r.sleep?.quality != null ? String(r.sleep.quality) : "",
        r.sleep?.notes ?? "",
      ]
        .map(csvCell)
        .join(","),
    );
    const blob = new Blob([[header.join(","), ...lines].join("\n")], { type: "text/csv" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = `nbhd-body-log-${dayKey(now, timeZone)}.csv`;
    a.click();
    URL.revokeObjectURL(url);
  }

  if (!tenant?.web_redesign) return null;

  const loading = sleepQuery.isLoading || weightQuery.isLoading;
  const dateLabel = (d: string) =>
    new Intl.DateTimeFormat("en-GB", { weekday: "short", day: "numeric", month: "short" }).format(new Date(`${d}T12:00:00`));

  return (
    <div className="space-y-8">
      <OpenSkyPageHeader
        eyebrow="Weight and sleep"
        title="Body log"
        actions={
          <>
            <button type="button" className="os-btn-text os-focus" onClick={exportCsv} disabled={rows.length === 0}>
              Export CSV
            </button>
            <button type="button" className="os-btn os-focus" onClick={() => setAdding((v) => !v)} aria-expanded={adding}>
              Add entry
            </button>
          </>
        }
      />

      <div className="flex flex-wrap items-end justify-between gap-6">
        <div role="radiogroup" aria-label="Range" className="flex gap-1 rounded-full border border-os-hairline p-1">
          {([7, 30, 90] as Range[]).map((r) => (
            <button
              key={r}
              type="button"
              role="radio"
              aria-checked={range === r}
              onClick={() => setRange(r)}
              className={clsx(
                "os-focus min-h-[36px] rounded-full px-4 text-[0.875rem]",
                range === r ? "bg-os-accent-soft text-white" : "text-os-muted hover:text-os-ink",
              )}
            >
              {r} days
            </button>
          ))}
        </div>
        <dl className="flex gap-10">
          <div>
            <dt className="os-label">Avg sleep</dt>
            <dd className="os-num mt-1 text-[1.5rem] font-light text-white">{avgSleep == null ? "—" : hoursLabel(avgSleep)}</dd>
          </div>
          <div>
            <dt className="os-label">Avg weight</dt>
            <dd className="os-num mt-1 text-[1.5rem] font-light text-white">{avgWeight == null ? "—" : `${avgWeight.toFixed(1)} kg`}</dd>
          </div>
          <div>
            <dt className="os-label">Entries</dt>
            <dd className="os-num mt-1 text-[1.5rem] font-light text-white">{rows.length}</dd>
          </div>
        </dl>
      </div>

      {adding ? (
        <form
          className="flex flex-wrap items-end gap-4 os-hairline-top pt-4"
          onSubmit={(e) => {
            e.preventDefault();
            void addEntry();
          }}
        >
          <label className="flex flex-col gap-1 text-[0.8125rem] text-os-muted">
            Date
            <input
              type="date"
              value={draft.date || dayKey(now, timeZone)}
              onChange={(e) => setDraft((d) => ({ ...d, date: e.target.value }))}
              className="os-focus h-11 rounded-lg border border-os-hairline bg-transparent px-3 text-[0.9375rem] text-os-ink [color-scheme:dark]"
            />
          </label>
          <label className="flex flex-col gap-1 text-[0.8125rem] text-os-muted">
            Weight (kg)
            <input
              inputMode="decimal"
              placeholder="71.6"
              value={draft.weight}
              onChange={(e) => setDraft((d) => ({ ...d, weight: e.target.value }))}
              className="os-focus os-num h-11 w-28 rounded-lg border border-os-hairline bg-transparent px-3 text-[0.9375rem] text-os-ink"
            />
          </label>
          <label className="flex flex-col gap-1 text-[0.8125rem] text-os-muted">
            Sleep (hours)
            <input
              inputMode="decimal"
              placeholder="7.5"
              value={draft.sleep}
              onChange={(e) => setDraft((d) => ({ ...d, sleep: e.target.value }))}
              className="os-focus os-num h-11 w-28 rounded-lg border border-os-hairline bg-transparent px-3 text-[0.9375rem] text-os-ink"
            />
          </label>
          <button type="submit" className="os-btn os-focus">
            Save
          </button>
          <button type="button" className="os-btn-text os-focus" onClick={() => setAdding(false)}>
            Cancel
          </button>
          {addError ? (
            <p role="alert" className="w-full text-[0.875rem] text-os-danger">
              {addError}
            </p>
          ) : null}
        </form>
      ) : null}

      {/* The editable table is one of the few true "objects" — it keeps a quiet surface. */}
      <div className="overflow-x-auto rounded-2xl border border-os-hairline bg-os-surface">
        <table className="w-full min-w-[560px] border-collapse text-left">
          <caption className="sr-only">Body log: weight and sleep by day. Select a value to edit it.</caption>
          <thead>
            <tr className="os-hairline-bottom">
              <th scope="col" className="os-label px-4 py-3 font-semibold">Date</th>
              <th scope="col" className="os-label px-2 py-3 text-right font-semibold">Weight</th>
              <th scope="col" className="os-label px-2 py-3 text-right font-semibold">Sleep</th>
              <th scope="col" className="os-label px-2 py-3 text-right font-semibold">Quality</th>
              <th scope="col" className="os-label px-4 py-3 font-semibold">Note</th>
            </tr>
          </thead>
          <tbody>
            {loading ? (
              <tr>
                <td colSpan={5} className="px-4 py-6 text-[0.9375rem] text-os-muted">Loading…</td>
              </tr>
            ) : rows.length === 0 ? (
              <tr>
                <td colSpan={5} className="px-4 py-8 text-[0.9375rem] text-os-muted">
                  Nothing logged in the last {range} days. Add an entry, or let your iPhone sync sleep from Apple Health.
                </td>
              </tr>
            ) : (
              rows.map((r) => {
                const wErr = errors[cellKey(r.date, "weight")];
                const sErr = errors[cellKey(r.date, "sleep")];
                const qErr = errors[cellKey(r.date, "quality")];
                const nErr = errors[cellKey(r.date, "note")];
                const rowError = wErr || sErr || qErr || nErr;
                return (
                  <tr key={r.date} className="border-t border-os-hairline align-middle">
                    <th scope="row" className="whitespace-nowrap px-4 py-1 text-[0.9375rem] font-normal text-os-ink">
                      {dateLabel(r.date)}
                      {rowError ? (
                        <span role="alert" className="block text-[0.8125rem] text-os-danger">
                          {rowError}
                        </span>
                      ) : null}
                    </th>
                    <td className="w-28 px-2 py-1">
                      <EditableCell
                        label={`Weight on ${dateLabel(r.date)}`}
                        value={r.weight ? Number(r.weight.weight_kg).toFixed(1) : ""}
                        display={r.weight ? `${Number(r.weight.weight_kg).toFixed(1)} kg` : <span className="text-os-faint">Add</span>}
                        error={wErr}
                        onSave={(v) => saveWeight(r, v)}
                      />
                    </td>
                    <td className="w-28 px-2 py-1">
                      <EditableCell
                        label={`Sleep hours on ${dateLabel(r.date)}`}
                        value={r.sleep ? Number(r.sleep.duration_hours).toFixed(2).replace(/\.?0+$/, "") : ""}
                        display={
                          r.sleep ? (
                            <span className={Number(r.sleep.duration_hours) < 7 ? "text-os-attn" : undefined}>
                              {hoursLabel(Number(r.sleep.duration_hours))}
                            </span>
                          ) : (
                            <span className="text-os-faint">Add</span>
                          )
                        }
                        error={sErr}
                        onSave={(v) => saveSleep(r, "sleep", v)}
                      />
                    </td>
                    <td className="w-24 px-2 py-1">
                      <EditableCell
                        label={`Sleep quality on ${dateLabel(r.date)}, 1 to 5`}
                        inputMode="numeric"
                        value={r.sleep?.quality != null ? String(r.sleep.quality) : ""}
                        display={r.sleep?.quality != null ? `${r.sleep.quality} / 5` : <span className="text-os-faint">—</span>}
                        error={qErr}
                        onSave={(v) => saveSleep(r, "quality", v)}
                      />
                    </td>
                    <td className="px-2 py-1 pr-4">
                      <EditableCell
                        label={`Note on ${dateLabel(r.date)}`}
                        inputMode="text"
                        align="left"
                        value={r.sleep?.notes ?? ""}
                        display={r.sleep?.notes ? <span className="truncate">{r.sleep.notes}</span> : <span className="text-os-faint">—</span>}
                        error={nErr}
                        onSave={(v) => saveSleep(r, "note", v)}
                      />
                    </td>
                  </tr>
                );
              })
            )}
          </tbody>
        </table>
      </div>
      <p className="text-[0.8125rem] text-os-faint">
        Select a value to edit it. Enter saves, Esc cancels. Changes show on your Overview straight away.
      </p>
    </div>
  );
}
