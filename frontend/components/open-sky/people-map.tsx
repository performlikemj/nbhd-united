"use client";

import { useMemo, useState } from "react";

import type { HomeNeighbor, NeighborBond } from "@/lib/types";

/*
 * "Your people": you at the center, your sky on the inner ring, everyone else
 * further out. Distance is the relationship you chose, never activity. Lines
 * run only from you to each person; their width is a 3-step bucket, never a
 * number. When the outer crowd is large the others become quiet dots on 2–3
 * rings with names on hover/focus/search, and the A–Z list carries the load.
 */

/** Past this many non-sky neighbors, the outer ring switches to quiet dots. */
export const QUIET_AFTER = 16;

const INNER = 0.22;
const OUTER = 0.39; // the single labelled outer ring (small neighborhoods)
const QUIET_RINGS = [0.33, 0.405, 0.475];
// Phones in a crowd show only your sky, so it gets the room the outer rings had.
const PHONE_SKY = 0.29;
const PHONE_HINT = 0.45;

const BOND_WIDTH: Record<NeighborBond, number> = { light: 0.8, steady: 1.6, strong: 2.6 };
const BOND_ALPHA: Record<NeighborBond, number> = { light: 0.32, steady: 0.48, strong: 0.66 };
// Outside your sky in a crowd: same three steps, scaled down and quieter.
const QUIET_WIDTH: Record<NeighborBond, number> = { light: 0.4, steady: 0.7, strong: 1.1 };
const QUIET_ALPHA: Record<NeighborBond, number> = { light: 0.1, steady: 0.16, strong: 0.24 };

export function byName(a: HomeNeighbor, b: HomeNeighbor) {
  return a.display_name.localeCompare(b.display_name) || a.friendship_id.localeCompare(b.friendship_id);
}

export function sinceLabel(n: HomeNeighbor) {
  return n.in_my_sky ? "in your sky" : `friends since ${n.friends_since.slice(0, 4)}`;
}

type Spot = { n: HomeNeighbor; a: number; x: number; y: number; quiet: boolean };

function angles(count: number, turn: number) {
  return Array.from({ length: count }, (_, i) => -Math.PI / 2 + turn + (i / Math.max(count, 1)) * Math.PI * 2);
}

/** Turn a ring so its spokes stay as far as possible from the inner spokes. */
function clearTurn(count: number, inner: number[]) {
  const step = (Math.PI * 2) / Math.max(count, 1);
  let best = step / 2;
  let bestGap = -1;
  for (let k = 0; k < 24; k++) {
    const turn = (k / 24) * step;
    const gap = Math.min(
      Math.PI,
      ...angles(count, turn).flatMap((a) => inner.map((b) => Math.abs(((a - b + Math.PI * 3) % (Math.PI * 2)) - Math.PI))),
    );
    if (gap > bestGap + 1e-6) {
      bestGap = gap;
      best = turn;
    }
  }
  return best;
}

function place(neighbors: HomeNeighbor[]): { spots: Spot[]; quiet: boolean; rings: number[] } {
  const inner = neighbors.filter((n) => n.in_my_sky).sort(byName);
  const outer = neighbors.filter((n) => !n.in_my_sky).sort(byName);
  const innerA = angles(inner.length, 0);
  const at = (n: HomeNeighbor, a: number, r: number, quiet: boolean): Spot => ({
    n,
    a,
    x: 0.5 + Math.cos(a) * r,
    y: 0.5 + Math.sin(a) * r,
    quiet,
  });
  const spots = inner.map((n, i) => at(n, innerA[i], INNER, false));
  if (outer.length <= QUIET_AFTER) {
    const oa = angles(outer.length, clearTurn(outer.length, innerA));
    return { spots: [...spots, ...outer.map((n, i) => at(n, oa[i], OUTER, false))], quiet: false, rings: [INNER, OUTER] };
  }
  // Quiet crowd: 2 rings up to 40 people, else 3; each ring holds a share
  // proportional to its length, filled in name order.
  const rings = outer.length <= 40 ? QUIET_RINGS.slice(1) : QUIET_RINGS;
  const total = rings.reduce((a, r) => a + r, 0);
  let start = 0;
  rings.forEach((r, k) => {
    const take = k === rings.length - 1 ? outer.length - start : Math.round((outer.length * r) / total);
    const group = outer.slice(start, start + take);
    start += take;
    // Offset each ring by a fraction of a step so dots on neighbouring rings don't line up.
    const step = (Math.PI * 2) / Math.max(group.length, 1);
    const oa = angles(group.length, step * (0.5 + 0.33 * k));
    group.forEach((n, i) => spots.push(at(n, oa[i], r, true)));
  });
  return { spots, quiet: true, rings: [INNER, ...rings] };
}

