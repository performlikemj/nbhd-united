"use client";

import Link from "next/link";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { SectionCard } from "@/components/section-card";
import { fetchConstellation, fetchPendingLessons } from "@/lib/api";
import { ConstellationData, ConstellationNode } from "@/lib/types";

type ViewMode = "constellation" | "list";

const VIEW_MODE_KEY = "constellationViewMode";
const MOBILE_BREAKPOINT = 768;
const CLUSTER_THRESHOLD = 5;
const NODE_PADDING = 60;

function formatDate(dateString: string): string {
  const date = new Date(dateString);
  if (Number.isNaN(date.getTime())) {
    return dateString;
  }

  return date.toLocaleDateString(undefined, {
    month: "short",
    day: "numeric",
    year: new Date().getFullYear() === date.getFullYear() ? undefined : "numeric",
  });
}

function clusterNodeColor(id: number | null): string {
  if (id == null) {
    return "#5fbaaf";
  }
  const hue = (id * 42) % 360;
  return `hsl(${hue}, 65%, 52%)`;
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
  if (node.tags.length > 0) {
    return node.tags.slice(0, 2).join(" / ");
  }
  const words = node.text.split(/\s+/).slice(0, 5).join(" ");
  return words.length < node.text.length ? words + "\u2026" : words;
}

function loadStoredViewMode(): ViewMode | null {
  if (typeof window === "undefined") {
    return null;
  }
  const raw = window.localStorage.getItem(VIEW_MODE_KEY);
  if (raw === "constellation" || raw === "list") {
    return raw;
  }
  // Migrate old values
  if (raw === "graph") return "constellation";
  if (raw === "cards") return "list";
  return null;
}

