/**
 * Constellation game — pure nega-self encounter logic. No Phaser, no DOM.
 *
 * SERVER-SEAM: this mirrors what the backend will eventually compute. The real
 * gaps could be derived server-side from GET /api/v1/lessons/galaxy/ + tutoring
 * history; keeping the shapes/thresholds here means that lift needs no client
 * rewrite. For now it runs client-side over the live galaxy payload.
 */

export type StarStage = "proto" | "ignited" | "radiant" | "supernova";

export interface GalaxyStar {
  id: number;
  text: string;
  tags: string[];
  cluster_id: number | null;
  cluster_label: string;
  star_stage: StarStage;
  x: number | null;
  y: number | null;
  journal_count: number;
  connection_count: number;
  last_tutored_at: string | null;
  last_visited_at: string | null;
  galaxy_note: string;
  source_type: string;
  created_at: string;
}

export interface GalaxyEdge {
  source: number;
  target: number;
  similarity: number;
  connection_type: string;
}

export interface GalaxyCluster {
  id: number;
  label: string;
  count: number;
  tags: string[];
}

export interface GalaxyData {
  stars: GalaxyStar[];
  edges: GalaxyEdge[];
  clusters?: GalaxyCluster[];
}

export type GapType = "stale_cluster" | "stuck_proto" | "drifted_star";

export interface Gap {
  type: GapType;
  clusterId: number | null;
  clusterLabel: string;
  focalStarId: number | null;
  focalStarText: string | null;
  daysSince: number | null;
  severity: number;
}

export interface ReframeStar {
  id: number;
  text: string;
  star_stage: StarStage;
  galaxy_note: string;
}

const STAGE_RANK: Record<string, number> = { proto: 0, ignited: 1, radiant: 2, supernova: 3 };
const STALE_DAYS = 14;
const STUCK_PROTO_DAYS = 14;
const NEVER_VISITED_DAYS = 365;

function stageRank(stage: string): number {
  return STAGE_RANK[stage] === undefined ? -1 : STAGE_RANK[stage];
}

function daysSinceIso(iso: string | null, now: number): number {
  if (!iso) return NEVER_VISITED_DAYS;
  const t = Date.parse(iso);
  if (Number.isNaN(t)) return NEVER_VISITED_DAYS;
  const ms = now - t;
  if (ms <= 0) return 0;
  return Math.floor(ms / 86_400_000);
}

function starDaysSince(star: GalaxyStar, now: number): number {
  if (star.last_visited_at) return daysSinceIso(star.last_visited_at, now);
  if (star.last_tutored_at) return daysSinceIso(star.last_tutored_at, now);
  return NEVER_VISITED_DAYS;
}

function clusterLabelFor(stars: GalaxyStar[], clusterId: number | null): string {
  for (const s of stars) if (s.cluster_id === clusterId && s.cluster_label) return s.cluster_label;
  return "that corner";
}

/**
 * Three flavours of real neglect, scored by severity (most-severe first):
 *   stale_cluster — a whole cluster you haven't come near in a long time.
 *   stuck_proto   — a proto star with an old created_at that never grew.
 *   drifted_star  — a once-bright (radiant+) star you stopped going back to.
 */
export function detectGaps(stars: GalaxyStar[], nowMs?: number): Gap[] {
  const now = typeof nowMs === "number" ? nowMs : Date.now();
  const list = Array.isArray(stars) ? stars : [];
  const gaps: Gap[] = [];

  const byCluster = new Map<number, GalaxyStar[]>();
  for (const s of list) {
    if (s.cluster_id === null || s.cluster_id === undefined) continue;
    const arr = byCluster.get(s.cluster_id) ?? [];
    arr.push(s);
    byCluster.set(s.cluster_id, arr);
  }

  for (const [cid, members] of byCluster.entries()) {
    const label = clusterLabelFor(list, cid);
    let freshest = Infinity;
    for (const s of members) freshest = Math.min(freshest, starDaysSince(s, now));
    if (freshest >= STALE_DAYS) {
      const focal = members.slice().sort((a, b) => stageRank(b.star_stage) - stageRank(a.star_stage))[0];
      gaps.push({
        type: "stale_cluster",
        clusterId: cid,
        clusterLabel: label,
        focalStarId: focal ? focal.id : null,
        focalStarText: focal ? focal.text : null,
        daysSince: freshest === Infinity ? NEVER_VISITED_DAYS : freshest,
        severity: freshest + members.length * 2,
      });
    }
  }

  for (const s of list) {
    if (s.star_stage !== "proto" || !s.created_at) continue;
    const ageDays = daysSinceIso(s.created_at, now);
    if (ageDays < STUCK_PROTO_DAYS) continue;
    gaps.push({
      type: "stuck_proto",
      clusterId: s.cluster_id ?? null,
      clusterLabel: s.cluster_label || clusterLabelFor(list, s.cluster_id),
      focalStarId: s.id,
      focalStarText: s.text,
      daysSince: ageDays,
      severity: 8 + ageDays * 0.3,
    });
  }

  for (const s of list) {
    if (stageRank(s.star_stage) < STAGE_RANK.radiant) continue;
    const visited = s.last_visited_at ? daysSinceIso(s.last_visited_at, now) : null;
    if (visited !== null && visited < STALE_DAYS) continue;
    gaps.push({
      type: "drifted_star",
      clusterId: s.cluster_id ?? null,
      clusterLabel: s.cluster_label || clusterLabelFor(list, s.cluster_id),
      focalStarId: s.id,
      focalStarText: s.text,
      daysSince: visited,
      severity: (visited === null ? NEVER_VISITED_DAYS : visited) + stageRank(s.star_stage) * 8,
    });
  }

  gaps.sort((a, b) => b.severity - a.severity);
  return gaps;
}

