"use client";

import { useMemo, useState } from "react";

interface TimezoneSelectorProps {
  value: string;
  onChange: (value: string) => void;
  defaultTimezone?: string;
  className?: string;
}

interface TimezoneGroup {
  region: string;
  options: string[];
}

function getSupportedTimezones(): string[] {
  if (typeof window === "undefined") {
    return ["UTC"];
  }

  try {
    const supported = Intl.supportedValuesOf("timeZone");
    if (!supported || supported.length === 0) {
      return ["UTC"];
    }
    return supported;
  } catch {
    return ["UTC"];
  }
}

function normalizeOffsetText(offsetMinutes: number) {
  const sign = offsetMinutes >= 0 ? "+" : "-";
  const absMinutes = Math.abs(offsetMinutes);
  const hours = Math.floor(absMinutes / 60);
  const minutes = absMinutes % 60;
  if (minutes === 0) {
    return `${sign}${hours}`;
  }
  return `${sign}${hours}:${String(minutes).padStart(2, "0")}`;
}

function getUtcOffsetLabel(timezone: string): string {
  try {
    const parts = new Intl.DateTimeFormat("en-US", {
      timeZone: timezone,
      timeZoneName: "longOffset",
    }).formatToParts(new Date());
    const token = parts.find((part) => part.type === "timeZoneName")?.value ?? "UTC";

    if (token === "GMT") {
      return "UTC+0";
    }

    const match = token.match(/GMT([+-])(\d{2})(?::(\d{2}))?/);
    if (match) {
      const sign = match[1] === "+" ? 1 : -1;
      const hours = Number.parseInt(match[2], 10);
      const minutes = Number.parseInt(match[3] ?? "0", 10);
      return `UTC${normalizeOffsetText(sign * (hours * 60 + minutes))}`;
    }
  } catch {
    // fallback
  }

  return "UTC";
}

function makeTimezoneGroups(timezones: string[]): TimezoneGroup[] {
  const grouped: Record<string, string[]> = {};
  for (const timezone of timezones) {
    const [region, ...rest] = timezone.split("/");
    const key = rest.length > 0 ? region : "Other";
    if (!grouped[key]) {
      grouped[key] = [];
    }
    grouped[key].push(timezone);
  }

  return Object.entries(grouped)
    .map(([region, options]) => ({
      region,
      options: options.sort(),
    }))
    .sort((a, b) => a.region.localeCompare(b.region));
}

export default function TimezoneSelector({
  value,
  onChange,
  defaultTimezone,
  className = "",
}: TimezoneSelectorProps) {
  const supportedTimezones = useMemo(() => getSupportedTimezones(), []);
  const [filter, setFilter] = useState("");

  const normalizedDefaultTimezone =
    value || defaultTimezone || Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC";

  const filteredTimezones = useMemo(() => {
    const normalized = filter.trim().toLowerCase();
    if (!normalized) {
      return supportedTimezones;
    }

    return supportedTimezones.filter((timezone) => {
      const needle = timezone.toLowerCase();
      return needle.includes(normalized) || getUtcOffsetLabel(timezone).toLowerCase().includes(normalized);
    });
  }, [filter, supportedTimezones]);

  const timezoneGroups = useMemo(() => makeTimezoneGroups(filteredTimezones), [filteredTimezones]);

  const currentValue = supportedTimezones.includes(value)
    ? value
    : normalizedDefaultTimezone || "UTC";

  return (
    <div className={`space-y-2 ${className}`.trim()}>
      <label className="text-sm text-ink/70">
        Search timezone
        <input
          type="text"
          value={filter}
          onChange={(event) => setFilter(event.target.value)}
          className="mt-1 w-full rounded-panel border border-ink/15 bg-white px-3 py-2 text-sm"
          placeholder="Type region, city, or UTC offset"
        />
      </label>

      <label className="text-sm text-ink/70">
        Timezone
        <select
          className="mt-1 w-full rounded-panel border border-ink/15 bg-white px-3 py-2 text-sm"
          value={currentValue}
          onChange={(event) => onChange(event.target.value)}
        >
          {timezoneGroups.length === 0 ? (
            <option value={currentValue}>No timezones match your search</option>
          ) : (
            timezoneGroups.map((group) => (
              <optgroup key={group.region} label={group.region}>
                {group.options.map((timezone) => (
                  <option key={timezone} value={timezone}>
                    {`${timezone} (${getUtcOffsetLabel(timezone)})`}
                  </option>
                ))}
              </optgroup>
            ))
          )}
        </select>
      </label>
    </div>
  );
}