function defaultViewMode(): ViewMode {
  if (typeof window === "undefined") {
    return "list";
  }
  return window.innerWidth >= MOBILE_BREAKPOINT ? "constellation" : "list";
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

  const userToggledRef = useRef(false);
  const observerRef = useRef<ResizeObserver | null>(null);

  // Ref callback — attaches ResizeObserver when the container div mounts/unmounts
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

  // Container measurement is handled by the containerRef callback above

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

  // Compute pixel positions for SVG constellation
  const positionedNodes = useMemo(() => {
    const hasPositions = filteredNodes.some((n) => n.x != null && n.y != null);
    const count = filteredNodes.length;

    return filteredNodes.map((node, i) => {
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

      // Map [-1, 1] to pixel coordinates within container
      const px = containerSize.width > 0
        ? NODE_PADDING + ((nx + 1) / 2) * (containerSize.width - 2 * NODE_PADDING)
        : 0;
      const py = containerSize.height > 0
        ? NODE_PADDING + ((ny + 1) / 2) * (containerSize.height - 2 * NODE_PADDING)
        : 0;

      return { ...node, px, py };
    });
  }, [filteredNodes, containerSize]);

  const nodePositions = useMemo(() => {
    const map = new Map<number, { px: number; py: number }>();
    positionedNodes.forEach((node) => map.set(node.id, { px: node.px, py: node.py }));
    return map;
  }, [positionedNodes]);

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

  // Container height based on lesson count
  const containerHeight = useMemo(() => {
    const count = filteredNodes.length;
    if (count === 0) return 200;
    if (count <= 4) return 320;
    if (count <= 20) return 420;
    return Math.min(600, 420 + (count - 20) * 5);
  }, [filteredNodes.length]);

  if (error) {
    return (
      <SectionCard title="Constellation" subtitle="A visual learning map of approved lessons">
        <div className="rounded-panel border border-rose-border bg-rose-bg px-3 py-2 text-sm text-rose-text">{error}</div>
      </SectionCard>
    );
  }

  if (loading) {
    return (
      <SectionCard title="Constellation" subtitle="Loading your learning graph...">
        Loading...
      </SectionCard>
    );
  }

  const progressPercent = Math.min(100, (totalLessonCount / CLUSTER_THRESHOLD) * 100);

  return (
    <div className="grid gap-4 md:grid-cols-[minmax(0,1fr)_320px]">
      <main className="space-y-4 min-w-0">
        {/* Progress banner */}
        {isSparse && totalLessonCount > 0 ? (
          <div className="animate-reveal rounded-panel border border-border bg-surface p-4">
            <div className="flex items-center justify-between gap-3">
              <p className="text-sm text-ink">
                <span className="font-semibold">{totalLessonCount}</span>
                {" of "}
                <span className="font-semibold">{CLUSTER_THRESHOLD}</span>
                {" lessons"}
              </p>
              <span className="text-xs text-ink-faint">Clusters form at {CLUSTER_THRESHOLD}</span>
            </div>
            <div className="mt-2 h-1 w-full overflow-hidden rounded-full bg-border">
              <div
                className="h-full rounded-full transition-all duration-500"
                style={{ width: `${progressPercent}%`, backgroundColor: "var(--signal)" }}
              />
            </div>
            <p className="mt-2 text-xs text-ink-faint">
              Keep chatting with your assistant. Lessons appear here as they&apos;re discovered.
            </p>
          </div>
        ) : null}

        <SectionCard title="Lessons Constellation" subtitle="Navigate how your approved lessons connect over time">
          <div className="mb-4 flex flex-wrap items-center justify-between gap-3">
            <div className="inline-flex rounded-full border border-border bg-surface p-1">
              <button
                type="button"
                onClick={() => handleSetViewMode("constellation")}
                className={`rounded-full px-3 py-1.5 text-xs font-medium transition ${
                  viewMode === "constellation" ? "bg-accent text-white" : "text-ink-muted hover:text-ink"
                }`}
              >
                Constellation
              </button>
              <button
                type="button"
                onClick={() => handleSetViewMode("list")}
                className={`rounded-full px-3 py-1.5 text-xs font-medium transition ${
                  viewMode === "list" ? "bg-accent text-white" : "text-ink-muted hover:text-ink"
                }`}
              >
                List
              </button>
            </div>
            {pendingCount > 0 ? (
              <Link
                href="/constellation/pending"
                className="rounded-full border border-border bg-accent/10 px-3 py-1.5 text-xs font-semibold text-accent"
              >
                {pendingCount === 1 ? "1 lesson waiting" : `${pendingCount} lessons waiting`}
              </Link>
            ) : null}
          </div>

          {viewMode === "constellation" ? (
            <>
              {/* Empty state */}
              {filteredNodes.length === 0 ? (
                <div className="flex items-center justify-center rounded-panel border border-border bg-surface p-8 text-center" style={{ height: 200 }}>
                  <div>
                    <p className="text-lg font-display text-ink">Your constellation begins here</p>
                    <p className="mt-2 text-sm text-ink-muted">
                      As you chat with your assistant, lessons will appear as stars in your personal sky.
                    </p>
                  </div>
                </div>
              ) : (
                /* SVG Constellation View */
                <div
                  ref={containerRef}
                  className="relative overflow-hidden rounded-panel border border-border bg-surface"
                  style={{ height: containerHeight }}
                >
                  {/* Wait for container to be measured before rendering nodes */}
                  {containerSize.width === 0 ? null : <>
                  {/* Connection lines (SVG layer) */}
                  <svg className="pointer-events-none absolute inset-0 h-full w-full" style={{ zIndex: 0 }}>
                    {allEdges.map((edge, i) => {
                      const sourcePos = nodePositions.get(Number(edge.source));
                      const targetPos = nodePositions.get(Number(edge.target));
                      if (!sourcePos || !targetPos) return null;

                      const sim = edge.similarity ?? 0;
                      let stroke: string;
                      let strokeWidth: number;
                      let strokeDasharray: string | undefined;

                      if (sim >= 0.75) {
                        stroke = "rgba(95, 186, 175, 0.7)";
                        strokeWidth = 2;
                      } else if (sim >= 0.5) {
                        stroke = "rgba(95, 186, 175, 0.35)";
                        strokeWidth = 1;
                      } else {
                        stroke = "rgba(95, 186, 175, 0.3)";
                        strokeWidth = 1;
                        strokeDasharray = "4 6";
                      }

                      return (
                        <line
                          key={`${edge.source}-${edge.target}-${i}`}
                          x1={sourcePos.px}
                          y1={sourcePos.py}
                          x2={targetPos.px}
                          y2={targetPos.py}
                          stroke={stroke}
                          strokeWidth={strokeWidth}
                          strokeDasharray={strokeDasharray}
                        />
                      );
                    })}
                  </svg>

                  {/* Node layer */}
                  {positionedNodes.map((node) => {
                    const isSelected = selectedNodeId === node.id;
                    const color = clusterNodeColor(node.cluster_id);

                    return (
                      <button
                        key={node.id}
                        type="button"
                        className="absolute -translate-x-1/2 -translate-y-1/2 flex flex-col items-center gap-1.5 group focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent focus-visible:ring-offset-2"
                        style={{ left: node.px, top: node.py, zIndex: isSelected ? 3 : 1 }}
                        aria-label={`Lesson: ${lessonTitle(node)}`}
                        onClick={() => setSelectedNodeId((prev) => (prev === node.id ? null : node.id))}
                      >
                        <div
                          className={`h-9 w-9 rounded-full transition-transform duration-200 group-hover:scale-125 ${
                            isSelected ? "scale-125 ring-2 ring-accent ring-offset-2" : ""
                          }`}
                          style={{
                            backgroundColor: color,
                            boxShadow: `0 0 16px 5px ${color}40`,
                            animation: "constellation-breathe 4s ease-in-out infinite",
                            animationDelay: `${(node.id * 700) % 4000}ms`,
                          }}
                        />
                        <span className="max-w-[180px] truncate rounded-full bg-surface/92 px-2.5 py-0.5 text-center text-xs font-medium text-ink-muted shadow-sm">
                          {lessonTitle(node)}
                        </span>
                      </button>
                    );
                  })}

                  {/* Detail panel — positioned near selected node */}
                  {selectedNode && nodePositions.get(selectedNode.id) ? (() => {
                    const pos = nodePositions.get(selectedNode.id)!;
                    const rightHalf = pos.px > containerSize.width / 2;
                    const panelStyle: React.CSSProperties = {
                      position: "absolute",
                      top: Math.max(8, Math.min(pos.py - 60, containerHeight - 240)),
                      zIndex: 10,
                      ...(rightHalf
                        ? { right: Math.max(8, containerSize.width - pos.px + 30) }
                        : { left: Math.max(8, pos.px + 30) }),
                    };

                    return (
                      <div
                        className="w-64 rounded-panel border border-border bg-surface/95 p-3 shadow-lg animate-reveal"
                        style={panelStyle}
                      >
                        <p className="text-xs font-semibold uppercase tracking-wide text-signal-text">
                          {lessonTitle(selectedNode)}
                        </p>
                        <p className="mt-1.5 text-sm leading-relaxed text-ink">{selectedNode.text}</p>
                        <p className="mt-2 text-xs text-ink-muted">
                          {selectedNode.context || `Source: ${selectedNode.source_type || "journal"}`}
                        </p>
                        <p className="mt-1 text-xs text-ink-faint">{formatDate(selectedNode.created_at)}</p>
                        {selectedNode.tags.length > 0 ? (
                          <div className="mt-2 flex flex-wrap gap-1">
                            {selectedNode.tags.map((tag) => (
                              <span
                                key={`${selectedNode.id}-${tag}`}
                                className="rounded-full border border-border bg-surface px-2 py-0.5 text-[11px] text-ink-muted"
                              >
                                {tag}
                              </span>
                            ))}
                          </div>
                        ) : null}
                        <button
                          type="button"
                          onClick={() => setSelectedNodeId(null)}
                          className="mt-2 text-xs text-ink-faint hover:text-ink-muted"
                        >
                          Close
                        </button>
                      </div>
                    );
                  })() : null}
                  </>}
                </div>
              )}
            </>
          ) : (
            /* List View */
            <div className="space-y-4">
              {/* Sparse mode: flat cards */}
              {isSparse && filteredNodes.length > 0 ? (
                <div className="space-y-3">
                  {filteredNodes.map((node, index) => {
                    const isExpanded = selectedNodeId === node.id;
                    const title = lessonTitle(node);

                    return (
                      <button
                        type="button"
                        key={node.id}
                        onClick={() => setSelectedNodeId((prev) => (prev === node.id ? null : node.id))}
                        className={`animate-reveal w-full rounded-panel border border-border bg-surface p-3 text-left transition ${
                          isExpanded ? "border-accent/60" : ""
                        } active:bg-surface-hover`}
                        style={{ animationDelay: `${index * 80}ms` }}
                      >
                        <p className="text-xs font-semibold uppercase tracking-wide text-signal-text">{title}</p>
                        <p className="mt-1 text-sm leading-relaxed text-ink">{node.text}</p>
                        <p className="mt-1 text-xs text-ink-faint">{formatDate(node.created_at)}</p>
                        {node.tags.length > 0 ? (
                          <div className="mt-2 flex flex-wrap gap-1">
                            {node.tags.map((tag) => (
                              <span key={`${node.id}-${tag}`} className="rounded-full border border-border bg-surface px-2 py-0.5 text-[11px] text-ink-muted">
                                {tag}
                              </span>
                            ))}
                          </div>
                        ) : null}
                        {isExpanded ? (
                          <div className="mt-3 space-y-2 border-t border-border pt-2">
                            <p className="text-xs text-ink-muted">{node.context || "No context provided."}</p>
                            {(node.source_type || node.source_ref) ? (
                              <p className="text-xs text-ink-faint">
                                Source: {node.source_type ?? ""}{node.source_ref ? ` \u2014 ${node.source_ref}` : ""}
                              </p>
                            ) : null}
                          </div>
                        ) : null}
                      </button>
                    );
                  })}
                </div>
              ) : null}

              {/* Normal mode: cluster-grouped cards */}
              {!isSparse ? (
                clusterCards.length === 0 ? (
                  <p className="rounded-panel border border-border bg-surface p-4 text-sm text-ink-muted">No approved lessons match your current search yet.</p>
                ) : (
                  clusterCards.map((cluster) => {
                    const isCollapsed = isClusterCollapsed(cluster.id);
                    return (
                      <section key={String(cluster.id)} className="space-y-2">
                        <button
                          type="button"
                          onClick={() => handleToggleCluster(cluster.id)}
                          className="flex w-full items-center justify-between rounded-panel border border-border bg-surface px-3 py-2 text-left"
                        >
                          <div className="flex min-w-0 items-center gap-2">
                            <span className="inline-block h-2.5 w-2.5 shrink-0 rounded-full" style={{ backgroundColor: clusterNodeColor(cluster.id) }} />
                            <div className="min-w-0">
                              <p className="truncate text-sm font-semibold text-ink">{cluster.label}</p>
                              <p className="text-xs text-ink-muted">{cluster.count} lessons</p>
                            </div>
                          </div>
                          <span className="text-xs text-ink-muted">{isCollapsed ? "\u25B8" : "\u25BE"}</span>
                        </button>
                        {!isCollapsed ? (
                          <div className="space-y-2">
                            {cluster.nodes.map((node) => {
                              const isExpanded = selectedNodeId === node.id;
                              return (
                                <button
                                  type="button"
                                  key={node.id}
                                  onClick={() => setSelectedNodeId((prev) => (prev === node.id ? null : node.id))}
                                  className={`w-full rounded-panel border border-border bg-surface p-3 text-left transition ${isExpanded ? "border-accent/60" : ""} active:bg-surface-hover`}
                                >
                                  <p className="text-sm font-medium leading-relaxed text-ink">{node.text}</p>
                                  <p className="mt-1 text-xs text-ink-faint">{formatDate(node.created_at)}</p>
                                  {node.tags.length > 0 ? (
                                    <div className="mt-2 flex flex-wrap gap-1">
                                      {node.tags.map((tag) => (
                                        <span key={`${node.id}-${tag}`} className="rounded-full border border-border bg-surface px-2 py-0.5 text-[11px] text-ink-muted">{tag}</span>
                                      ))}
                                    </div>
                                  ) : null}
                                  {isExpanded ? (
                                    <div className="mt-3 space-y-2 border-t border-border pt-2">
                                      <p className="text-xs text-ink-muted">{node.context || "No context provided."}</p>
                                      {(node.source_type || node.source_ref) ? (
                                        <p className="text-xs text-ink-faint">
                                          Source: {node.source_type ?? ""}{node.source_ref ? ` \u2014 ${node.source_ref}` : ""}
                                        </p>
                                      ) : null}
                                    </div>
                                  ) : null}
                                </button>
                              );
                            })}
                          </div>
                        ) : null}
                      </section>
                    );
                  })
                )
              ) : null}

              {/* Empty state for list */}
              {totalLessonCount === 0 ? (
                <div className="animate-reveal rounded-panel border border-border bg-surface p-6 text-center">
                  <p className="text-lg font-display text-ink">Your constellation begins here</p>
                  <p className="mt-2 text-sm text-ink-muted">
                    As you chat with your assistant, lessons and insights will be discovered and added automatically.
                  </p>
                </div>
              ) : null}
            </div>
          )}
        </SectionCard>
      </main>

      <aside className="space-y-4 min-w-0">
        <SectionCard title="Filters" subtitle="Search and quick filters">
          <label className="mb-3 block text-sm text-ink-muted">
            <span className="mb-2 block text-xs uppercase tracking-[0.1em] text-ink-faint">Search lessons</span>
            <input
              value={searchText}
              onChange={(event) => setSearchText(event.target.value)}
              placeholder="Filter by text or tag"
              className="w-full rounded-panel border border-border bg-surface px-3 py-2 text-sm text-ink placeholder:text-ink-faint focus:border-accent focus:outline-none focus:ring-1 focus:ring-accent"
            />
          </label>
          <div className="flex items-center justify-between gap-2">
            <span className="text-sm text-ink-muted">Pending approvals</span>
            <Link href="/constellation/pending" className="inline-flex items-center rounded-full border border-border bg-accent/10 px-3 py-1 text-xs font-semibold text-accent">
              {pendingCount} pending
            </Link>
          </div>
        </SectionCard>

        <SectionCard
          title="Cluster List"
          subtitle={isSparse ? "Clusters form as your constellation grows" : "Quick filter by cluster"}
        >
          {isSparse ? (
            <div className="flex items-center gap-3">
              <span className="flex h-8 w-8 shrink-0 items-center justify-center rounded-full" style={{ backgroundColor: "var(--signal)", opacity: 0.15 }}>
                <span style={{ color: "var(--signal-text)" }} className="text-sm" aria-hidden="true">{"\u2726"}</span>
              </span>
              <div>
                <p className="text-sm text-ink">{totalLessonCount} of {CLUSTER_THRESHOLD} lessons</p>
                <p className="text-xs text-ink-muted">Patterns emerge at {CLUSTER_THRESHOLD}</p>
              </div>
            </div>
          ) : clustersForSidebar.length ? (
            <div className="space-y-2">
              {clustersForSidebar.map((cluster) => (
                <button
                  type="button"
                  key={`${cluster.id}-${cluster.label}`}
                  onClick={() => setSelectedClusterId((current) => (current === cluster.id ? null : cluster.id))}
                  className={`flex w-full items-center justify-between rounded-panel border border-border p-2 text-left transition ${
                    selectedClusterId === cluster.id ? "bg-surface-hover" : "bg-surface"
                  }`}
                >
                  <div className="flex items-center gap-2 min-w-0">
                    <span className="inline-block h-2.5 w-2.5 shrink-0 rounded-full" style={{ backgroundColor: clusterNodeColor(cluster.id) }} />
                    <span className="truncate text-sm text-ink">{cluster.label}</span>
                  </div>
                  <span className="text-xs text-ink-muted">{cluster.count}</span>
                </button>
              ))}
            </div>
          ) : (
            <p className="text-sm text-ink-muted">No clusters yet.</p>
          )}
        </SectionCard>
      </aside>

      {/* Breathing animation keyframes */}
      <style jsx global>{`
        @keyframes constellation-breathe {
          0%, 100% { box-shadow: 0 0 8px 2px rgba(95, 186, 175, 0.12); }
          50% { box-shadow: 0 0 18px 6px rgba(95, 186, 175, 0.3); }
        }
        @media (prefers-reduced-motion: reduce) {
          .constellation-breathe { animation: none !important; }
        }
      `}</style>
    </div>
  );
}
