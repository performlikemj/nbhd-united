"use client";

import { useMemo, useState } from "react";

import { OpenSkyRow, OpenSkySection } from "@/components/open-sky/primitives";
import { getErrorMessage } from "@/lib/errors";
import {
  useAcceptWaveMutation,
  useCirclesQuery,
  useDeclineWaveMutation,
  useJoinMissionMutation,
  useMissionAsksQuery,
  useNeighborhoodHomeQuery,
  useSkyMembershipMutation,
} from "@/lib/queries";
import type { HomeNeighbor, MissionAsk, NeighborBond } from "@/lib/types";

// Line width = how much you two share, in three bounded steps. A quiet friend
// still gets a visible line; nothing on screen is a number.
const BOND_WIDTH: Record<NeighborBond, number> = { light: 0.8, steady: 1.6, strong: 2.6 };
const BOND_ALPHA: Record<NeighborBond, number> = { light: 0.32, steady: 0.48, strong: 0.66 };

const INNER = 0.22; // ring radii as a fraction of the map box (a circle on desktop, taller on phones)
const OUTER = 0.39;

function byName(a: HomeNeighbor, b: HomeNeighbor) {
  return a.display_name.localeCompare(b.display_name) || a.friendship_id.localeCompare(b.friendship_id);
}

function sinceLabel(n: HomeNeighbor) {
  return n.in_my_sky ? "in your sky" : `friends since ${n.friends_since.slice(0, 4)}`;
}

/**
 * Stable spots on two rings: your sky inside, everyone else outside. Order is
 * by name, never activity; the outer ring turns to stay clear of inner spokes
 * so no line runs through someone else's name.
 */
function placePeople(neighbors: HomeNeighbor[]) {
  const inner = neighbors.filter((n) => n.in_my_sky).sort(byName);
  const outer = neighbors.filter((n) => !n.in_my_sky).sort(byName);
  const angles = (count: number, turn: number) =>
    Array.from({ length: count }, (_, i) => -Math.PI / 2 + turn + (i / Math.max(count, 1)) * Math.PI * 2);
  const innerA = angles(inner.length, 0);
  const step = (Math.PI * 2) / Math.max(outer.length, 1);
  let bestTurn = step / 2;
  let bestGap = -1;
  for (let k = 0; k < 24; k++) {
    const turn = (k / 24) * step;
    const gap = Math.min(
      Math.PI,
      ...angles(outer.length, turn).flatMap((a) =>
        innerA.map((b) => {
          const d = Math.abs(((a - b + Math.PI * 3) % (Math.PI * 2)) - Math.PI);
          return d;
        }),
      ),
    );
    if (gap > bestGap + 1e-6) {
      bestGap = gap;
      bestTurn = turn;
    }
  }
  const at = (n: HomeNeighbor, a: number, r: number) => ({ n, x: 0.5 + Math.cos(a) * r, y: 0.5 + Math.sin(a) * r });
  return [
    ...inner.map((n, i) => at(n, innerA[i], INNER)),
    ...outer.map((n, i) => at(n, angles(outer.length, bestTurn)[i], OUTER)),
  ];
}

