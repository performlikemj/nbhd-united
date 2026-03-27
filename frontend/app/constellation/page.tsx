"use client";

import Link from "next/link";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { fetchConstellation, fetchPendingLessons } from "@/lib/api";
import { ConstellationData, ConstellationNode } from "@/lib/types";

type ViewMode = "constellation" | "list";

const VIEW_MODE_KEY = "constellationViewMode";
const MOBILE_BREAKPOINT = 768;
const CLUSTER_THRESHOLD = 5;
const NODE_PADDING = 120;
const MIN_NODE_SPACING = 90;
const COLLISION_ITERATIONS = 5;

// Brand palette for clusters — cycles through constellation colors
const CLUSTER_PALETTE = [
  "#7C6BF0", // purple
  "#4ECDC4", // teal
  "#E8B4B8", // pink
  "#60A5FA", // blue
  "#F59E0B", // amber
  "#A78BFA", // violet
  "#34D399", // emerald
  "#FB923C", // orange
];

function formatDate(dateString: string): string {
  const date = new Date(dateString);
  if (Number.isNaN(date.getTime())) return dateString;
  return date.toLocaleDateString(undefined, {
    month: "short",
    day: "numeric",
    year: new Date().getFullYear() === date.getFullYear() ? undefined : "numeric",
  });
}

function clusterNodeColor(id: number | null): string {
  if (id == null) return "#4ECDC4";
  return CLUSTER_PALETTE[Math.abs(id) % CLUSTER_PALETTE.length];
}

function getClusterLabel(
  clusterId: number | null,
  clusters: ConstellationData["clusters"],
): string {
  if (clusterId == null) {
    return clusters.length === 0 ? "Your Lessons" : "Unclustered";
  }
  const found = clusters.find((cluster) => cluster.id === clusterId);
  return found?.label || `Cluster ${clusterId}`;
}

function lessonTitle(node: ConstellationNode): string {
  if (node.tags.length > 0) return node.tags.slice(0, 2).join(" / ");
  const words = node.text.split(/\s+/).slice(0, 5).join(" ");
  return words.length < node.text.length ? words + "\u2026" : words;
}

function loadStoredViewMode(): ViewMode | null {
  if (typeof window === "undefined") return null;
  const raw = window.localStorage.getItem(VIEW_MODE_KEY);
  if (raw === "constellation" || raw === "list") return raw;
  if (raw === "graph") return "constellation";
  if (raw === "cards") return "list";
  return null;
}

function defaultViewMode(): ViewMode {
  if (typeof window === "undefined") return "list";
  return window.innerWidth >= MOBILE_BREAKPOINT ? "constellation" : "list";
}

/** Push overlapping nodes apart until spacing is satisfied */
function resolveCollisions(
  nodes: { id: number; px: number; py: number }[],
  width: number,
  height: number,
): { id: number; px: number; py: number }[] {
  const result = nodes.map((n) => ({ ...n }));
  for (let iter = 0; iter < COLLISION_ITERATIONS; iter++) {
    for (let i = 0; i < result.length; i++) {
      for (let j = i + 1; j < result.length; j++) {
        const dx = result[j].px - result[i].px;
        const dy = result[j].py - result[i].py;
        const dist = Math.sqrt(dx * dx + dy * dy) || 1;
        if (dist < MIN_NODE_SPACING) {
          const overlap = (MIN_NODE_SPACING - dist) / 2;
          const nx = (dx / dist) * overlap;
          const ny = (dy / dist) * overlap;
          result[i].px -= nx;
          result[i].py -= ny;
          result[j].px += nx;
          result[j].py += ny;
        }
      }
    }
    // Clamp to bounds
    for (const n of result) {
      n.px = Math.max(NODE_PADDING, Math.min(width - NODE_PADDING, n.px));
      n.py = Math.max(NODE_PADDING, Math.min(height - NODE_PADDING, n.py));
    }
  }
  return result;
}

