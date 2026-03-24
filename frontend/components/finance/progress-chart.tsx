"use client";

import {
  Bar,
  BarChart,
  CartesianGrid,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import { FinanceSnapshot } from "@/lib/types";
import { ChartTooltip } from "./chart-tooltip";

function formatShortCurrency(value: number): string {
  if (value >= 1000) return `$${(value / 1000).toFixed(0)}K`;
  return `$${value.toFixed(0)}`;
}

function formatMonth(dateStr: string): string {
  const d = new Date(dateStr + "T00:00:00");
  return d.toLocaleDateString("en-US", { month: "short" });
}

export function ProgressChart({ snapshots }: { snapshots: FinanceSnapshot[] }) {
  if (snapshots.length < 2) return null;

  // Sort oldest first for left-to-right timeline
  const sorted = [...snapshots].sort(
    (a, b) => new Date(a.date).getTime() - new Date(b.date).getTime(),
  );

  const data = sorted.map((snap) => ({
    month: formatMonth(snap.date),
    debt: parseFloat(snap.total_debt),
    savings: parseFloat(snap.total_savings),
  }));

  return (
    <div
      className="h-[180px] sm:h-[220px]"
      role="img"
      aria-label={`Monthly progress chart showing debt and savings over ${data.length} months`}
    >
      <ResponsiveContainer width="100%" height="100%">
        <BarChart data={data} margin={{ top: 4, right: 4, bottom: 0, left: 0 }}>
          <CartesianGrid
            strokeDasharray="3 3"
            stroke="var(--border)"
            strokeOpacity={0.5}
            vertical={false}
          />
          <XAxis
            dataKey="month"
            tick={{ fontSize: 11, fill: "var(--ink-muted)" }}
            tickLine={false}
            axisLine={false}
          />
          <YAxis
            tickFormatter={formatShortCurrency}
            tick={{ fontSize: 11, fill: "var(--ink-muted)" }}
            tickLine={false}
            axisLine={false}
            width={48}
          />
          <Tooltip
            content={({ active, payload, label }) => (
              <ChartTooltip
                active={active}
                payload={payload?.map((p) => ({
                  name: p.name as string,
                  value: p.value as number,
                  color: p.name === "debt" ? "var(--rose-text)" : "var(--emerald-text)",
                }))}
                label={label != null ? String(label) : undefined}
                formatter={formatShortCurrency}
              />
            )}
          />
          <Bar
            dataKey="debt"
            name="Debt"
            fill="var(--rose-text)"
            radius={[4, 4, 0, 0]}
            animationDuration={500}
          />
          <Bar
            dataKey="savings"
            name="Savings"
            fill="var(--emerald-text)"
            radius={[4, 4, 0, 0]}
            animationDuration={500}
          />
        </BarChart>
      </ResponsiveContainer>
    </div>
  );
}