function PeopleMap({
  neighbors,
  selectedId,
  onSelect,
}: {
  neighbors: HomeNeighbor[];
  selectedId: string | null;
  onSelect: (id: string) => void;
}) {
  const placed = useMemo(() => placePeople(neighbors), [neighbors]);
  return (
    <div className="relative mx-auto aspect-[4/5] w-full max-w-[640px] sm:aspect-square">
      <svg viewBox="0 0 100 100" preserveAspectRatio="none" className="absolute inset-0 h-full w-full" aria-hidden="true">
        <ellipse cx="50" cy="50" rx={INNER * 100} ry={INNER * 100} fill="none" stroke="var(--os-hairline)" strokeWidth="1" strokeDasharray="2 5" vectorEffect="non-scaling-stroke" />
        <ellipse cx="50" cy="50" rx={OUTER * 100} ry={OUTER * 100} fill="none" stroke="var(--os-hairline)" strokeWidth="1" strokeDasharray="2 5" vectorEffect="non-scaling-stroke" />
        {placed.map(({ n, x, y }) => (
          <line
            key={n.friendship_id}
            x1="50"
            y1="50"
            x2={x * 100}
            y2={y * 100}
            stroke={n.friendship_id === selectedId ? "var(--os-accent)" : `rgba(226,232,240,${BOND_ALPHA[n.bond]})`}
            strokeWidth={BOND_WIDTH[n.bond]}
            strokeLinecap="round"
            vectorEffect="non-scaling-stroke"
          />
        ))}
      </svg>
      <div className="absolute left-1/2 top-1/2 flex -translate-x-1/2 -translate-y-1/2 flex-col items-center gap-1.5">
        <span className="h-4 w-4 rounded-full bg-os-ink shadow-[0_0_18px_rgba(226,232,240,0.55)]" aria-hidden="true" />
        <span className="rounded bg-[rgba(7,9,12,0.8)] px-1 text-[0.8125rem] text-os-ink">You</span>
      </div>
      {placed.map(({ n, x, y }) => {
        const on = n.friendship_id === selectedId;
        // Names sit on the far side of the dot so the spoke never crosses them.
        const above = y < 0.5;
        return (
          <button
            key={n.friendship_id}
            type="button"
            onClick={() => onSelect(n.friendship_id)}
            aria-pressed={on}
            aria-label={`${n.display_name}, ${sinceLabel(n)}`}
            className={`os-focus group absolute flex -translate-x-1/2 items-center rounded-lg px-1.5 ${above ? "-translate-y-full flex-col-reverse" : "flex-col"}`}
            style={{ left: `${x * 100}%`, top: `calc(${y * 100}% + ${above ? 7 : -7}px)` }}
          >
            <span
              className={`h-3.5 w-3.5 shrink-0 rounded-full ring-2 ring-os-sky transition ${on ? "outline outline-1 outline-offset-2 outline-os-accent" : ""}`}
              style={{ backgroundColor: `hsl(${n.avatar_hue} 55% 70%)` }}
              aria-hidden="true"
            />
            <span className={`flex flex-col items-center rounded bg-[rgba(7,9,12,0.8)] px-1 ${above ? "mb-1" : "mt-1"}`}>
              <span className={`whitespace-nowrap text-[0.8125rem] leading-tight ${on ? "text-os-accent" : "text-os-ink group-hover:text-os-accent"}`}>
                {n.display_name}
              </span>
              <span className="whitespace-nowrap text-[0.6875rem] leading-tight text-os-faint">{sinceLabel(n)}</span>
            </span>
          </button>
        );
      })}
    </div>
  );
}

function outlineBtn(extra = "") {
  return `os-focus min-h-[40px] rounded-full border border-os-ring px-4 text-[0.8125rem] text-os-ink transition hover:border-os-accent-line hover:text-os-accent disabled:opacity-40 ${extra}`;
}

/**
 * Open Sky Neighborhood: the "Your people" map (distance = relationship), then
 * asks, waves and circles as quiet lists. Nothing here counts, scores or says
 * when someone was last around.
 */
