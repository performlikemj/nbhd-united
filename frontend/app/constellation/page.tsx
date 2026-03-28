"use client";

import Link from "next/link";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { fetchConstellation, fetchPendingLessons } from "@/lib/api";
import { ConstellationData, ConstellationNode } from "@/lib/types";

type ViewMode = "constellation" | "list";

const VIEW_MODE_KEY = "constellationViewMode";
const MOBILE_BREAKPOINT = 768;
const CLUSTER_THRESHOLD = 5;
const COLLISION_ITERATIONS = 15;

/** Responsive spacing — scales with container width */
function getSpacing(width: number) {
  const mobile = width < 768;
  return {
    nodePadding: mobile ? 40 : 80,
    minNodeSpacing: mobile ? 60 : 120,
    clusterMinSpacing: mobile ? 90 : 180,
  };
}

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
  return "constellation";
}

/** Push overlapping nodes apart with strong repulsion + cluster separation */
function resolveCollisions(
  nodes: { id: number; px: number; py: number; cluster_id: number | null }[],
  width: number,
  height: number,
  spacing: ReturnType<typeof getSpacing>,
): { id: number; px: number; py: number }[] {
  const result = nodes.map((n) => ({ ...n }));
  const { nodePadding, minNodeSpacing, clusterMinSpacing } = spacing;

  for (let iter = 0; iter < COLLISION_ITERATIONS; iter++) {
    for (let i = 0; i < result.length; i++) {
      for (let j = i + 1; j < result.length; j++) {
        const dx = result[j].px - result[i].px;
        const dy = result[j].py - result[i].py;
        const dist = Math.sqrt(dx * dx + dy * dy) || 1;
        const sameCluster = result[i].cluster_id === result[j].cluster_id;
        const minDist = sameCluster ? minNodeSpacing : clusterMinSpacing;
        if (dist < minDist) {
          const force = ((minDist - dist) / minDist) * minDist * 0.5;
          const nx = (dx / dist) * force;
          const ny = (dy / dist) * force;
          result[i].px -= nx;
          result[i].py -= ny;
          result[j].px += nx;
          result[j].py += ny;
        }
      }
    }
    for (const n of result) {
      n.px = Math.max(nodePadding, Math.min(width - nodePadding, n.px));
      n.py = Math.max(nodePadding, Math.min(height - nodePadding, n.py));
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

  const graphNodeRef = useRef<HTMLDivElement | null>(null);
  const containerRef = useCallback((node: HTMLDivElement | null) => {
    graphNodeRef.current = node;
    if (observerRef.current) {
      observerRef.current.disconnect();
      observerRef.current = null;
    }
    if (node) {
      // Immediate measurement
      const rect = node.getBoundingClientRect();
      if (rect.width > 0 && rect.height > 0) {
        setContainerSize({ width: rect.width, height: rect.height });
      }
      observerRef.current = new ResizeObserver((entries) => {
        const { width, height } = entries[0].contentRect;
        setContainerSize({ width, height });
      });
      observerRef.current.observe(node);
    }
  }, []);

  // Fallback measurement after paint (mobile Safari can delay ResizeObserver)
  useEffect(() => {
    if (containerSize.width > 0) return;
    const timer = setTimeout(() => {
      if (graphNodeRef.current) {
        const rect = graphNodeRef.current.getBoundingClientRect();
        if (rect.width > 0 && rect.height > 0) {
          setContainerSize({ width: rect.width, height: rect.height });
        }
      }
    }, 100);
    return () => clearTimeout(timer);
  }, [containerSize.width, viewMode]);

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

    loadData();

    return () => {
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

  // Responsive spacing based on container width
  const spacing = useMemo(() => getSpacing(containerSize.width), [containerSize.width]);

  // Compute pixel positions with collision avoidance
  // Graph container is a flex child — ResizeObserver gives its actual width
  // (shrinks automatically when desktop panel is open)
  const positionedNodes = useMemo(() => {
    const hasPositions = filteredNodes.some((n) => n.x != null && n.y != null);
    const count = filteredNodes.length;
    const { nodePadding } = spacing;
    const w = containerSize.width;
    const h = containerSize.height;

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

      const px = w > 0 ? nodePadding + ((nx + 1) / 2) * (w - 2 * nodePadding) : 0;
      const py = h > 0 ? nodePadding + ((ny + 1) / 2) * (h - 2 * nodePadding) : 0;

      return { ...node, px, py };
    });

    if (raw.length > 1 && w > 0) {
      const resolved = resolveCollisions(
        raw.map((n) => ({ id: n.id, px: n.px, py: n.py, cluster_id: n.cluster_id })),
        w, h, spacing,
      );
      const posMap = new Map(resolved.map((r) => [r.id, r]));
      return raw.map((n) => {
        const p = posMap.get(n.id);
        return p ? { ...n, px: p.px, py: p.py } : n;
      });
    }

    return raw;
  }, [filteredNodes, containerSize, spacing]);

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

  // Nodes directly connected to the selected node via edges
  const connectedNodeIds = useMemo(() => {
    if (selectedNodeId == null) return new Set<number>();
    const connected = new Set<number>();
    connected.add(selectedNodeId);
    for (const edge of [...data.edges, ...data.affinity_edges]) {
      const src = Number(edge.source);
      const tgt = Number(edge.target);
      if (src === selectedNodeId) connected.add(tgt);
      if (tgt === selectedNodeId) connected.add(src);
    }
    return connected;
  }, [selectedNodeId, data.edges, data.affinity_edges]);

  // Age-based opacity: older lessons are slightly dimmer
  const nodeAgeOpacity = useMemo(() => {
    const map = new Map<number, number>();
    if (filteredNodes.length === 0) return map;
    const now = Date.now();
    const oldest = Math.min(...filteredNodes.map((n) => new Date(n.created_at).getTime()));
    const range = now - oldest || 1;
    for (const node of filteredNodes) {
      const age = now - new Date(node.created_at).getTime();
      // Newest = 1.0, oldest = 0.5
      map.set(node.id, 1 - (age / range) * 0.5);
    }
    return map;
  }, [filteredNodes]);

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

  // Node size based on cluster importance — smaller on mobile
  const isMobile = containerSize.width > 0 && containerSize.width < 768;
  function nodeSize(clusterId: number | null): number {
    const count = clusterSizes.get(clusterId) || 1;
    const scale = isMobile ? 0.7 : 1;
    if (count >= 5) return Math.round(48 * scale);
    if (count >= 3) return Math.round(40 * scale);
    if (count >= 2) return Math.round(32 * scale);
    return Math.round(24 * scale);
  }

  // Ref for auto-scrolling to detail panel on mobile
  const detailRef = useRef<HTMLElement>(null);
  useEffect(() => {
    if (selectedNode && detailRef.current && containerSize.width < MOBILE_BREAKPOINT) {
      detailRef.current.scrollIntoView({ behavior: "smooth", block: "nearest" });
    }
  }, [selectedNode, containerSize.width]);

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
    const graphHeight = Math.max(380, Math.min(520, filteredNodes.length * 55 + 160));

    // Connected nodes count for the detail panel
    const connectedCount = selectedNodeId != null ? connectedNodeIds.size - 1 : 0;

    // Shared detail panel content
    const detailContent = selectedNode ? (
      <div className="space-y-4 p-5">
        <div className="flex items-start justify-between gap-3">
          <span
            className="rounded px-2 py-0.5 text-[10px] font-bold uppercase tracking-[0.12em]"
            style={{ backgroundColor: `${clusterNodeColor(selectedNode.cluster_id)}20`, color: clusterNodeColor(selectedNode.cluster_id) }}
          >
            {lessonTitle(selectedNode)}
          </span>
          <button type="button" onClick={() => setSelectedNodeId(null)} className="shrink-0 rounded-full p-1 text-ink-faint hover:text-ink transition" aria-label="Close">
            <svg viewBox="0 0 20 20" fill="currentColor" className="h-5 w-5"><path fillRule="evenodd" d="M4.293 4.293a1 1 0 011.414 0L10 8.586l4.293-4.293a1 1 0 111.414 1.414L11.414 10l4.293 4.293a1 1 0 01-1.414 1.414L10 11.414l-4.293 4.293a1 1 0 01-1.414-1.414L8.586 10 4.293 5.707a1 1 0 010-1.414z" clipRule="evenodd"/></svg>
          </button>
        </div>

        {/* Connected lessons with strength */}
        {connectedCount > 0 && (
          <div>
            <p className="text-[11px] text-accent mb-2">
              Connected to {connectedCount} other lesson{connectedCount !== 1 ? "s" : ""}
            </p>
            <div className="space-y-1.5 max-h-[140px] overflow-y-auto scrollbar-none">
              {[...data.edges, ...data.affinity_edges]
                .filter((e) => Number(e.source) === selectedNodeId || Number(e.target) === selectedNodeId)
                .sort((a, b) => (b.similarity ?? 0) - (a.similarity ?? 0))
                .map((edge) => {
                  const otherId = Number(edge.source) === selectedNodeId ? Number(edge.target) : Number(edge.source);
                  const other = nodesById.get(otherId);
                  if (!other) return null;
                  const sim = edge.similarity ?? 0;
                  const strengthLabel = sim >= 0.75 ? "Strong" : sim >= 0.5 ? "Moderate" : "Weak";
                  return (
                    <button
                      key={`conn-${otherId}`}
                      type="button"
                      onClick={() => setSelectedNodeId(otherId)}
                      className="flex w-full items-center gap-2 rounded-lg p-1.5 text-left transition hover:bg-white/5"
                    >
                      <span
                        className="inline-block h-2 w-2 shrink-0 rounded-full"
                        style={{ backgroundColor: clusterNodeColor(other.cluster_id) }}
                      />
                      <span className="flex-1 truncate text-[11px] text-ink-muted">{lessonTitle(other)}</span>
                      <span className={`shrink-0 text-[9px] font-medium ${sim >= 0.75 ? "text-accent" : sim >= 0.5 ? "text-ink-faint" : "text-ink-faint/60"}`}>
                        {strengthLabel}
                      </span>
                    </button>
                  );
                })}
            </div>
          </div>
        )}

        <p className="text-sm leading-relaxed text-ink">{selectedNode.text}</p>
        {selectedNode.context && <p className="text-xs text-ink-muted">{selectedNode.context}</p>}
        <div className="space-y-1">
          <p className="text-xs text-ink-faint">
            {selectedNode.source_type ? `Extracted from ${selectedNode.source_type}` : "Source: journal"}
            {selectedNode.source_ref ? ` \u2014 ${selectedNode.source_ref}` : ""}
          </p>
          <p className="text-xs text-ink-faint">{formatDate(selectedNode.created_at)}</p>
        </div>

        {/* Tags — clickable to filter */}
        {selectedNode.tags.length > 0 && (
          <div className="flex flex-wrap gap-1.5 pt-3 border-t border-border">
            {selectedNode.tags.map((tag) => (
              <button
                key={`tag-${selectedNode.id}-${tag}`}
                type="button"
                onClick={() => {
                  setSearchText((prev) => prev === tag ? "" : tag);
                  setSelectedNodeId(null);
                }}
                className={`rounded-full border px-2.5 py-0.5 text-[11px] transition ${
                  searchText === tag
                    ? "border-accent bg-accent/10 text-accent"
                    : "border-border bg-surface-elevated text-ink-muted hover:border-accent/30 hover:text-ink"
                }`}
              >
                {tag}
              </button>
            ))}
          </div>
        )}

      </div>
    ) : null;

    return (
      <div className="flex flex-col -mt-4">
        {/* ── Controls bar ── */}
        <div className="px-4 sm:px-6 py-4 flex flex-wrap items-center justify-between gap-3">
          <div>
            <h1 className="font-headline text-xl font-bold text-ink">Lessons Constellation</h1>
            <p className="text-xs text-ink-muted">Navigate how your approved lessons connect</p>
          </div>
          <div className="flex flex-wrap items-center gap-2">
            <div className="inline-flex rounded-xl border border-border bg-surface-elevated p-1">
              <button type="button" onClick={() => handleSetViewMode("constellation")}
                className="rounded-lg px-3 py-1.5 text-xs font-medium bg-accent text-white">
                Constellation
              </button>
              <button type="button" onClick={() => handleSetViewMode("list")}
                className="rounded-lg px-3 py-1.5 text-xs font-medium text-ink-muted hover:text-ink transition">
                List
              </button>
            </div>
            {pendingCount > 0 && (
              <Link href="/constellation/pending"
                className="rounded-full border border-accent/30 bg-accent/10 px-3 py-1.5 text-xs font-semibold text-accent">
                {pendingCount === 1 ? "1 lesson waiting" : `${pendingCount} lessons waiting`}
              </Link>
            )}
            <button type="button" onClick={() => setShowFilters(!showFilters)}
              className="rounded-lg border border-border bg-surface-elevated px-3 py-1.5 text-xs text-ink-muted hover:text-ink transition">
              Filters
            </button>
          </div>
        </div>

        {/* Progress (sparse) */}
        {isSparse && totalLessonCount > 0 && (
          <div className="px-4 sm:px-6 pb-3">
            <div className="rounded-xl border border-border bg-surface-elevated p-3 max-w-md">
              <p className="text-xs text-ink-muted">
                <span className="font-semibold text-ink">{totalLessonCount}</span> of {CLUSTER_THRESHOLD} lessons — clusters form at {CLUSTER_THRESHOLD}
              </p>
              <div className="mt-1.5 h-1 w-full overflow-hidden rounded-full bg-border">
                <div className="h-full rounded-full transition-all" style={{ width: `${progressPercent}%`, backgroundColor: "var(--signal)" }} />
              </div>
            </div>
          </div>
        )}

        {/* ── Graph + Desktop Panel (flex row on md+) ── */}
        <div className="flex flex-col md:flex-row">
          {/* Graph container */}
          <div
            ref={containerRef}
            className="constellation-bg relative w-full overflow-hidden rounded-xl md:flex-1"
            style={{ height: graphHeight }}
          >
            {containerSize.width > 0 && filteredNodes.length > 0 && (
              <>
                {/* Edges — pointer-events on lines for hover tooltips */}
                <svg className="absolute inset-0 h-full w-full" style={{ pointerEvents: "none" }}>
                  <defs>
                    <linearGradient id="edge-grad-pt" x1="0%" y1="0%" x2="100%" y2="100%">
                      <stop offset="0%" stopColor="#7C6BF0" /><stop offset="100%" stopColor="#4ECDC4" />
                    </linearGradient>
                    <linearGradient id="edge-grad-tp" x1="0%" y1="0%" x2="100%" y2="100%">
                      <stop offset="0%" stopColor="#4ECDC4" /><stop offset="100%" stopColor="#E8B4B8" />
                    </linearGradient>
                    <linearGradient id="edge-grad-pp" x1="0%" y1="0%" x2="100%" y2="100%">
                      <stop offset="0%" stopColor="#7C6BF0" /><stop offset="100%" stopColor="#E8B4B8" />
                    </linearGradient>
                  </defs>
                  {allEdges.map((edge, i) => {
                    const s = nodePositions.get(Number(edge.source));
                    const t = nodePositions.get(Number(edge.target));
                    if (!s || !t) return null;
                    const sim = edge.similarity ?? 0;
                    const grads = ["url(#edge-grad-pt)", "url(#edge-grad-tp)", "url(#edge-grad-pp)"];
                    // Highlight edges connected to selected node, dim others
                    const isConnectedEdge = selectedNodeId != null && (
                      Number(edge.source) === selectedNodeId || Number(edge.target) === selectedNodeId
                    );
                    const dimmed = selectedNodeId != null && !isConnectedEdge;
                    const baseOpacity = sim >= 0.75 ? 0.45 : sim >= 0.5 ? 0.25 : 0.12;
                    const srcNode = nodesById.get(Number(edge.source));
                    const tgtNode = nodesById.get(Number(edge.target));
                    const strengthLabel = sim >= 0.75 ? "Strong" : sim >= 0.5 ? "Moderate" : "Weak";
                    const sharedTags = srcNode && tgtNode
                      ? srcNode.tags.filter((t) => tgtNode.tags.includes(t))
                      : [];
                    const tooltipText = `${strengthLabel} connection (${Math.round(sim * 100)}%)${sharedTags.length > 0 ? `\nShared themes: ${sharedTags.join(", ")}` : ""}`;
                    return (
                      <line key={`e-${edge.source}-${edge.target}-${i}`}
                        x1={s.px} y1={s.py} x2={t.px} y2={t.py}
                        stroke={grads[i % grads.length]}
                        strokeWidth={isConnectedEdge ? Math.max(2, sim >= 0.75 ? 3 : 2) : (sim >= 0.75 ? 2 : sim >= 0.5 ? 1.5 : 1)}
                        strokeDasharray={sim < 0.5 && !isConnectedEdge ? "4 6" : undefined}
                        opacity={dimmed ? 0.04 : isConnectedEdge ? 0.7 : baseOpacity}
                        className="transition-opacity duration-300"
                        style={{ pointerEvents: "stroke" }}
                      >
                        <title>{tooltipText}</title>
                      </line>
                    );
                  })}
                </svg>

                {/* Cluster labels (desktop only) */}
                {Array.from(clusterAnchors.entries()).map(([cid, anchor]) => {
                  const label = getClusterLabel(cid, data.clusters);
                  const short = label.length > 18 ? label.slice(0, 18) + "\u2026" : label;
                  return (
                    <div key={`cl-${cid}`}
                      className="pointer-events-none absolute -translate-x-1/2 max-w-[140px] text-center hidden md:block"
                      style={{ left: anchor.px, top: anchor.py - nodeSize(cid) / 2 - 20 }}>
                      <span className="text-[9px] font-headline font-semibold uppercase tracking-[0.08em] opacity-40"
                        style={{ color: clusterNodeColor(cid) }}>{short}</span>
                    </div>
                  );
                })}

                {/* Nodes */}
                {positionedNodes.map((node) => {
                  const sel = selectedNodeId === node.id;
                  const isConnected = connectedNodeIds.has(node.id);
                  const dimmed = selectedNodeId != null && !isConnected;
                  const color = clusterNodeColor(node.cluster_id);
                  const size = nodeSize(node.cluster_id);
                  const ageScale = nodeAgeOpacity.get(node.id) ?? 1;
                  // Older nodes slightly smaller (90-100% of base size)
                  const timeSize = Math.round(size * (0.9 + ageScale * 0.1));
                  return (
                    <button key={node.id} type="button"
                      className="absolute -translate-x-1/2 -translate-y-1/2 focus-visible:outline-none group transition-opacity duration-300"
                      style={{
                        left: node.px, top: node.py,
                        zIndex: sel ? 3 : isConnected ? 2 : 1,
                        width: timeSize + 8, height: timeSize + 8,
                        opacity: dimmed ? 0.15 : ageScale,
                      }}
                      aria-label={`Lesson: ${lessonTitle(node)}`}
                      onClick={() => setSelectedNodeId((p) => (p === node.id ? null : node.id))}>
                      <div className={`mx-auto rounded-full transition-transform duration-200 group-hover:scale-110 ${sel ? "scale-110 ring-2 ring-white/50 ring-offset-2 ring-offset-c-dark" : ""}`}
                        style={{ width: timeSize, height: timeSize, backgroundColor: color,
                          filter: `drop-shadow(0 0 ${timeSize / 3}px ${color}${dimmed ? "30" : "90"})`,
                          animation: dimmed ? "none" : "constellation-breathe 4s ease-in-out infinite",
                          animationDelay: `${(node.id * 700) % 4000}ms` }} />
                      <div className="pointer-events-none absolute left-1/2 -translate-x-1/2 bottom-full mb-2 opacity-0 group-hover:opacity-100 transition-opacity" style={{ zIndex: 50 }}>
                        <span className="block max-w-[180px] truncate rounded-lg bg-surface border border-border px-2.5 py-1 text-[11px] font-medium text-ink shadow-lg whitespace-nowrap">
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
                <div className="text-center max-w-sm px-4">
                  <p className="text-lg font-headline font-bold text-ink">Your constellation begins here</p>
                  <p className="mt-2 text-sm text-ink-muted">As you chat with your assistant, lessons will appear as stars in your personal sky.</p>
                </div>
              </div>
            )}

            {/* Filters overlay */}
            {showFilters && (
              <div className="absolute right-4 top-4 z-20 rounded-2xl border border-border bg-surface p-5 w-72 max-h-[60vh] overflow-y-auto space-y-4 shadow-xl">
                <div className="flex items-center justify-between">
                  <h3 className="font-headline text-sm font-bold text-ink">Filters</h3>
                  <button type="button" onClick={() => setShowFilters(false)} className="text-ink-faint hover:text-ink text-xs">Close</button>
                </div>
                {allTags.length > 0 && (
                  <div>
                    <h4 className="text-[10px] font-bold uppercase tracking-[0.12em] text-ink-faint mb-2">Tags</h4>
                    <div className="flex flex-wrap gap-1.5">
                      {allTags.map((tag) => (
                        <button key={tag} type="button"
                          onClick={() => setSearchText((p) => p === tag ? "" : tag)}
                          className={`rounded-full px-2.5 py-1 text-xs transition ${searchText === tag ? "bg-accent text-white" : "border border-border text-ink-muted hover:text-ink"}`}>
                          {tag}
                        </button>
                      ))}
                    </div>
                  </div>
                )}
                {!isSparse && clustersForSidebar.length > 0 && (
                  <div>
                    <h4 className="text-[10px] font-bold uppercase tracking-[0.12em] text-ink-faint mb-2">Clusters</h4>
                    <div className="space-y-1">
                      {clustersForSidebar.map((c) => (
                        <button type="button" key={`f-${c.id}`}
                          onClick={() => setSelectedClusterId((cur) => (cur === c.id ? null : c.id))}
                          className={`flex w-full items-center justify-between rounded-lg p-2 text-left transition ${selectedClusterId === c.id ? "bg-surface-hover" : "hover:bg-surface-hover"}`}>
                          <div className="flex items-center gap-2 min-w-0">
                            <span className="inline-block h-2.5 w-2.5 shrink-0 rounded-full" style={{ backgroundColor: clusterNodeColor(c.id) }} />
                            <span className="truncate text-xs text-ink">{c.label}</span>
                          </div>
                          <span className="text-xs text-ink-faint">{c.count}</span>
                        </button>
                      ))}
                    </div>
                  </div>
                )}
              </div>
            )}
          </div>

          {/* ── Desktop detail panel (flex sibling, solid bg) ── */}
          {selectedNode && (
            <aside className="hidden md:flex w-96 shrink-0 flex-col border-l border-border bg-surface overflow-y-auto" style={{ height: graphHeight }}>
              {detailContent}
            </aside>
          )}
        </div>

        {/* ── Legend (in flow, below graph) ── */}
        {clustersForSidebar.length > 0 && (
          <div className="px-4 sm:px-6 py-3 overflow-x-auto scrollbar-none border-t border-border bg-surface-elevated">
            <div className="flex gap-4 md:flex-wrap whitespace-nowrap">
              {clustersForSidebar.map((cluster) => {
                const short = cluster.label.length > 20 ? cluster.label.slice(0, 20) + "\u2026" : cluster.label;
                return (
                  <button key={`leg-${cluster.id}`} type="button"
                    onClick={() => setSelectedClusterId((c) => (c === cluster.id ? null : cluster.id))}
                    className={`flex shrink-0 items-center gap-1.5 text-[10px] font-bold uppercase tracking-[0.1em] transition ${
                      selectedClusterId === cluster.id ? "text-ink" : "text-ink-faint hover:text-ink-muted"}`}>
                    <span className="inline-block h-2 w-2 shrink-0 rounded-full" style={{ backgroundColor: clusterNodeColor(cluster.id) }} />
                    {short}
                  </button>
                );
              })}
            </div>
          </div>
        )}

        {/* ── Mobile detail panel (in flow, below legend) ── */}
        {selectedNode && (
          <aside ref={detailRef} className="md:hidden border-t border-border bg-surface">
            {detailContent}
          </aside>
        )}

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
