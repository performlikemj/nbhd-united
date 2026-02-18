"use client";

import { useEffect, useMemo, useState } from "react";

type Frequency = "everyDay" | "weekdays" | "weekends" | "weekly" | "monthly";

type BuilderMode = "easy" | "advanced";

export interface ScheduleBuilderProps {
  expr: string;
  onChange: (expr: string) => void;
}

interface BuilderState {
  frequency: Frequency;
  hour: string;
  minute: string;
  weekdays: number[];
  monthDay: string;
}

interface ParsedCronResult {
  frequency: Frequency;
  hour: string;
  minute: string;
  weekdays: number[];
  monthDay: string;
}

const WEEKDAY_LABELS = [
  "Mon",
  "Tue",
  "Wed",
  "Thu",
  "Fri",
  "Sat",
  "Sun",
] as const;

const WEEKDAY_NAMES = [
  "Monday",
  "Tuesday",
  "Wednesday",
  "Thursday",
  "Friday",
  "Saturday",
  "Sunday",
] as const;

const DAY_OF_MONTH_LABELS = [
  "1st",
  "2nd",
  "3rd",
  "4th",
  "5th",
  "6th",
  "7th",
  "8th",
  "9th",
  "10th",
  "11th",
  "12th",
  "13th",
  "14th",
  "15th",
  "16th",
  "17th",
  "18th",
  "19th",
  "20th",
  "21st",
  "22nd",
  "23rd",
  "24th",
  "25th",
  "26th",
  "27th",
  "28th",
];

function pad2(value: number | string) {
  return String(value).padStart(2, "0");
}

function isValidMinute(value: string) {
  const parsed = Number(value);
  return Number.isInteger(parsed) && parsed >= 0 && parsed <= 59;
}

function isValidHour(value: string) {
  const parsed = Number(value);
  return Number.isInteger(parsed) && parsed >= 0 && parsed <= 23;
}

function clampRange(value: string, min: number, max: number) {
  const n = Number(value);
  if (!Number.isInteger(n)) return false;
  return n >= min && n <= max;
}

function monthDayOrdinal(day: number) {
  if (day % 10 === 1 && day !== 11) return `${day}st`;
  if (day % 10 === 2 && day !== 12) return `${day}nd`;
  if (day % 10 === 3 && day !== 13) return `${day}rd`;
  return `${day}th`;
}

function buildCronFromState(state: BuilderState) {
  const minute = pad2(state.minute);
  const hour = pad2(state.hour);

  switch (state.frequency) {
    case "everyDay":
      return `${minute} ${hour} * * *`;
    case "weekdays":
      return `${minute} ${hour} * * 1-5`;
    case "weekends":
      return `${minute} ${hour} * * 0,6`;
    case "weekly": {
      const sorted = [...state.weekdays].sort((a, b) => a - b);
      const days = sorted.length > 0 ? sorted.join(",") : "1";
      return `${minute} ${hour} * * ${days}`;
    }
    case "monthly":
      return `${minute} ${hour} ${state.monthDay} * *`;
    default:
      return `${minute} ${hour} * * *`;
  }
}

function parseCronExpr(expr: string): ParsedCronResult | null {
  const clean = expr.trim().split(/\s+/);
  if (clean.length !== 5) return null;

  const [minuteRaw, hourRaw, dayOfMonth, month, dayOfWeek] = clean;

  if (!isValidMinute(minuteRaw) || !isValidHour(hourRaw)) return null;
  const minute = String(Number(minuteRaw));
  const hour = String(Number(hourRaw));

  if (month !== "*") return null;

  if (dayOfMonth === "*" && dayOfWeek === "*") {
    return {
      frequency: "everyDay",
      hour,
      minute,
      weekdays: [],
      monthDay: "1",
    };
  }

  if (dayOfMonth === "*" && dayOfWeek === "1-5") {
    return {
      frequency: "weekdays",
      hour,
      minute,
      weekdays: [],
      monthDay: "1",
    };
  }

  if (dayOfMonth === "*" && dayOfWeek === "0,6") {
    return {
      frequency: "weekends",
      hour,
      minute,
      weekdays: [],
      monthDay: "1",
    };
  }

  const weekDays = dayOfWeek
    .split(",")
    .map((token) => Number(token));

  if (
    dayOfMonth === "*" &&
    weekDays.length > 0 &&
    weekDays.every((d) => Number.isInteger(d) && d >= 0 && d <= 6)
  ) {
    return {
      frequency: "weekly",
      hour,
      minute,
      weekdays: [...new Set(weekDays)].sort((a, b) => a - b),
      monthDay: "1",
    };
  }

  const domNum = Number(dayOfMonth);
  if (
    Number.isInteger(domNum) &&
    clampRange(dayOfMonth, 1, 28) &&
    dayOfWeek === "*"
  ) {
    return {
      frequency: "monthly",
      hour,
      minute,
      weekdays: [],
      monthDay: String(domNum),
    };
  }

  return null;
}

