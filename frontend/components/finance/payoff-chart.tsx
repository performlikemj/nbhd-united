"use client";

import {
  Area,
  AreaChart,
  CartesianGrid,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import { PayoffPlan } from "@/lib/types";
import { ChartTooltip } from "./chart-tooltip";

function formatShortCurrency(value: number): string {
  if (value >= 1000) return `$${(value / 1000).toFixed(0)}K`;
  return `$${value.toFixed(0)}`;
}

export function PayoffChart({ plan }: { plan: PayoffPlan }) {
  const schedule = plan.schedule_json ?? [];
  if (schedule.length === 0) return null;

  const data = schedule.map((entry) => ({
    month: `Mo ${entry.month}`,
    remaining: parseFloat(entry.total_remaining),
  }));

  return (
    <div
      className="h-[180px] sm:h-[240px]"
      role="img"
      aria-label={`Debt payoff timeline showing balance decreasing from ${formatShortCurrency(data[0]?.remaining ?? 0)} to $0 over ${plan.payoff_months} months using the ${plan.strategy} strategy`}
    >
      <ResponsiveContainer width="100%" height="100%">
        <AreaChart data={data} margin={{ top: 4, right: 4, bottom: 0, left: 0 }}>
          <defs>
            <linearGradient id="payoffGrad" x1="0" y1="0" x2="0" y2="1">
              <stop offset="0%" stopColor="var(--accent)" stopOpacity={0.2} />
              <stop offset="95%" stopColor="var(--accent)" stopOpacity={0.02} />
            </linearGradient>
          </defs>
          <CartesianGrid
            strokeDasharray="3 3"
            stroke="var(--border)"
            strokeOpacity={0.5}
          />
          <XAxis
            dataKey="month"
            tick={{ fontSize: 11, fill: "var(--ink-muted)" }}
            tickLine={false}
            axisLine={false}
            interval="preserveStartEnd"
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
                  name: "Remaining",
                  value: p.value as number,
                  color: "var(--accent)",
                }))}
                label={label != null ? String(label) : undefined}
                formatter={formatShortCurrency}
              />
            )}
          />
          <Area
            type="monotone"
            dataKey="remaining"
            stroke="var(--accent)"
            strokeWidth={2}
            fill="url(#payoffGrad)"
            animationDuration={600}
          />
        </AreaChart>
      </ResponsiveContainer>
    </div>
  );
}
