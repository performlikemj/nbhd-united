"use client";

import dynamic from "next/dynamic";
import Link from "next/link";
import { useCallback, useEffect, useMemo, useState } from "react";

import { SectionCard } from "@/components/section-card";
import { fetchConstellation, fetchPendingLessons } from "@/lib/api";
import { ConstellationData, ConstellationEdge, ConstellationNode } from "@/lib/types";

type ViewMode = "cards" | "graph";

type ConstellationGraphNode = ConstellationNode & {
  context?: string;
  source_type?: string;
  source_ref?: string;
};

type ConstellationGraphData = {
  nodes: ConstellationGraphNode[];
  links: ConstellationEdge[];
};

const VIEW_MODE_KEY = "constellationViewMode";
const MOBILE_BREAKPOINT = 768;

const ForceGraph2D = dynamic(() => import("react-force-graph-2d"), {
  ssr: false,
});

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

function formatLongDate(dateString: string): string {
  const date = new Date(dateString);
  if (Number.isNaN(date.getTime())) {
    return dateString;
  }
  return date.toLocaleString();
}

function clusterLabelColor(id: number | null): string {
  if (id == null) {
    return "#7f8b9c";
  }
  const hue = (id * 42) % 360;
  return `hsl(${hue}, 65%, 52%)`;
}

function getClusterLabel(clusterId: number | null, clusters: ConstellationData["clusters"]): string {
  if (clusterId == null) {
    return "Unclustered";
  }

  const found = clusters.find((cluster) => cluster.id === clusterId);
  return found?.label || `Cluster ${clusterId}`;
}

function getRelatedLessons(
  nodeId: number,
  edges: ConstellationEdge[],
  nodesById: Map<number, ConstellationGraphNode>,
): ConstellationGraphNode[] {
  const related: ConstellationGraphNode[] = [];

  for (const edge of edges) {
    const sourceId = Number(edge.source);
    const targetId = Number(edge.target);
    const otherId = sourceId === nodeId ? targetId : targetId === nodeId ? sourceId : null;

    if (otherId == null) {
      continue;
    }

    const relatedNode = nodesById.get(otherId);
    if (relatedNode && !related.some((item) => item.id === relatedNode.id)) {
      related.push(relatedNode);
    }
  }

  return related;
}

function loadStoredViewMode(): ViewMode | null {
  if (typeof window === "undefined") {
    return null;
  }

  const raw = window.localStorage.getItem(VIEW_MODE_KEY);
  if (raw === "cards" || raw === "graph") {
    return raw;
  }

  return null;
}

function defaultViewMode(): ViewMode {
  if (typeof window === "undefined") {
    return "cards";
  }

  return window.innerWidth >= MOBILE_BREAKPOINT ? "graph" : "cards";
}