function formatHumanWeekdays(days: number[]) {
  const labels = days.map((d) => WEEKDAY_NAMES[d]);
  if (labels.length === 0) return "";
  if (labels.length === 1) return labels[0];
  if (labels.length === 2) return `${labels[0]} and ${labels[1]}`;
  return `${labels.slice(0, -1).join(", ")} and ${labels.at(-1)}`;
}

export function cronToHuman(expr: string, tz?: string): string {
  const parsed = parseCronExpr(expr);
  if (!parsed) return tz ? `Custom schedule (${tz})` : "Custom schedule";

  const time = `${pad2(parsed.hour)}:${pad2(parsed.minute)}`;

  let result = "";
  switch (parsed.frequency) {
    case "everyDay":
      result = `Every day at ${time}`;
      break;
    case "weekdays":
      result = `Every weekday at ${time}`;
      break;
    case "weekends":
      result = `Every weekend at ${time}`;
      break;
    case "weekly":
      result = `Every ${formatHumanWeekdays(parsed.weekdays)} at ${time}`;
      break;
    case "monthly":
      result = `Monthly on the ${monthDayOrdinal(Number(parsed.monthDay))} at ${time}`;
      break;
    default:
      result = `Custom schedule`;
      break;
  }

  return tz ? `${result} (${tz})` : result;
}

const defaultBuilderState: BuilderState = {
  frequency: "everyDay",
  hour: "9",
  minute: "0",
  weekdays: [1],
  monthDay: "1",
};