/** Where the name sits relative to its dot: outward, so the spoke never crosses it. */
function labelSide(a: number): "left" | "right" | "above" | "below" {
  const c = Math.cos(a), s = Math.sin(a);
  if (Math.abs(c) > 0.45) return c > 0 ? "right" : "left";
  return s < 0 ? "above" : "below";
}

const SIDE_CLASS: Record<ReturnType<typeof labelSide>, string> = {
  right: "left-full top-1/2 -translate-y-1/2 pl-1 items-start text-left",
  left: "right-full top-1/2 -translate-y-1/2 pr-1 items-end text-right",
  above: "bottom-full left-1/2 -translate-x-1/2 pb-0.5 items-center text-center",
  below: "top-full left-1/2 -translate-x-1/2 pt-0.5 items-center text-center",
};

export function PeopleMap({
  neighbors,
  selectedId,
  matchIds,
  onSelect,
}: {
  neighbors: HomeNeighbor[];
  selectedId: string | null;
  matchIds: Set<string>;
  onSelect: (id: string) => void;
}) {
  const { spots, quiet, rings } = useMemo(() => place(neighbors), [neighbors]);
  const others = spots.filter((s) => s.quiet).length;

  return (
    <div
      className={`relative ${quiet ? "" : "mx-auto w-full"} ${quiet ? "-mx-4 aspect-square w-[calc(100%+2rem)] max-w-[760px] [--sky-r:29%] sm:mx-auto sm:w-full sm:[--sky-r:22%]" : "aspect-[4/5] max-w-[640px] sm:aspect-square"}`}
    >
      <svg viewBox="0 0 100 100" preserveAspectRatio="none" className="absolute inset-0 h-full w-full" aria-hidden="true">
        {quiet ? (
          <>
            <ellipse cx="50" cy="50" rx={PHONE_SKY * 100} ry={PHONE_SKY * 100} fill="none" stroke="var(--os-hairline)" strokeWidth="1" strokeDasharray="2 5" vectorEffect="non-scaling-stroke" className="sm:hidden" />
            <ellipse cx="50" cy="50" rx={PHONE_HINT * 100} ry={PHONE_HINT * 100} fill="none" stroke="var(--os-hairline)" strokeWidth="1" strokeDasharray="1 7" vectorEffect="non-scaling-stroke" className="sm:hidden" />
          </>
        ) : null}
        {rings.map((r, k) => (
          <ellipse
            key={r}
            cx="50"
            cy="50"
            rx={r * 100}
            ry={r * 100}
            fill="none"
            stroke="var(--os-hairline)"
            strokeWidth="1"
            strokeDasharray="2 5"
            vectorEffect="non-scaling-stroke"
            className={quiet ? "hidden sm:inline" : undefined}
          />
        ))}
        {quiet
          ? spots
              .filter((sp) => !sp.quiet)
              .map(({ n, a }) => (
                <line
                  key={`phone-${n.friendship_id}`}
                  x1="50"
                  y1="50"
                  x2={50 + Math.cos(a) * PHONE_SKY * 100}
                  y2={50 + Math.sin(a) * PHONE_SKY * 100}
                  stroke={n.friendship_id === selectedId || matchIds.has(n.friendship_id) ? "var(--os-accent)" : `rgba(226,232,240,${BOND_ALPHA[n.bond]})`}
                  strokeWidth={BOND_WIDTH[n.bond]}
                  strokeLinecap="round"
                  vectorEffect="non-scaling-stroke"
                  className="sm:hidden"
                />
              ))
          : null}
        {spots.map(({ n, x, y, quiet: q }) => {
          const on = n.friendship_id === selectedId;
          const hit = matchIds.has(n.friendship_id);
          return (
            <line
              key={n.friendship_id}
              x1="50"
              y1="50"
              x2={x * 100}
              y2={y * 100}
              stroke={on || hit ? "var(--os-accent)" : `rgba(226,232,240,${(q ? QUIET_ALPHA : BOND_ALPHA)[n.bond]})`}
              strokeOpacity={on || hit ? (q ? 0.7 : 1) : 1}
              strokeWidth={(q ? QUIET_WIDTH : BOND_WIDTH)[n.bond]}
              strokeLinecap="round"
              vectorEffect="non-scaling-stroke"
              className={q || quiet ? "hidden sm:inline" : undefined}
            />
          );
        })}
      </svg>

      <div className="absolute left-1/2 top-1/2 flex -translate-x-1/2 -translate-y-1/2 flex-col items-center gap-1.5">
        <span className="h-4 w-4 rounded-full bg-os-ink shadow-[0_0_18px_rgba(226,232,240,0.55)]" aria-hidden="true" />
        <span className="rounded bg-[rgba(7,9,12,0.8)] px-1 text-[0.8125rem] text-os-ink">You</span>
      </div>

      {spots.map(({ n, a, x, y, quiet: q }) => {
        const on = n.friendship_id === selectedId;
        const hit = matchIds.has(n.friendship_id);
        const side = labelSide(a);
        const showName = !q || on || hit;
        return (
          <button
            key={n.friendship_id}
            type="button"
            onClick={() => onSelect(n.friendship_id)}
            aria-pressed={on}
            aria-label={`${n.display_name}, ${sinceLabel(n)}`}
            className={`os-focus group absolute flex h-6 w-6 -translate-x-1/2 -translate-y-1/2 items-center justify-center rounded-full ${
              q ? "hidden sm:flex" : ""
            } ${on || hit ? "z-20" : q ? "z-0 hover:z-20 focus-visible:z-20" : "z-10"}`}
            style={
              quiet && !q
                ? { left: `calc(50% + var(--sky-r) * ${Math.cos(a).toFixed(4)})`, top: `calc(50% + var(--sky-r) * ${Math.sin(a).toFixed(4)})` }
                : { left: `${x * 100}%`, top: `${y * 100}%` }
            }
          >
            <span
              aria-hidden="true"
              className={`rounded-full transition ${
                q ? "h-[7px] w-[7px] opacity-70 group-hover:opacity-100" : "h-3.5 w-3.5 ring-2 ring-os-sky"
              } ${on || hit ? "opacity-100 outline outline-1 outline-offset-2 outline-os-accent" : ""}`}
              style={{
                backgroundColor: `hsl(${n.avatar_hue} ${q ? 35 : 55}% ${q ? 72 : 70}%)`,
                boxShadow: q ? `0 0 6px hsl(${n.avatar_hue} 40% 70% / 0.45)` : undefined,
              }}
            />
            <span
              aria-hidden="true"
              className={`pointer-events-none absolute flex flex-col ${SIDE_CLASS[side]} ${
                showName ? "" : "hidden group-hover:flex group-focus-visible:flex"
              }`}
            >
              <span
                className={`whitespace-nowrap rounded bg-[rgba(7,9,12,0.82)] px-1 text-[0.8125rem] leading-tight ${
                  on || hit ? "text-os-accent" : "text-os-ink"
                } ${q ? "" : "max-w-[7.5rem] truncate sm:max-w-[9.5rem]"}`}
              >
                {quiet && !q ? (
                  // Phones in a crowd: first name on the map; the list has the full name.
                  <>
                    <span className="sm:hidden">{n.display_name.split(" ")[0]}</span>
                    <span className="hidden sm:inline">{n.display_name}</span>
                  </>
                ) : (
                  n.display_name
                )}
              </span>
              {!quiet || q ? (
                <span className="whitespace-nowrap rounded bg-[rgba(7,9,12,0.82)] px-1 text-[0.6875rem] leading-tight text-os-faint">
                  {sinceLabel(n)}
                </span>
              ) : null}
            </span>
          </button>
        );
      })}

      {quiet ? (
        // Phones: the crowd lives in the list below; the map keeps your sky.
        <p
          className="absolute left-1/2 w-max -translate-x-1/2 rounded bg-[rgba(7,9,12,0.82)] px-2 text-[0.8125rem] text-os-muted sm:hidden"
          style={{ top: `${(0.5 + PHONE_HINT) * 100}%`, transform: "translate(-50%, -50%)" }}
        >
          {others} more {others === 1 ? "friend" : "friends"} &mdash; see the list
        </p>
      ) : null}
    </div>
  );
}