export default function ConstellationPage() {
  const [data, setData] = useState<ConstellationData>({ nodes: [], edges: [], clusters: [] });
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");

  const [searchText, setSearchText] = useState("");
  const [selectedClusterId, setSelectedClusterId] = useState<number | null>(null);
  const [hoveredNode, setHoveredNode] = useState<ConstellationGraphNode | null>(null);
  const [selectedNode, setSelectedNode] = useState<ConstellationGraphNode | null>(null);
  const [selectedCardNodeId, setSelectedCardNodeId] = useState<number | null>(null);
  const [pendingCount, setPendingCount] = useState(0);
  const [viewMode, setViewMode] = useState<ViewMode>("cards");
  const [collapsedClusters, setCollapsedClusters] = useState<Set<string>>(new Set());

  useEffect(() => {
    let mounted = true;

    async function loadData() {
      try {
        const [constellationData, pendingLessons] = await Promise.all([
          fetchConstellation(),
          fetchPendingLessons(),
        ]);

        if (!mounted) {
          return;
        }

        setData(constellationData);
        setPendingCount(pendingLessons.length);
      } catch (err) {
        if (!mounted) {
          return;
        }
        setError(err instanceof Error ? err.message : "Failed to load constellation.");
      } finally {
        if (mounted) {
          setLoading(false);
        }
      }
    }

    const storedMode = loadStoredViewMode();
    setViewMode(storedMode ?? defaultViewMode());

    const mediaQuery = window.matchMedia(`(min-width: ${MOBILE_BREAKPOINT}px)`);
    const handleMediaChange = () => {
      const persisted = loadStoredViewMode();
      if (!persisted) {
        setViewMode(window.innerWidth >= MOBILE_BREAKPOINT ? "graph" : "cards");
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

  const filteredNodes = useMemo<ConstellationGraphNode[]>(() => {
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

  const filteredGraphData = useMemo<ConstellationGraphData>(() => {
    return {
      nodes: filteredNodes,
      links: data.edges.filter((edge) => {
        return filteredNodeIds.has(Number(edge.source)) && filteredNodeIds.has(Number(edge.target));
      }),
    };
  }, [data.edges, filteredNodeIds, filteredNodes]);

  const nodesById = useMemo(() => {
    const map = new Map<number, ConstellationGraphNode>();
    data.nodes.forEach((node) => map.set(Number(node.id), node));
    return map;
  }, [data.nodes]);

  const selectedCardNode = useMemo(() => {
    if (selectedCardNodeId == null) return null;
    return nodesById.get(selectedCardNodeId) ?? null;
  }, [nodesById, selectedCardNodeId]);

  const relatedLessons = useMemo(() => {
    if (!selectedCardNode) return [];
    return getRelatedLessons(selectedCardNode.id, data.edges, nodesById);
  }, [data.edges, nodesById, selectedCardNode]);

  const clusterCards = useMemo(() => {
    const groups = new Map<number | null, ConstellationGraphNode[]>();

    filteredNodes.forEach((node) => {
      const key = node.cluster_id;
      const existing = groups.get(key);
      if (existing) {
        existing.push(node);
      } else {
        groups.set(key, [node]);
      }
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
      items.push({ id: null, label: "Unclustered", count: unclusteredCount });
    }

    return items.filter((item) => item.count > 0);
  }, [data.clusters, filteredNodes]);

  const handleToggleCluster = useCallback((clusterId: number | null) => {
    const key = String(clusterId);
    setCollapsedClusters((previous) => {
      const next = new Set(previous);
      if (next.has(key)) {
        next.delete(key);
      } else {
        next.add(key);
      }
      return next;
    });
  }, []);

  const isClusterCollapsed = useCallback(
    (clusterId: number | null) => collapsedClusters.has(String(clusterId)),
    [collapsedClusters],
  );

  const handleSetViewMode = (mode: ViewMode) => {
    setViewMode(mode);
  };

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

  return (
    <div className="grid gap-4 md:grid-cols-[minmax(0,1fr)_320px]">
      <main className="space-y-4 min-w-0">
        <SectionCard title="Lessons Constellation" subtitle="Navigate how your approved lessons connect over time">
          <div className="mb-4 flex flex-wrap items-center justify-between gap-3">
            <div className="inline-flex rounded-full border border-border bg-surface p-1">
              <button
                type="button"
                onClick={() => handleSetViewMode("cards")}
                className={`rounded-full px-3 py-1.5 text-xs font-medium transition ${
                  viewMode === "cards" ? "bg-accent text-white" : "text-ink-muted hover:text-ink"
                }`}
              >
                Cards
              </button>
              <button
                type="button"
                onClick={() => handleSetViewMode("graph")}
                className={`rounded-full px-3 py-1.5 text-xs font-medium transition ${
                  viewMode === "graph" ? "bg-accent text-white" : "text-ink-muted hover:text-ink"
                }`}
              >
                Graph
              </button>
            </div>
            <Link
              href="/constellation/pending"
              className="rounded-full border border-border bg-accent/10 px-3 py-1.5 text-xs font-semibold text-accent"
            >
              {`You have ${pendingCount} lessons waiting for approval`}
            </Link>
          </div>

          {viewMode === "graph" ? (
            <div className="relative h-[64vh] min-h-[360px] rounded-panel border border-border bg-surface">
              <ForceGraph2D
                graphData={filteredGraphData as unknown as ConstellationGraphData}
                nodeId="id"
                nodeLabel={(node) => (node as ConstellationGraphNode).text}
                nodeColor={(node) => clusterLabelColor((node as ConstellationGraphNode).cluster_id)}
                nodeVal={1}
                nodeRelSize={7}
                backgroundColor="transparent"
                linkCurvature={0}
                linkDirectionalArrowLength={0}
                linkWidth={() => 1}
                linkColor={(link) => {
                  const similarity = Number((link as { similarity?: number }).similarity ?? 0);
                  const opacity = Math.max(0.08, Math.min(0.9, Number(similarity) || 0.2));
                  return `rgba(120, 130, 142, ${opacity})`;
                }}
                onNodeClick={(node: unknown) => setSelectedNode(node as ConstellationGraphNode)}
                onNodeHover={(node: unknown) =>
                  setHoveredNode((node as ConstellationGraphNode | null) ?? null)
                }
                cooldownTicks={120}
                enableZoomInteraction
                enableNodeDrag={false}
                d3AlphaDecay={0.025}
                d3VelocityDecay={0.32}
              />

              {hoveredNode ? (
                <div className="pointer-events-none absolute left-3 top-3 max-w-sm rounded-panel border border-border bg-surface/95 px-3 py-2 text-sm text-ink shadow-lg">
                  {hoveredNode.text}
                </div>
              ) : null}

              {selectedNode ? (
                <div className="absolute right-3 top-3 z-10 w-72 max-w-[85%] rounded-panel border border-border bg-surface/95 p-3 shadow-lg md:right-4 md:top-4">
                  <h3 className="text-sm font-semibold text-ink">Selected Lesson</h3>
                  <p className="mt-2 text-sm text-ink">{selectedNode.text}</p>
                  <p className="mt-2 text-xs text-ink-muted">{selectedNode.context || "No context"}</p>
                  <p className="mt-2 text-xs text-ink-faint">Date: {formatLongDate(selectedNode.created_at)}</p>
                  {(selectedNode.source_type || selectedNode.source_ref) ? (
                    <p className="mt-2 text-xs text-ink-faint">
                      Source: {selectedNode.source_type ?? ""}
                      {selectedNode.source_ref ? ` — ${selectedNode.source_ref}` : ""}
                    </p>
                  ) : null}
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
                  <button
                    type="button"
                    onClick={() => setSelectedNode(null)}
                    className="mt-3 rounded-full border border-border px-3 py-1.5 text-xs text-ink-muted transition hover:border-border-strong"
                  >
                    Close
                  </button>
                </div>
              ) : null}

              {!filteredGraphData.nodes.length ? (
                <div className="absolute inset-0 flex items-center justify-center p-6 text-center text-sm text-ink-muted">
                  No approved lessons match your current search yet.
                </div>
              ) : null}
            </div>
          ) : (
            <div className="space-y-4">
              {clusterCards.length === 0 ? (
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
                          <span
                            className="inline-block h-2.5 w-2.5 shrink-0 rounded-full"
                            style={{ backgroundColor: clusterLabelColor(cluster.id) }}
                          />
                          <div className="min-w-0">
                            <p className="truncate text-sm font-semibold text-ink">{cluster.label}</p>
                            <p className="text-xs text-ink-muted">{cluster.count} lessons</p>
                          </div>
                        </div>
                        <span className="text-xs text-ink-muted">{isCollapsed ? "▸" : "▾"}</span>
                      </button>

                      {!isCollapsed ? (
                        <div className="space-y-2">
                          {cluster.nodes.map((node) => {
                            const isExpanded = selectedCardNodeId === node.id;

                            return (
                              <button
                                type="button"
                                key={node.id}
                                onClick={() =>
                                  setSelectedCardNodeId((previous) => (previous === node.id ? null : node.id))
                                }
                                className={`w-full rounded-panel border border-border bg-surface p-3 text-left transition ${
                                  isExpanded ? "border-accent/60" : ""
                                } active:bg-surface-hover`}
                              >
                                <p className="text-sm font-medium leading-relaxed text-ink">{node.text}</p>
                                <p className="mt-1 text-xs text-ink-faint">{formatDate(node.created_at)}</p>
                                {node.tags.length ? (
                                  <div className="mt-2 flex flex-wrap gap-1">
                                    {node.tags.map((tag) => (
                                      <span
                                        key={`${node.id}-${tag}`}
                                        className="rounded-full border border-border bg-surface px-2 py-0.5 text-[11px] text-ink-muted"
                                      >
                                        {tag}
                                      </span>
                                    ))}
                                  </div>
                                ) : null}

                                {isExpanded ? (
                                  <div className="mt-3 space-y-2 border-t border-border pt-2 text-left">
                                    <p className="text-xs text-ink-muted">{node.context || "No context provided."}</p>
                                    {(node.source_type || node.source_ref) ? (
                                      <p className="text-xs text-ink-faint">
                                        Source: {node.source_type ?? ""}
                                        {node.source_ref ? ` — ${node.source_ref}` : ""}
                                      </p>
                                    ) : null}
                                    <div>
                                      <p className="text-xs font-semibold text-ink">Related lessons</p>
                                      <div className="mt-1 flex flex-wrap gap-1">
                                        {(selectedCardNode?.id === node.id ? relatedLessons : getRelatedLessons(node.id, data.edges, nodesById))
                                          .slice(0, 4)
                                          .map((relatedLesson) => (
                                            <span
                                              key={relatedLesson.id}
                                              className="rounded-full border border-border px-2 py-0.5 text-[11px] text-ink-muted"
                                            >
                                              {relatedLesson.text}
                                            </span>
                                          ))}
                                      </div>
                                    </div>
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
              )}
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
            <Link
              href="/constellation/pending"
              className="inline-flex items-center rounded-full border border-border bg-accent/10 px-3 py-1 text-xs font-semibold text-accent"
            >
              {pendingCount} pending
            </Link>
          </div>
        </SectionCard>

        <SectionCard title="Cluster List" subtitle="Quick filter by cluster">
          {clustersForSidebar.length ? (
            <div className="space-y-2">
              {clustersForSidebar.map((cluster) => (
                <button
                  type="button"
                  key={`${cluster.id}-${cluster.label}`}
                  onClick={() =>
                    setSelectedClusterId((current) => (current === cluster.id ? null : cluster.id))
                  }
                  className={`flex w-full items-center justify-between rounded-panel border border-border p-2 text-left transition ${
                    selectedClusterId === cluster.id ? "bg-surface-hover" : "bg-surface"
                  }`}
                >
                  <div className="flex items-center gap-2 min-w-0">
                    <span
                      className="inline-block h-2.5 w-2.5 shrink-0 rounded-full"
                      style={{ backgroundColor: clusterLabelColor(cluster.id) }}
                    />
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
    </div>
  );
}