export default function ScheduleBuilder({ expr, onChange }: ScheduleBuilderProps) {
  const initialParsed = parseCronExpr(expr);
  const [mode, setMode] = useState<BuilderMode>(
    initialParsed ? "easy" : "advanced",
  );
  const [state, setState] = useState<BuilderState>(() => {
    return initialParsed
      ? {
          frequency: initialParsed.frequency,
          hour: pad2(initialParsed.hour),
          minute: pad2(initialParsed.minute),
          weekdays: initialParsed.weekdays,
          monthDay: initialParsed.monthDay,
        }
      : defaultBuilderState;
  });
  const [parseError, setParseError] = useState<string>(
    initialParsed ? "" : "This expression uses a custom pattern not supported by the schedule builder.",
  );

  useEffect(() => {
    if (mode === "easy") {
      const parsed = parseCronExpr(expr);
      if (parsed) {
        setState({
          frequency: parsed.frequency,
          hour: pad2(parsed.hour),
          minute: pad2(parsed.minute),
          weekdays: parsed.weekdays,
          monthDay: parsed.monthDay,
        });
        setParseError("");
      }
    }
  }, [expr, mode]);

  useEffect(() => {
    if (mode === "easy") {
      const exprFromState = buildCronFromState(state);
      if (exprFromState !== expr) {
        onChange(exprFromState);
      }
    }
  }, [expr, mode, onChange, state]);

  const humanLabel = useMemo(() => cronToHuman(expr), [expr]);
  const shownCron = mode === "easy" ? buildCronFromState(state) : expr || buildCronFromState(state);

  const handleModeToggle = () => {
    if (mode === "easy") {
      setMode("advanced");
      setParseError("");
      return;
    }

    const parsed = parseCronExpr(expr);
    if (!parsed) {
      setParseError(
        "This expression uses a custom pattern not supported by the schedule builder.",
      );
      return;
    }

    setState({
      frequency: parsed.frequency,
      hour: pad2(parsed.hour),
      minute: pad2(parsed.minute),
      weekdays: parsed.weekdays,
      monthDay: parsed.monthDay,
    });
    setMode("easy");
    setParseError("");
  };

  const handleFrequencyChange = (nextFrequency: Frequency) => {
    setState((prev) => ({
      ...prev,
      frequency: nextFrequency,
      weekdays: nextFrequency === "weekly" ? prev.weekdays : [1],
      monthDay: nextFrequency === "monthly" ? prev.monthDay : "1",
    }));
  };

  const timeOptionMinutes = ["00", "15", "30", "45"];
  const hourOptions = Array.from({ length: 24 }, (_, i) => pad2(i));

  return (
    <section className="space-y-2 md:col-span-2">
      <div className="flex items-center justify-between gap-3">
        <p className="text-sm font-medium text-ink-muted">Runs: {humanLabel}</p>
        <button
          type="button"
          onClick={handleModeToggle}
          className="text-xs text-accent hover:underline cursor-pointer"
        >
          {mode === "easy" ? "Use cron expression" : "Use schedule builder"}
        </button>
      </div>

      {parseError ? <p className="text-xs text-rose-text">{parseError}</p> : null}

      {mode === "easy" ? (
        <div className="grid gap-2 rounded-panel border border-border bg-surface p-2">
          <label className="text-sm text-ink-muted">
            Frequency
            <select
              className="mt-1 w-full rounded-panel border border-border bg-surface px-3 py-2 text-sm"
              value={state.frequency}
              onChange={(e) => handleFrequencyChange(e.target.value as Frequency)}
            >
              <option value="everyDay">Every day</option>
              <option value="weekdays">Weekdays (Mon–Fri)</option>
              <option value="weekends">Weekends (Sat–Sun)</option>
              <option value="weekly">Weekly</option>
              <option value="monthly">Monthly</option>
            </select>
          </label>

          {state.frequency === "weekly" ? (
            <div>
              <p className="mb-1 text-sm text-ink-muted">Days</p>
              <div className="flex flex-wrap gap-1.5">
                {WEEKDAY_LABELS.map((dayLabel, index) => {
                  const selected = state.weekdays.includes(index);
                  return (
                    <button
                      key={dayLabel}
                      type="button"
                      onClick={() =>
                        setState((prev) => {
                          const hasDay = prev.weekdays.includes(index);
                          const nextDays = hasDay
                            ? prev.weekdays.filter((day) => day !== index)
                            : [...prev.weekdays, index];
                          return {
                            ...prev,
                            weekdays: nextDays,
                          };
                        })
                      }
                      className={`rounded-full border px-2.5 py-1 text-xs ${
                        selected
                          ? "border-accent bg-accent text-white"
                          : "border-border bg-surface text-ink"
                      }`}
                    >
                      {dayLabel}
                    </button>
                  );
                })}
              </div>
            </div>
          ) : null}

          {state.frequency === "monthly" ? (
            <label className="text-sm text-ink-muted">
              Day of month
              <select
                className="mt-1 w-full rounded-panel border border-border bg-surface px-3 py-2 text-sm"
                value={state.monthDay}
                onChange={(e) => setState((prev) => ({ ...prev, monthDay: e.target.value }))}
              >
                {DAY_OF_MONTH_LABELS.map((label, index) => (
                  <option key={label} value={String(index + 1)}>
                    {label}
                  </option>
                ))}
              </select>
            </label>
          ) : null}

          <div className="grid gap-2 sm:grid-cols-2">
            <label className="text-sm text-ink-muted">
              Hour
              <select
                className="mt-1 w-full rounded-panel border border-border bg-surface px-3 py-2 text-sm"
                value={state.hour}
                onChange={(e) => setState((prev) => ({ ...prev, hour: e.target.value }))}
              >
                {hourOptions.map((hour) => (
                  <option key={hour} value={hour}>
                    {hour}
                  </option>
                ))}
              </select>
            </label>
            <label className="text-sm text-ink-muted">
              Minute
              <select
                className="mt-1 w-full rounded-panel border border-border bg-surface px-3 py-2 text-sm"
                value={state.minute}
                onChange={(e) => setState((prev) => ({ ...prev, minute: e.target.value }))}
              >
                {timeOptionMinutes.map((minute) => (
                  <option key={minute} value={minute}>
                    {minute}
                  </option>
                ))}
              </select>
            </label>
          </div>
        </div>
      ) : (
        <label className="text-sm text-ink-muted">
          Cron Expression
          <input
            className="mt-1 w-full rounded-panel border border-border bg-surface px-3 py-2 text-sm font-mono"
            value={expr}
            onChange={(event) => onChange(event.target.value)}
            required
          />
        </label>
      )}

      {mode === "advanced" && (
        <div className="inline-flex items-center rounded-full bg-surface-hover px-3 py-1.5 text-xs font-mono text-ink-muted">
          📋 Cron expression: {shownCron}
        </div>
      )}
    </section>
  );
}
