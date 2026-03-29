"use client";

import {
  Area,
  AreaChart,
  CartesianGrid,
  Legend,
  Line,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import { FinanceSnapshot, PayoffPlan } from "@/lib/types";
import { ChartTooltip } from "./chart-tooltip";

function formatShortCurrency(value: number): string {
  if (value >= 1000) return `$${(value / 1000).toFixed(1)}K`;
  return `$${value.toFixed(0)}`;
}

function formatMonthLabel(dateStr: string): string {
  const d = new Date(dateStr + "T00:00:00");
  return d.toLocaleDateString("en-US", { month: "short", year: "2-digit" });
}

interface PayoffChartProps {
  plan: PayoffPlan;
  snapshots?: FinanceSnapshot[];
}

export function PayoffChart({ plan, snapshots = [] }: PayoffChartProps) {
  const schedule = plan.schedule_json ?? [];
  if (schedule.length === 0) return null;

  // Build the projected timeline from the plan's schedule
  const planCreated = new Date(plan.created_at);
  const projectedData = schedule.map((entry) => {
    const monthDate = new Date(planCreated);
    monthDate.setMonth(monthDate.getMonth() + entry.month - 1);
    monthDate.setDate(1);
    const key = `${monthDate.getFullYear()}-${String(monthDate.getMonth() + 1).padStart(2, "0")}-01`;
    return {
      date: key,
      label: formatMonthLabel(key),
      projected: parseFloat(entry.total_remaining),
    };
  });

  // Build a map of actual balances from snapshots
  const actualMap = new Map<string, number>();
  for (const snap of snapshots) {
    // Normalize to first of month
    const d = new Date(snap.date + "T00:00:00");
    const key = `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-01`;
    actualMap.set(key, parseFloat(snap.total_debt));
  }

  // Merge: use projected timeline as the backbone, overlay actual where available
  const data = projectedData.map((point) => ({
    ...point,
    actual: actualMap.has(point.date) ? actualMap.get(point.date) : undefined,
  }));

  // Also add any actual snapshots that are before the plan started (pre-plan history)
  const sortedSnapshots = [...snapshots]
    .sort((a, b) => new Date(a.date).getTime() - new Date(b.date).getTime());
  const planStartDate = projectedData[0]?.date;
  const preplanData = sortedSnapshots
    .filter((s) => {
      const d = new Date(s.date + "T00:00:00");
      const key = `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-01`;
      return key < planStartDate;
    })
    .map((s) => {
      const d = new Date(s.date + "T00:00:00");
      const key = `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-01`;
      return {
        date: key,
        label: formatMonthLabel(key),
        projected: undefined as number | undefined,
        actual: parseFloat(s.total_debt),
      };
    });

  const combined = [...preplanData, ...data];
  const hasActualData = combined.some((d) => d.actual !== undefined);

  // Thin out labels for readability — show ~6 labels max
  const labelInterval = Math.max(1, Math.floor(combined.length / 6));

  return (
    <div
      className="h-[200px] sm:h-[260px]"
      role="img"
      aria-label={`Debt payoff chart showing ${hasActualData ? "projected vs actual" : "projected"} balance over ${plan.payoff_months} months`}
    >
      <ResponsiveContainer width="100%" height="100%">
        <AreaChart data={combined} margin={{ top: 8, right: 8, bottom: 0, left: 0 }}>
          <defs>
            <linearGradient id="projectedGrad" x1="0" y1="0" x2="0" y2="1">
              <stop offset="0%" stopColor="#4ECDC4" stopOpacity={0.15} />
              <stop offset="95%" stopColor="#4ECDC4" stopOpacity={0.02} />
            </linearGradient>
            <linearGradient id="actualGrad" x1="0" y1="0" x2="0" y2="1">
              <stop offset="0%" stopColor="var(--accent)" stopOpacity={0.2} />
              <stop offset="95%" stopColor="var(--accent)" stopOpacity={0.02} />
            </linearGradient>
          </defs>
          <CartesianGrid
            strokeDasharray="3 3"
            stroke="var(--border)"
            strokeOpacity={0.3}
            vertical={false}
          />
          <XAxis
            dataKey="label"
            tick={{ fontSize: 10, fill: "var(--ink-faint)" }}
            tickLine={false}
            axisLine={false}
            interval={labelInterval}
          />
          <YAxis
            tickFormatter={formatShortCurrency}
            tick={{ fontSize: 10, fill: "var(--ink-faint)" }}
            tickLine={false}
            axisLine={false}
            width={44}
          />
          <Tooltip
            content={({ active, payload, label }) => (
              <ChartTooltip
                active={active}
                payload={payload
                  ?.filter((p) => p.value != null)
                  .map((p) => ({
                    name: p.dataKey === "projected" ? "Projected" : "Actual",
                    value: p.value as number,
                    color: p.dataKey === "projected" ? "#4ECDC4" : "var(--accent)",
                  }))}
                label={label != null ? String(label) : undefined}
                formatter={formatShortCurrency}
              />
            )}
          />
          {/* Projected line — dashed teal with fill */}
          <Area
            type="monotone"
            dataKey="projected"
            stroke="#4ECDC4"
            strokeWidth={2}
            strokeDasharray="6 3"
            fill="url(#projectedGrad)"
            animationDuration={600}
            connectNulls
            dot={false}
          />
          {/* Actual line — solid purple, only renders if we have snapshot data */}
          {hasActualData && (
            <Line
              type="monotone"
              dataKey="actual"
              stroke="var(--accent)"
              strokeWidth={2.5}
              animationDuration={800}
              connectNulls
              dot={{ r: 3, fill: "var(--accent)", stroke: "var(--bg)", strokeWidth: 2 }}
              activeDot={{ r: 5, fill: "var(--accent)", stroke: "white", strokeWidth: 2 }}
            />
          )}
          {hasActualData && (
            <Legend
              verticalAlign="top"
              align="right"
              iconSize={8}
              wrapperStyle={{ fontSize: 10, color: "var(--ink-faint)" }}
              formatter={(value: string) => (
                <span className="text-ink-faint text-[10px] ml-1">
                  {value === "projected" ? "Projected" : "Actual"}
                </span>
              )}
            />
          )}
        </AreaChart>
      </ResponsiveContainer>
    </div>
  );
}