export function NeighborhoodOpenSky({
  onMessage,
  onOpenCircle,
}: {
  onMessage: (n: HomeNeighbor) => void;
  onOpenCircle: (circleId: string) => void;
}) {
  const home = useNeighborhoodHomeQuery();
  const asks = useMissionAsksQuery();
  const circles = useCirclesQuery();
  const accept = useAcceptWaveMutation();
  const decline = useDeclineWaveMutation();
  const join = useJoinMissionMutation();
  const sky = useSkyMembershipMutation();
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [skyError, setSkyError] = useState("");

  const neighbors = home.data?.neighbors ?? [];
  const selected = neighbors.find((n) => n.friendship_id === selectedId) ?? null;
  const waves = home.data?.pending_in ?? [];
  const waiting = home.data?.pending_out ?? [];
  const openAsks = (asks.data ?? []).filter((m: MissionAsk) => m.my_status === "invited" && m.status === "active");

  const toggleSky = (n: HomeNeighbor) => {
    setSkyError("");
    sky.mutate(
      { friendshipId: n.friendship_id, inSky: !n.in_my_sky },
      { onError: (err) => setSkyError(skyErrorMessage(err)) },
    );
  };

  return (
    <div className="space-y-12 pb-10">
      <OpenSkySection label="Your people">
        {home.isLoading ? (
          <p className="py-16 text-center text-[0.9375rem] text-os-muted">Finding your people…</p>
        ) : home.error ? (
          <p className="py-16 text-center text-[0.9375rem] text-os-muted">Couldn&rsquo;t load your people. {getErrorMessage(home.error)}</p>
        ) : neighbors.length === 0 ? (
          <p className="py-16 text-center text-[0.9375rem] text-os-muted">
            No one here yet. Wave to someone by @handle below.
          </p>
        ) : (
          <>
            <p className="mb-2 max-w-[60ch] text-[0.875rem] text-os-muted">
              Closer in is your sky &mdash; the people you chose. A thicker line means you two share more.
            </p>
            <PeopleMap
              neighbors={neighbors}
              selectedId={selectedId}
              onSelect={(id) => {
                setSkyError("");
                setSelectedId((cur) => (cur === id ? null : id));
              }}
            />
            {selected ? (
              <div className="os-hairline-top mx-auto flex max-w-[640px] flex-wrap items-center gap-x-6 gap-y-3 pt-4" aria-live="polite">
                <div className="min-w-0 flex-1">
                  <p className="font-serif text-[1.5rem] leading-tight text-os-ink">{selected.display_name}</p>
                  <p className="text-[0.8125rem] text-os-muted">
                    @{selected.handle} · {sinceLabel(selected)}
                  </p>
                </div>
                <div className="flex gap-3">
                  <button type="button" className={outlineBtn()} onClick={() => onMessage(selected)}>
                    Message
                  </button>
                  <button type="button" className={outlineBtn()} onClick={() => toggleSky(selected)} disabled={sky.isPending}>
                    {selected.in_my_sky ? "Take out of your sky" : "Add to your sky"}
                  </button>
                </div>
                {skyError ? <p className="w-full text-[0.8125rem] text-os-danger" role="alert">{skyError}</p> : null}
              </div>
            ) : null}
          </>
        )}
      </OpenSkySection>

      <OpenSkySection label="Asks from your people">
        {openAsks.length === 0 ? (
          <p className="text-[0.9375rem] text-os-muted">Nothing asked of you right now.</p>
        ) : (
          openAsks.map((m) => {
            const joining = join.isPending && join.variables?.id === m.mission_id;
            return (
              <div key={m.mission_id} className="os-hairline-top flex min-h-[60px] items-center justify-between gap-4 py-2">
                <div className="min-w-0">
                  <p className="truncate text-[1.0625rem] text-os-ink">{m.title}</p>
                  {m.target_date ? <p className="text-[0.8125rem] text-os-muted">By {formatDay(m.target_date)}</p> : null}
                </div>
                <button type="button" className={outlineBtn("shrink-0")} disabled={joining} onClick={() => join.mutate({ id: m.mission_id })}>
                  {joining ? "Joining…" : "I can help"}
                </button>
              </div>
            );
          })
        )}
      </OpenSkySection>

      <OpenSkySection label="Waves">
        {waves.length === 0 && waiting.length === 0 ? (
          <p className="text-[0.9375rem] text-os-muted">No waves waiting.</p>
        ) : null}
        {waves.map((w) => {
          const busy =
            (accept.isPending && accept.variables === w.friendship_id) ||
            (decline.isPending && decline.variables === w.friendship_id);
          return (
            <div key={w.friendship_id} className="os-hairline-top flex min-h-[60px] flex-wrap items-center justify-between gap-x-4 gap-y-2 py-2">
              <div className="min-w-0">
                <p className="truncate text-[1.0625rem] text-os-ink">
                  {w.display_name} <span className="text-[0.8125rem] text-os-muted">@{w.handle}</span>
                </p>
                {w.note ? <p className="text-[0.8125rem] text-os-muted">&ldquo;{w.note}&rdquo;</p> : null}
              </div>
              <div className="flex shrink-0 gap-3">
                <button type="button" className={outlineBtn()} disabled={busy} onClick={() => decline.mutate(w.friendship_id)}>
                  Not now
                </button>
                <button type="button" className={outlineBtn("border-os-accent-line text-os-accent")} disabled={busy} onClick={() => accept.mutate(w.friendship_id)}>
                  Accept
                </button>
              </div>
            </div>
          );
        })}
        {waiting.length > 0 ? (
          <p className="os-hairline-top pt-3 text-[0.8125rem] text-os-muted">
            Waiting on {waiting.map((w) => `@${w.handle}`).join(", ")}
          </p>
        ) : null}
      </OpenSkySection>

      <OpenSkySection label="Circles">
        {(circles.data ?? []).length === 0 ? (
          <p className="text-[0.9375rem] text-os-muted">You&rsquo;re not in any circles yet.</p>
        ) : (
          (circles.data ?? []).map((c) => (
            <OpenSkyRow
              key={c.circle_id}
              title={
                <span className="inline-flex items-center gap-3">
                  <span className="h-2 w-2 rounded-full" style={{ backgroundColor: `hsl(${c.hue} 55% 65%)` }} aria-hidden="true" />
                  {c.name}
                </span>
              }
              detail={c.my_role === "admin" ? "you host" : undefined}
              onClick={() => onOpenCircle(c.circle_id)}
            />
          ))
        )}
      </OpenSkySection>
    </div>
  );
}

function formatDay(isoDay: string) {
  const d = new Date(`${isoDay}T12:00:00`);
  return Number.isNaN(d.getTime()) ? isoDay : d.toLocaleDateString(undefined, { month: "short", day: "numeric" });
}

/** The server owns the sky cap; its 409 body carries the number. */
function skyErrorMessage(err: unknown): string {
  try {
    const body = JSON.parse(err instanceof Error ? err.message : "") as { error?: string; cap?: number };
    if (body.error === "sky_full") {
      return typeof body.cap === "number"
        ? `Your sky holds ${body.cap} people. Take someone out first.`
        : "Your sky is full. Take someone out first.";
    }
  } catch {
    // Not a JSON body — fall through to the shared copy.
  }
  return getErrorMessage(err);
}