export default function ConstellationPage() {
  const [data, setData] = useState<ConstellationData>({ nodes: [], edges: [], affinity_edges: [], clusters: [] });
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");

  const [searchText, setSearchText] = useState("");
  const [selectedClusterId, setSelectedClusterId] = useState<number | null>(null);
  const [selectedNodeId, setSelectedNodeId] = useState<number | null>(null);
  const [pendingCount, setPendingCount] = useState(0);
  const [viewMode, setViewMode] = useState<ViewMode>("list");
  const [collapsedClusters, setCollapsedClusters] = useState<Set<string>>(new Set());
  const [containerSize, setContainerSize] = useState({ width: 0, height: 0 });
  const [showFilters, setShowFilters] = useState(false);

  const userToggledRef = useRef(false);
  const observerRef = useRef<ResizeObserver | null>(null);

  const containerRef = useCallback((node: HTMLDivElement | null) => {
    if (observerRef.current) {
      observerRef.current.disconnect();
      observerRef.current = null;
    }
    if (node) {
      observerRef.current = new ResizeObserver((entries) => {
        const { width, height } = entries[0].contentRect;
        setContainerSize({ width, height });
      });
      observerRef.current.observe(node);
    }
  }, []);

  useEffect(() => {
    let mounted = true;
    async function loadData() {
      try {
        const [constellationData, pendingLessons] = await Promise.all([
          fetchConstellation(),
          fetchPendingLessons(),
        ]);
        if (!mounted) return;
        setData(constellationData);
        setPendingCount(pendingLessons.length);
      } catch (err) {
        if (!mounted) return;
        setError(err instanceof Error ? err.message : "Failed to load constellation.");
      } finally {
        if (mounted) setLoading(false);
      }
    }

    const storedMode = loadStoredViewMode();
    setViewMode(storedMode ?? defaultViewMode());

    const mediaQuery = window.matchMedia(`(min-width: ${MOBILE_BREAKPOINT}px)`);
    const handleMediaChange = () => {
      const persisted = loadStoredViewMode();
      if (!persisted) {
        setViewMode(window.innerWidth >= MOBILE_BREAKPOINT ? "constellation" : "list");
      }
    };

    mediaQuery.addEventListener("change", handleMediaChange);
    loadData();

    return () => {
      mediaQuery.removeEventListener("change", handleMediaChange);
      mounted = false;
    };
  }, []);

  useEffect(() => {
    if (typeof window !== "undefined") {
      window.localStorage.setItem(VIEW_MODE_KEY, viewMode);
    }
  }, [viewMode]);

  const query = searchText.trim().toLowerCase();
  const totalLessonCount = data.nodes.length;
  const isSparse = totalLessonCount < CLUSTER_THRESHOLD;

  const filteredNodes = useMemo(() => {
    return data.nodes.filter((node) => {
      const matchesSearch = query
        ? node.text.toLowerCase().includes(query) || node.tags.some((tag) => tag.toLowerCase().includes(query))
        : true;
      const matchesCluster = selectedClusterId == null ? true : node.cluster_id === selectedClusterId;
      return matchesSearch && matchesCluster;
    });
  }, [data.nodes, query, selectedClusterId]);

  const filteredNodeIds = useMemo(() => {
    return new Set(filteredNodes.map((node) => Number(node.id)));
  }, [filteredNodes]);

  const allEdges = useMemo(() => {
    return [...data.edges, ...data.affinity_edges].filter((edge) => {
      return filteredNodeIds.has(Number(edge.source)) && filteredNodeIds.has(Number(edge.target));
    });
  }, [data.edges, data.affinity_edges, filteredNodeIds]);

  const nodesById = useMemo(() => {
    const map = new Map<number, ConstellationNode>();
    data.nodes.forEach((node) => map.set(Number(node.id), node));
    return map;
  }, [data.nodes]);

  // Cluster node counts for sizing
  const clusterSizes = useMemo(() => {
    const counts = new Map<number | null, number>();
    filteredNodes.forEach((n) => {
      counts.set(n.cluster_id, (counts.get(n.cluster_id) || 0) + 1);
    });
    return counts;
  }, [filteredNodes]);

  // Compute pixel positions with collision avoidance
  const positionedNodes = useMemo(() => {
    const hasPositions = filteredNodes.some((n) => n.x != null && n.y != null);
    const count = filteredNodes.length;

    const raw = filteredNodes.map((node, i) => {
      let nx: number;
      let ny: number;

      if (hasPositions && node.x != null && node.y != null) {
        nx = node.x;
        ny = node.y;
      } else if (count === 1) {
        nx = 0;
        ny = 0;
      } else {
        const angle = (2 * Math.PI * i) / count - Math.PI / 2;
        const radius = 0.6;
        nx = radius * Math.cos(angle);
        ny = radius * Math.sin(angle);
      }

      const px = containerSize.width > 0
        ? NODE_PADDING + ((nx + 1) / 2) * (containerSize.width - 2 * NODE_PADDING)
        : 0;
      const py = containerSize.height > 0
        ? NODE_PADDING + ((ny + 1) / 2) * (containerSize.height - 2 * NODE_PADDING)
        : 0;

      return { ...node, px, py };
    });

    // Resolve collisions
    if (raw.length > 1 && containerSize.width > 0) {
      const resolved = resolveCollisions(
        raw.map((n) => ({ id: n.id, px: n.px, py: n.py })),
        containerSize.width,
        containerSize.height,
      );
      const posMap = new Map(resolved.map((r) => [r.id, r]));
      return raw.map((n) => {
        const p = posMap.get(n.id);
        return p ? { ...n, px: p.px, py: p.py } : n;
      });
    }

    return raw;
  }, [filteredNodes, containerSize]);

  const nodePositions = useMemo(() => {
    const map = new Map<number, { px: number; py: number }>();
    positionedNodes.forEach((node) => map.set(node.id, { px: node.px, py: node.py }));
    return map;
  }, [positionedNodes]);

  // Find the "anchor" node per cluster (first node) for cluster labels
  const clusterAnchors = useMemo(() => {
    const anchors = new Map<number | null, typeof positionedNodes[0]>();
    for (const node of positionedNodes) {
      if (!anchors.has(node.cluster_id)) {
        anchors.set(node.cluster_id, node);
      }
    }
    return anchors;
  }, [positionedNodes]);

  const allTags = useMemo(() => {
    const tags = new Set<string>();
    data.nodes.forEach((n) => n.tags.forEach((t) => tags.add(t)));
    return Array.from(tags).sort();
  }, [data.nodes]);

  const selectedNode = useMemo(() => {
    if (selectedNodeId == null) return null;
    return nodesById.get(selectedNodeId) ?? null;
  }, [nodesById, selectedNodeId]);

  const clusterCards = useMemo(() => {
    const groups = new Map<number | null, ConstellationNode[]>();
    filteredNodes.forEach((node) => {
      const key = node.cluster_id;
      const existing = groups.get(key);
      if (existing) { existing.push(node); } else { groups.set(key, [node]); }
    });
    return Array.from(groups.entries())
      .map(([clusterId, nodes]) => ({
        id: clusterId,
        label: getClusterLabel(clusterId, data.clusters),
        count: nodes.length,
        nodes,
      }))
      .sort((a, b) => {
        if (a.id === null && b.id !== null) return 1;
        if (a.id !== null && b.id === null) return -1;
        return a.label.toLowerCase().localeCompare(b.label.toLowerCase());
      });
  }, [filteredNodes, data.clusters]);

  const clustersForSidebar = useMemo(() => {
    const items = data.clusters.map((cluster) => ({
      id: cluster.id as number | null,
      label: cluster.label || `Cluster ${cluster.id}`,
      count: filteredNodes.filter((node) => node.cluster_id === cluster.id).length,
    }));
    const unclusteredCount = filteredNodes.filter((node) => node.cluster_id == null).length;
    if (unclusteredCount > 0) {
      const label = data.clusters.length === 0 ? "Your Lessons" : "Unclustered";
      items.push({ id: null, label, count: unclusteredCount });
    }
    return items.filter((item) => item.count > 0);
  }, [data.clusters, filteredNodes]);

  const handleToggleCluster = useCallback((clusterId: number | null) => {
    const key = String(clusterId);
    setCollapsedClusters((previous) => {
      const next = new Set(previous);
      if (next.has(key)) { next.delete(key); } else { next.add(key); }
      return next;
    });
  }, []);

  const isClusterCollapsed = useCallback(
    (clusterId: number | null) => collapsedClusters.has(String(clusterId)),
    [collapsedClusters],
  );

  const handleSetViewMode = (mode: ViewMode) => {
    userToggledRef.current = true;
    setViewMode(mode);
  };

  // Node size based on cluster importance
  function nodeSize(clusterId: number | null): number {
    const count = clusterSizes.get(clusterId) || 1;
    if (count >= 5) return 48;
    if (count >= 3) return 40;
    if (count >= 2) return 32;
    return 24;
  }

  if (error) {
    return (
      <div className="rounded-panel border border-rose-border bg-rose-bg px-3 py-2 text-sm text-rose-text">{error}</div>
    );
  }

  if (loading) {
    return (
      <div className="flex items-center justify-center py-20">
        <p className="text-sm text-ink-muted">Loading your constellation...</p>
      </div>
    );
  }

  const progressPercent = Math.min(100, (totalLessonCount / CLUSTER_THRESHOLD) * 100);

  // ── Constellation view ──
  if (viewMode === "constellation") {
    return (
      <div className="relative -mx-4 -mt-8 sm:-mx-6">
        {/* Full-viewport graph container */}
        <div
          ref={containerRef}
          className="constellation-bg relative overflow-hidden"
          style={{ minHeight: "calc(100vh - 4rem)" }}
        >
          {/* Top-left controls */}
          <div className="absolute left-6 top-6 z-20 flex flex-col gap-3">
            <div>
              <h1 className="font-headline text-xl font-bold text-ink">Lessons Constellation</h1>
              <p className="text-xs text-ink-muted">Navigate how your approved lessons connect</p>
            </div>

            <div className="flex flex-wrap items-center gap-3">
              <div className="inline-flex rounded-xl border border-border bg-surface/60 backdrop-blur-md p-1">
                <button
                  type="button"
                  onClick={() => handleSetViewMode("constellation")}
                  className="flex items-center gap-1.5 rounded-lg px-3 py-2 text-xs font-medium bg-accent text-white shadow-lg"
                >
                  <svg viewBox="0 0 20 20" fill="currentColor" className="h-3.5 w-3.5"><circle cx="10" cy="10" r="3"/><circle cx="3" cy="5" r="2"/><circle cx="17" cy="5" r="2"/><circle cx="3" cy="15" r="2"/><circle cx="17" cy="15" r="2"/><line x1="10" y1="10" x2="3" y2="5" stroke="currentColor" strokeWidth="1"/><line x1="10" y1="10" x2="17" y2="5" stroke="currentColor" strokeWidth="1"/></svg>
                  Constellation
                </button>
                <button
                  type="button"
                  onClick={() => handleSetViewMode("list")}
                  className="flex items-center gap-1.5 rounded-lg px-3 py-2 text-xs font-medium text-ink-muted hover:text-ink transition"
                >
                  <svg viewBox="0 0 20 20" fill="currentColor" className="h-3.5 w-3.5"><rect x="2" y="3" width="16" height="2" rx="1"/><rect x="2" y="9" width="16" height="2" rx="1"/><rect x="2" y="15" width="16" height="2" rx="1"/></svg>
                  List
                </button>
              </div>

              {pendingCount > 0 && (
                <Link
                  href="/constellation/pending"
                  className="rounded-full border border-accent/30 bg-accent/10 px-3 py-1.5 text-xs font-semibold text-accent backdrop-blur-sm"
                >
                  {pendingCount === 1 ? "1 lesson waiting" : `${pendingCount} lessons waiting`}
                </Link>
              )}

              <button
                type="button"
                onClick={() => setShowFilters(!showFilters)}
                className="rounded-lg border border-border bg-surface/60 backdrop-blur-md px-3 py-2 text-xs text-ink-muted hover:text-ink transition"
              >
                Filters
              </button>
            </div>

            {/* Progress banner (sparse) */}
            {isSparse && totalLessonCount > 0 && (
              <div className="glass rounded-xl p-3 max-w-xs">
                <p className="text-xs text-ink-muted">
                  <span className="font-semibold text-ink">{totalLessonCount}</span> of {CLUSTER_THRESHOLD} lessons — clusters form at {CLUSTER_THRESHOLD}
                </p>
                <div className="mt-1.5 h-1 w-full overflow-hidden rounded-full bg-border">
                  <div className="h-full rounded-full" style={{ width: `${progressPercent}%`, backgroundColor: "var(--signal)" }} />
                </div>
              </div>
            )}
          </div>

          {/* Graph area */}
          {containerSize.width > 0 && filteredNodes.length > 0 && (
            <>
              {/* Edge lines with gradients */}
              <svg className="pointer-events-none absolute inset-0 h-full w-full">
                <defs>
                  <linearGradient id="edge-grad-pt" x1="0%" y1="0%" x2="100%" y2="100%">
                    <stop offset="0%" stopColor="#7C6BF0" />
                    <stop offset="100%" stopColor="#4ECDC4" />
                  </linearGradient>
                  <linearGradient id="edge-grad-tp" x1="0%" y1="0%" x2="100%" y2="100%">
                    <stop offset="0%" stopColor="#4ECDC4" />
                    <stop offset="100%" stopColor="#E8B4B8" />
                  </linearGradient>
                  <linearGradient id="edge-grad-pp" x1="0%" y1="0%" x2="100%" y2="100%">
                    <stop offset="0%" stopColor="#7C6BF0" />
                    <stop offset="100%" stopColor="#E8B4B8" />
                  </linearGradient>
                </defs>
                {allEdges.map((edge, i) => {
                  const sourcePos = nodePositions.get(Number(edge.source));
                  const targetPos = nodePositions.get(Number(edge.target));
                  if (!sourcePos || !targetPos) return null;
                  const sim = edge.similarity ?? 0;
                  const strokeWidth = sim >= 0.75 ? 2 : sim >= 0.5 ? 1.5 : 1;
                  const strokeDasharray = sim < 0.5 ? "4 6" : undefined;
                  const opacity = sim >= 0.75 ? 0.5 : sim >= 0.5 ? 0.3 : 0.15;
                  // Pick gradient based on edge index for variety
                  const grads = ["url(#edge-grad-pt)", "url(#edge-grad-tp)", "url(#edge-grad-pp)"];
                  const stroke = grads[i % grads.length];
                  return (
                    <line
                      key={`${edge.source}-${edge.target}-${i}`}
                      x1={sourcePos.px} y1={sourcePos.py}
                      x2={targetPos.px} y2={targetPos.py}
                      stroke={stroke} strokeWidth={strokeWidth}
                      strokeDasharray={strokeDasharray}
                      opacity={opacity}
                    />
                  );
                })}
              </svg>

              {/* Cluster labels */}
              {Array.from(clusterAnchors.entries()).map(([clusterId, anchor]) => {
                const label = getClusterLabel(clusterId, data.clusters);
                const color = clusterNodeColor(clusterId);
                return (
                  <div
                    key={`cluster-label-${clusterId}`}
                    className="pointer-events-none absolute -translate-x-1/2"
                    style={{ left: anchor.px, top: anchor.py - nodeSize(clusterId) / 2 - 28 }}
                  >
                    <span
                      className="text-[10px] font-headline font-bold uppercase tracking-[0.15em]"
                      style={{ color }}
                    >
                      {label}
                    </span>
                  </div>
                );
              })}

              {/* Nodes */}
              {positionedNodes.map((node) => {
                const isSelected = selectedNodeId === node.id;
                const color = clusterNodeColor(node.cluster_id);
                const size = nodeSize(node.cluster_id);
                return (
                  <button
                    key={node.id}
                    type="button"
                    className="absolute -translate-x-1/2 -translate-y-1/2 group focus-visible:outline-none"
                    style={{ left: node.px, top: node.py, zIndex: isSelected ? 20 : 1 }}
                    aria-label={`Lesson: ${lessonTitle(node)}`}
                    onClick={() => setSelectedNodeId((prev) => (prev === node.id ? null : node.id))}
                  >
                    <div
                      className={`rounded-full transition-transform duration-200 group-hover:scale-125 ${
                        isSelected ? "scale-125 ring-2 ring-white/40 ring-offset-2 ring-offset-c-dark" : ""
                      }`}
                      style={{
                        width: size,
                        height: size,
                        backgroundColor: color,
                        filter: `drop-shadow(0 0 ${size / 3}px ${color}90)`,
                        animation: "constellation-breathe 4s ease-in-out infinite",
                        animationDelay: `${(node.id * 700) % 4000}ms`,
                      }}
                    />
                    {/* Hover tooltip */}
                    <div className="absolute left-1/2 -translate-x-1/2 mt-2 opacity-0 group-hover:opacity-100 transition-opacity pointer-events-none whitespace-nowrap">
                      <span className="rounded-lg bg-surface/95 backdrop-blur-md border border-border px-2.5 py-1 text-[11px] font-medium text-ink shadow-lg">
                        {lessonTitle(node)}
                      </span>
                    </div>
                  </button>
                );
              })}
            </>
          )}

          {/* Empty state */}
          {filteredNodes.length === 0 && (
            <div className="absolute inset-0 flex items-center justify-center">
              <div className="text-center max-w-sm">
                <p className="text-lg font-headline font-bold text-ink">Your constellation begins here</p>
                <p className="mt-2 text-sm text-ink-muted">
                  As you chat with your assistant, lessons will appear as stars in your personal sky.
                </p>
              </div>
            </div>
          )}

          {/* Bottom-left legend */}
          {clustersForSidebar.length > 0 && (
            <div className="absolute bottom-6 left-6 z-20 glass rounded-xl px-4 py-3 flex flex-wrap gap-4">
              {clustersForSidebar.map((cluster) => (
                <button
                  key={`legend-${cluster.id}`}
                  type="button"
                  onClick={() => setSelectedClusterId((c) => (c === cluster.id ? null : cluster.id))}
                  className={`flex items-center gap-2 text-[10px] font-bold uppercase tracking-[0.12em] transition ${
                    selectedClusterId === cluster.id ? "text-ink" : "text-ink-faint hover:text-ink-muted"
                  }`}
                >
                  <span
                    className="inline-block h-2 w-2 rounded-full"
                    style={{ backgroundColor: clusterNodeColor(cluster.id) }}
                  />
                  {cluster.label}
                </button>
              ))}
            </div>
          )}

          {/* Right detail panel */}
          {selectedNode && (
            <aside
              className="absolute right-0 top-0 bottom-0 z-30 w-full sm:w-96 p-4 sm:p-6 pointer-events-none"
            >
              <div className="glass pointer-events-auto h-full rounded-2xl flex flex-col shadow-2xl overflow-hidden">
                <div className="p-5 border-b border-white/10 bg-white/5">
                  <div className="flex items-start justify-between gap-3">
                    <span
                      className="rounded px-2 py-0.5 text-[10px] font-bold uppercase tracking-[0.15em]"
                      style={{ backgroundColor: `${clusterNodeColor(selectedNode.cluster_id)}20`, color: clusterNodeColor(selectedNode.cluster_id) }}
                    >
                      {lessonTitle(selectedNode)}
                    </span>
                    <button
                      type="button"
                      onClick={() => setSelectedNodeId(null)}
                      className="shrink-0 rounded-full p-1 text-ink-faint hover:text-ink transition"
                      aria-label="Close"
                    >
                      <svg viewBox="0 0 20 20" fill="currentColor" className="h-5 w-5"><path fillRule="evenodd" d="M4.293 4.293a1 1 0 011.414 0L10 8.586l4.293-4.293a1 1 0 111.414 1.414L11.414 10l4.293 4.293a1 1 0 01-1.414 1.414L10 11.414l-4.293 4.293a1 1 0 01-1.414-1.414L8.586 10 4.293 5.707a1 1 0 010-1.414z" clipRule="evenodd"/></svg>
                    </button>
                  </div>
                </div>

                <div className="flex-grow overflow-y-auto p-5 space-y-4">
                  <p className="text-sm leading-relaxed text-ink">{selectedNode.text}</p>

                  {selectedNode.context && (
                    <p className="text-xs text-ink-muted">{selectedNode.context}</p>
                  )}

                  <div className="space-y-1">
                    <p className="text-xs text-ink-faint">
                      {selectedNode.source_type ? `Extracted from ${selectedNode.source_type}` : "Source: journal"}
                      {selectedNode.source_ref ? ` \u2014 ${selectedNode.source_ref}` : ""}
                    </p>
                    <p className="text-xs text-ink-faint">{formatDate(selectedNode.created_at)}</p>
                  </div>

                  {selectedNode.tags.length > 0 && (
                    <div className="flex flex-wrap gap-1.5 pt-2 border-t border-white/10">
                      {selectedNode.tags.map((tag) => (
                        <span
                          key={`${selectedNode.id}-${tag}`}
                          className="rounded-full border border-border bg-surface/80 px-2.5 py-0.5 text-[11px] text-ink-muted"
                        >
                          {tag}
                        </span>
                      ))}
                    </div>
                  )}
                </div>
              </div>
            </aside>
          )}

          {/* Filters panel (toggleable) */}
          {showFilters && (
            <div className="absolute right-6 top-6 z-20 glass rounded-2xl p-5 w-80 max-h-[70vh] overflow-y-auto space-y-5">
              <div className="flex items-center justify-between">
                <h3 className="font-headline text-sm font-bold text-ink">Filters</h3>
                <button type="button" onClick={() => setShowFilters(false)} className="text-ink-faint hover:text-ink text-xs">Close</button>
              </div>

              {allTags.length > 0 && (
                <div>
                  <h4 className="text-[10px] font-bold uppercase tracking-[0.15em] text-ink-faint mb-2">Tags</h4>
                  <div className="flex flex-wrap gap-1.5">
                    {allTags.map((tag) => (
                      <button
                        key={tag}
                        type="button"
                        onClick={() => setSearchText((prev) => prev === tag ? "" : tag)}
                        className={`rounded-full px-2.5 py-1 text-xs transition ${
                          searchText === tag
                            ? "bg-accent text-white"
                            : "border border-border text-ink-muted hover:border-border-strong hover:text-ink"
                        }`}
                      >
                        {tag}
                      </button>
                    ))}
                  </div>
                </div>
              )}

              {!isSparse && clustersForSidebar.length > 0 && (
                <div>
                  <h4 className="text-[10px] font-bold uppercase tracking-[0.15em] text-ink-faint mb-2">Clusters</h4>
                  <div className="space-y-1.5">
                    {clustersForSidebar.map((cluster) => (
                      <button
                        type="button"
                        key={`filter-${cluster.id}-${cluster.label}`}
                        onClick={() => setSelectedClusterId((c) => (c === cluster.id ? null : cluster.id))}
                        className={`flex w-full items-center justify-between rounded-lg p-2 text-left transition ${
                          selectedClusterId === cluster.id ? "bg-surface-hover" : "hover:bg-surface-hover"
                        }`}
                      >
                        <div className="flex items-center gap-2 min-w-0">
                          <span className="inline-block h-2.5 w-2.5 shrink-0 rounded-full" style={{ backgroundColor: clusterNodeColor(cluster.id) }} />
                          <span className="truncate text-xs text-ink">{cluster.label}</span>
                        </div>
                        <span className="text-xs text-ink-faint">{cluster.count}</span>
                      </button>
                    ))}
                  </div>
                </div>
              )}
            </div>
          )}
        </div>

        {/* Breathing animation */}
        <style jsx global>{`
          @keyframes constellation-breathe {
            0%, 100% { filter: drop-shadow(0 0 8px rgba(78, 205, 196, 0.15)); }
            50% { filter: drop-shadow(0 0 18px rgba(78, 205, 196, 0.35)); }
          }
          @media (prefers-reduced-motion: reduce) {
            [style*="constellation-breathe"] { animation: none !important; }
          }
        `}</style>
      </div>
    );
  }

  // ── List view ──
  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h1 className="font-headline text-xl font-bold text-ink">Lessons Constellation</h1>
          <p className="text-xs text-ink-muted">Navigate how your approved lessons connect</p>
        </div>
        <div className="flex items-center gap-3">
          <div className="inline-flex rounded-xl border border-border bg-surface/60 backdrop-blur-md p-1">
            <button
              type="button"
              onClick={() => handleSetViewMode("constellation")}
              className="rounded-lg px-3 py-2 text-xs font-medium text-ink-muted hover:text-ink transition"
            >
              Constellation
            </button>
            <button
              type="button"
              onClick={() => handleSetViewMode("list")}
              className="rounded-lg px-3 py-2 text-xs font-medium bg-accent text-white shadow-lg"
            >
              List
            </button>
          </div>
          {pendingCount > 0 && (
            <Link
              href="/constellation/pending"
              className="rounded-full border border-accent/30 bg-accent/10 px-3 py-1.5 text-xs font-semibold text-accent"
            >
              {pendingCount === 1 ? "1 lesson waiting" : `${pendingCount} lessons waiting`}
            </Link>
          )}
        </div>
      </div>

      {/* Progress banner (sparse) */}
      {isSparse && totalLessonCount > 0 && (
        <div className="glass rounded-xl p-4">
          <div className="flex items-center justify-between gap-3">
            <p className="text-sm text-ink">
              <span className="font-semibold">{totalLessonCount}</span> of {CLUSTER_THRESHOLD} lessons
            </p>
            <span className="text-xs text-ink-faint">Clusters form at {CLUSTER_THRESHOLD}</span>
          </div>
          <div className="mt-2 h-1 w-full overflow-hidden rounded-full bg-border">
            <div className="h-full rounded-full" style={{ width: `${progressPercent}%`, backgroundColor: "var(--signal)" }} />
          </div>
        </div>
      )}

      {/* Tags filter */}
      {allTags.length > 0 && (
        <div className="flex flex-wrap gap-1.5">
          {allTags.map((tag) => (
            <button
              key={tag}
              type="button"
              onClick={() => setSearchText((prev) => prev === tag ? "" : tag)}
              className={`rounded-full px-2.5 py-1 text-xs transition ${
                searchText === tag
                  ? "bg-accent text-white"
                  : "border border-border text-ink-muted hover:border-border-strong hover:text-ink"
              }`}
            >
              {tag}
            </button>
          ))}
        </div>
      )}

      {/* Sparse flat list */}
      {isSparse && filteredNodes.length > 0 && (
        <div className="space-y-3">
          {filteredNodes.map((node, index) => {
            const isExpanded = selectedNodeId === node.id;
            const title = lessonTitle(node);
            return (
              <button
                type="button"
                key={node.id}
                onClick={() => setSelectedNodeId((prev) => (prev === node.id ? null : node.id))}
                className={`animate-reveal w-full glass rounded-xl p-4 text-left transition ${
                  isExpanded ? "border-accent/60" : ""
                } active:bg-surface-hover`}
                style={{ animationDelay: `${index * 80}ms` }}
              >
                <p className="text-xs font-semibold uppercase tracking-wide text-signal-text">{title}</p>
                <p className="mt-1 text-sm leading-relaxed text-ink">{node.text}</p>
                <p className="mt-1 text-xs text-ink-faint">{formatDate(node.created_at)}</p>
                {node.tags.length > 0 && (
                  <div className="mt-2 flex flex-wrap gap-1">
                    {node.tags.map((tag) => (
                      <span key={`${node.id}-${tag}`} className="rounded-full border border-border bg-surface/80 px-2 py-0.5 text-[11px] text-ink-muted">{tag}</span>
                    ))}
                  </div>
                )}
                {isExpanded && (
                  <div className="mt-3 space-y-2 border-t border-white/10 pt-2">
                    <p className="text-xs text-ink-muted">{node.context || "No context provided."}</p>
                    {(node.source_type || node.source_ref) && (
                      <p className="text-xs text-ink-faint">
                        Source: {node.source_type ?? ""}{node.source_ref ? ` \u2014 ${node.source_ref}` : ""}
                      </p>
                    )}
                  </div>
                )}
              </button>
            );
          })}
        </div>
      )}

      {/* Clustered list */}
      {!isSparse && (
        clusterCards.length === 0 ? (
          <p className="glass rounded-xl p-4 text-sm text-ink-muted">No approved lessons match your current search yet.</p>
        ) : (
          clusterCards.map((cluster) => {
            const isCollapsed = isClusterCollapsed(cluster.id);
            return (
              <section key={String(cluster.id)} className="space-y-2">
                <button
                  type="button"
                  onClick={() => handleToggleCluster(cluster.id)}
                  className="flex w-full items-center justify-between glass rounded-xl px-4 py-3 text-left"
                >
                  <div className="flex min-w-0 items-center gap-2">
                    <span className="inline-block h-3 w-3 shrink-0 rounded-full" style={{ backgroundColor: clusterNodeColor(cluster.id) }} />
                    <div className="min-w-0">
                      <p className="truncate text-sm font-semibold text-ink">{cluster.label}</p>
                      <p className="text-xs text-ink-muted">{cluster.count} lessons</p>
                    </div>
                  </div>
                  <span className="text-xs text-ink-muted">{isCollapsed ? "\u25B8" : "\u25BE"}</span>
                </button>
                {!isCollapsed && (
                  <div className="space-y-2 pl-2">
                    {cluster.nodes.map((node) => {
                      const isExpanded = selectedNodeId === node.id;
                      return (
                        <button
                          type="button"
                          key={node.id}
                          onClick={() => setSelectedNodeId((prev) => (prev === node.id ? null : node.id))}
                          className={`w-full glass rounded-xl p-3 text-left transition ${isExpanded ? "border-accent/60" : ""} active:bg-surface-hover`}
                        >
                          <p className="text-sm font-medium leading-relaxed text-ink">{node.text}</p>
                          <p className="mt-1 text-xs text-ink-faint">{formatDate(node.created_at)}</p>
                          {node.tags.length > 0 && (
                            <div className="mt-2 flex flex-wrap gap-1">
                              {node.tags.map((tag) => (
                                <span key={`${node.id}-${tag}`} className="rounded-full border border-border bg-surface/80 px-2 py-0.5 text-[11px] text-ink-muted">{tag}</span>
                              ))}
                            </div>
                          )}
                          {isExpanded && (
                            <div className="mt-3 space-y-2 border-t border-white/10 pt-2">
                              <p className="text-xs text-ink-muted">{node.context || "No context provided."}</p>
                              {(node.source_type || node.source_ref) && (
                                <p className="text-xs text-ink-faint">
                                  Source: {node.source_type ?? ""}{node.source_ref ? ` \u2014 ${node.source_ref}` : ""}
                                </p>
                              )}
                            </div>
                          )}
                        </button>
                      );
                    })}
                  </div>
                )}
              </section>
            );
          })
        )
      )}

      {/* Empty state */}
      {totalLessonCount === 0 && (
        <div className="animate-reveal glass rounded-xl p-8 text-center">
          <p className="text-lg font-headline font-bold text-ink">Your constellation begins here</p>
          <p className="mt-2 text-sm text-ink-muted">
            As you chat with your assistant, lessons and insights will be discovered and added automatically.
          </p>
        </div>
      )}
    </div>
  );
}
