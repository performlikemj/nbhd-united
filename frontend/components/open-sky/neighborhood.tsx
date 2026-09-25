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
import { EveryoneList, PeopleMap, QUIET_AFTER, sinceLabel } from "@/components/open-sky/people-map";
import type { HomeNeighbor, MissionAsk } from "@/lib/types";

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
  const [query, setQuery] = useState("");

  const neighbors = useMemo(() => home.data?.neighbors ?? [], [home.data]);
  const crowd = neighbors.filter((n) => !n.in_my_sky).length > QUIET_AFTER;
  const matchIds = useMemo(() => {
    const q = query.trim().toLowerCase();
    if (!q) return new Set<string>();
    const hits = neighbors.filter((n) => n.display_name.toLowerCase().includes(q) || n.handle.toLowerCase().includes(q));
    // Label a handful on the map; the list below shows every match.
    return new Set(hits.slice(0, 6).map((n) => n.friendship_id));
  }, [neighbors, query]);
  const select = (id: string, fromList = false) => {
    setSkyError("");
    setSelectedId((cur) => (cur === id && !fromList ? null : id));
    if (fromList) requestAnimationFrame(() => document.getElementById("person-panel")?.scrollIntoView({ block: "nearest" }));
  };
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
              {crowd
                ? "Closer in is your sky \u2014 the people you chose. Everyone else is a small dot further out; hover, tap or search to see a name."
                : "Closer in is your sky \u2014 the people you chose. A thicker line means you two share more."}
            </p>
            {crowd ? (
              <div className="mb-2 max-w-[360px]">
                <label htmlFor="find-person" className="sr-only">
                  Find a person
                </label>
                <input
                  id="find-person"
                  type="search"
                  value={query}
                  onChange={(e) => setQuery(e.target.value)}
                  onKeyDown={(e) => {
                    if (e.key === "Enter" && matchIds.size > 0) select([...matchIds][0], true);
                  }}
                  placeholder="Find a person"
                  autoComplete="off"
                  className="os-focus min-h-[44px] w-full rounded-full border border-os-ring bg-transparent px-4 text-[0.9375rem] text-os-ink placeholder:text-os-faint focus:border-os-accent-line"
                />
              </div>
            ) : null}
            <PeopleMap neighbors={neighbors} selectedId={selectedId} matchIds={matchIds} onSelect={(id) => select(id)} />
            {selected ? (
              <div id="person-panel" className="os-hairline-top mx-auto flex max-w-[640px] scroll-mt-24 flex-wrap items-center gap-x-6 gap-y-3 pt-4" aria-live="polite">
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
            {crowd || neighbors.length > 8 ? (
              <div className="mt-10">
                <h3 className="os-label mb-1">Everyone</h3>
                <EveryoneList neighbors={neighbors} query={query} selectedId={selectedId} onSelect={(id) => select(id, true)} />
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