const PAGE = 40;

/** A–Z list of everyone: the primary view on phones and for big neighborhoods. */
export function EveryoneList({
  neighbors,
  query,
  selectedId,
  onSelect,
}: {
  neighbors: HomeNeighbor[];
  query: string;
  selectedId: string | null;
  onSelect: (id: string) => void;
}) {
  const [shown, setShown] = useState(PAGE);
  const rows = useMemo(() => {
    const q = query.trim().toLowerCase();
    const all = [...neighbors].sort(byName);
    return q ? all.filter((n) => n.display_name.toLowerCase().includes(q) || n.handle.toLowerCase().includes(q)) : all;
  }, [neighbors, query]);
  const visible = rows.slice(0, query.trim() ? rows.length : shown);

  return (
    <div>
      {rows.length === 0 ? <p className="py-3 text-[0.9375rem] text-os-muted">No one matches &ldquo;{query}&rdquo;.</p> : null}
      <ul>
        {visible.map((n, i) => {
          const letter = n.display_name.charAt(0).toLocaleUpperCase();
          const header = i === 0 || visible[i - 1].display_name.charAt(0).toLocaleUpperCase() !== letter ? letter : null;
          const on = n.friendship_id === selectedId;
          return (
            <li key={n.friendship_id}>
              {header ? <p className="os-label pb-1 pt-4 text-[0.6875rem]">{header}</p> : null}
              <button
                type="button"
                onClick={() => onSelect(n.friendship_id)}
                aria-pressed={on}
                className={`os-focus os-hairline-top flex min-h-[44px] w-full items-center justify-between gap-4 text-left ${on ? "text-os-accent" : "text-os-ink"}`}
              >
                <span className="flex min-w-0 items-center gap-3">
                  <span className="h-2 w-2 shrink-0 rounded-full" style={{ backgroundColor: `hsl(${n.avatar_hue} 45% 70%)` }} aria-hidden="true" />
                  <span className="truncate text-[0.9375rem]">{n.display_name}</span>
                </span>
                <span className={`shrink-0 text-[0.8125rem] ${n.in_my_sky ? "text-os-accent" : "text-os-muted"}`}>{sinceLabel(n)}</span>
              </button>
            </li>
          );
        })}
      </ul>
      {!query.trim() && rows.length > shown ? (
        <button
          type="button"
          onClick={() => setShown((s) => s + PAGE)}
          className="os-focus os-hairline-top mt-1 min-h-[44px] w-full text-left text-[0.875rem] text-os-accent"
        >
          Show more
        </button>
      ) : null}
    </div>
  );
}