/** The shadow's line — cheeky, external, never cruel; always grounded in a real field. */
export function buildTaunt(gap: Gap | null): string {
  if (!gap) return "Going somewhere? I doubt it.";
  const label = gap.clusterLabel || "that corner";
  const days = gap.daysSince;
  const focal = gap.focalStarText;
  const when =
    days === null || days === undefined
      ? "in a while"
      : days >= NEVER_VISITED_DAYS
        ? "not even once"
        : days >= 60
          ? `in ${Math.round(days / 30)} months`
          : days >= 14
            ? `in ${days} days`
            : "lately";

  switch (gap.type) {
    case "stale_cluster":
      return `Oh, ${label}? You haven't dropped by ${when}. I've been keeping it dark for you — figured you'd given up.`;
    case "stuck_proto":
      return `Cute little proto over in ${label}: ${focal ? `"${focal}." ` : ""}It never grew. Some things just stay small, don't they?`;
    case "drifted_star":
      return `${focal ? `Remember "${focal}"? ` : `Remember that bright one in ${label}? `}You haven't looked at it ${when}. Bright things fade when you stop watching.`;
    default:
      return `${label} has been gathering dust ${when}. Convenient, ignoring it.`;
  }
}

/**
 * Real stars to fire back with. A comeback only lands if it's RELATED to the
 * jab, so rank by relatedness first: the focal star's graph neighbours (edges)
 * and same-cluster stars, brightest stage + a galaxy_note as tie-breakers.
 * Prefer 2–3 on-point comebacks over padding to 4 with unrelated stars.
 */
export function chooseReframes(stars: GalaxyStar[], gap: Gap | null, edges?: GalaxyEdge[]): ReframeStar[] {
  const list = Array.isArray(stars) ? stars : [];
  const edgeList = Array.isArray(edges) ? edges : [];
  const focalId = gap ? gap.focalStarId : null;
  const clusterId = gap ? gap.clusterId : null;

  const neighbors = new Set<number>();
  if (focalId !== null && focalId !== undefined) {
    for (const e of edgeList) {
      if (e.source === focalId) neighbors.add(e.target);
      else if (e.target === focalId) neighbors.add(e.source);
    }
  }

  const sameCluster = (s: GalaxyStar) =>
    clusterId !== null && clusterId !== undefined && s.cluster_id === clusterId;
  const isRelevant = (s: GalaxyStar) => neighbors.has(s.id) || sameCluster(s);

  const score = (s: GalaxyStar) => {
    let v = stageRank(s.star_stage) * 8;
    if (neighbors.has(s.id)) v += 100;
    if (sameCluster(s)) v += 40;
    if (s.galaxy_note) v += 8;
    if (s.journal_count) v += Math.min(s.journal_count, 6) * 0.5;
    return v;
  };

  const grown = (s: GalaxyStar) => s.id !== focalId && stageRank(s.star_stage) >= STAGE_RANK.ignited;

  let pool = list.filter(grown).filter(isRelevant).sort((a, b) => score(b) - score(a));
  if (pool.length < 2) {
    const extra = list.filter(grown).filter((s) => !isRelevant(s)).sort((a, b) => score(b) - score(a));
    pool = pool.concat(extra);
  }
  if (pool.length < 2) {
    pool = list.filter((s) => s.id !== focalId).sort((a, b) => score(b) - score(a));
  }

  const limit = pool.filter(isRelevant).length >= 2 ? 3 : 4;
  return pool.slice(0, limit).map((s) => ({
    id: s.id,
    text: s.text,
    star_stage: s.star_stage,
    galaxy_note: s.galaxy_note || "",
  }));
}
