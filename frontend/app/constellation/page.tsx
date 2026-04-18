"use client";

import Link from "next/link";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { fetchConstellation, fetchPendingLessons } from "@/lib/api";
import { ConstellationData, ConstellationEdge, ConstellationNode } from "@/lib/types";

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
  if (id == null) return "#64748B";
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

/**
 * Client-side tag-based clustering fallback.
 * When the API returns no clusters, groups nodes by their most prominent tags
 * so the constellation still shows meaningful visual differentiation.
 */
function clusterByTags(nodes: ConstellationNode[]): {
  clusters: ConstellationData["clusters"];
  clusterMap: Map<number, number>;
} {
  if (nodes.length === 0) return { clusters: [], clusterMap: new Map() };

  // Build tag frequency across all nodes
  const tagCounts = new Map<string, number>();
  for (const n of nodes) {
    for (const t of n.tags) {
      tagCounts.set(t, (tagCounts.get(t) || 0) + 1);
    }
  }

  // Select top tags as cluster seeds (tags shared by >= 2 nodes, up to 8)
  const seedTags = [...tagCounts.entries()]
    .filter(([, count]) => count >= 2)
    .sort((a, b) => b[1] - a[1])
    .slice(0, 8)
    .map(([tag]) => tag);

  // If we have fewer than 2 seed tags, try using all tags with count >= 1
  if (seedTags.length < 2) {
    const allTagsSorted = [...tagCounts.entries()]
      .sort((a, b) => b[1] - a[1])
      .slice(0, 6)
      .map(([tag]) => tag);
    seedTags.length = 0;
    seedTags.push(...allTagsSorted);
  }

  // Assign each node to the cluster of its first matching seed tag
  const clusterMap = new Map<number, number>();
  for (const n of nodes) {
    const matchIdx = n.tags.findIndex((t) => seedTags.includes(t));
    if (matchIdx >= 0) {
      clusterMap.set(n.id, seedTags.indexOf(n.tags[matchIdx]));
    }
    // Nodes with no matching tag left unassigned (cluster_id stays null)
  }

  // For unassigned nodes, try to assign to the cluster of their most connected neighbor
  // (simple: just assign to cluster 0 if we have seeds)
  for (const n of nodes) {
    if (!clusterMap.has(n.id) && seedTags.length > 0) {
      // Assign to a spread of clusters to avoid one mega-cluster
      clusterMap.set(n.id, n.id % seedTags.length);
    }
  }

  // Build cluster objects
  const clusterCounts = new Map<number, number>();
  const clusterTagSets = new Map<number, Set<string>>();
  for (const [nodeId, clusterId] of clusterMap) {
    clusterCounts.set(clusterId, (clusterCounts.get(clusterId) || 0) + 1);
    const node = nodes.find((n) => n.id === nodeId);
    if (node) {
      const tagSet = clusterTagSets.get(clusterId) || new Set<string>();
      node.tags.forEach((t) => tagSet.add(t));
      clusterTagSets.set(clusterId, tagSet);
    }
  }

  const clusters: ConstellationData["clusters"] = seedTags
    .map((tag, i) => ({
      id: i,
      label: tag.charAt(0).toUpperCase() + tag.slice(1).replace(/_/g, " "),
      count: clusterCounts.get(i) || 0,
      tags: [...(clusterTagSets.get(i) || [])].slice(0, 5),
    }))
    .filter((c) => c.count > 0);

  return { clusters, clusterMap };
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

/** Canvas-based starfield with three depth layers */
function Starfield({ width, height }: { width: number; height: number }) {
  const canvasRef = useRef<HTMLCanvasElement>(null);

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas || width <= 0 || height <= 0) return;
    const ctx = canvas.getContext("2d");
    if (!ctx) return;

    const dpr = window.devicePixelRatio || 1;
    canvas.width = width * dpr;
    canvas.height = height * dpr;
    ctx.scale(dpr, dpr);
    ctx.clearRect(0, 0, width, height);

    const layers = [
      { count: 220, rMin: 0.3, rMax: 0.8, aMin: 0.15, aMax: 0.45 },
      { count: 90, rMin: 0.8, rMax: 1.5, aMin: 0.35, aMax: 0.7 },
      { count: 22, rMin: 1.4, rMax: 2.4, aMin: 0.55, aMax: 0.95 },
    ];

    // Deterministic pseudo-random using a simple LCG so stars don't jump on resize
    let seed = 42;
    function rand() {
      seed = (seed * 1664525 + 1013904223) & 0x7fffffff;
      return seed / 0x7fffffff;
    }

    for (const layer of layers) {
      for (let i = 0; i < layer.count; i++) {
        const x = rand() * width;
        const y = rand() * height;
        const r = layer.rMin + rand() * (layer.rMax - layer.rMin);
        const a = layer.aMin + rand() * (layer.aMax - layer.aMin);
        ctx.beginPath();
        ctx.arc(x, y, r, 0, Math.PI * 2);
        ctx.fillStyle = `rgba(226, 232, 240, ${a})`;
        ctx.fill();
      }
    }
  }, [width, height]);

  return (
    <canvas
      ref={canvasRef}
      className="absolute inset-0 pointer-events-none"
      style={{ width, height }}
      aria-hidden="true"
    />
  );
}

/** Compute cluster centroids + bounding radii from positioned pixel coordinates */
function computeClusterBounds(
  nodes: Array<{ px: number; py: number; cluster_id: number | null }>,
): Map<string, { cx: number; cy: number; r: number; count: number }> {
  const groups = new Map<string, Array<{ px: number; py: number }>>();
  for (const n of nodes) {
    const key = String(n.cluster_id ?? "null");
    const arr = groups.get(key) || [];
    arr.push(n);
    groups.set(key, arr);
  }
  const bounds = new Map<string, { cx: number; cy: number; r: number; count: number }>();
  for (const [key, ns] of groups) {
    const xs = ns.map((n) => n.px);
    const ys = ns.map((n) => n.py);
    const cx = (Math.min(...xs) + Math.max(...xs)) / 2;
    const cy = (Math.min(...ys) + Math.max(...ys)) / 2;
    const r = Math.max(60, Math.max(...ns.map((n) => Math.hypot(n.px - cx, n.py - cy))) + 70);
    bounds.set(key, { cx, cy, r, count: ns.length });
  }
  return bounds;
}

/** Detail panel for the V1 Safe constellation view */
function StarDetailPanel({
  node,
  clusters,
  allEdges,
  nodesById,
  onClose,
  onJump,
}: {
  node: ConstellationNode;
  clusters: ConstellationData["clusters"];
  allEdges: ConstellationEdge[];
  nodesById: Map<number, ConstellationNode>;
  onClose: () => void;
  onJump: (id: number) => void;
}) {
  const clusterColor = clusterNodeColor(node.cluster_id);
  const label = getClusterLabel(node.cluster_id, clusters);

  const connections = allEdges
    .filter((e) => Number(e.source) === node.id || Number(e.target) === node.id)
    .map((e) => {
      const otherId = Number(e.source) === node.id ? Number(e.target) : Number(e.source);
      return { other: nodesById.get(otherId), sim: e.similarity ?? 0 };
    })
    .filter((c): c is { other: ConstellationNode; sim: number } => c.other != null)
    .sort((a, b) => b.sim - a.sim);

  return (
    <div className="p-5 space-y-4">
      <div className="flex items-start justify-between">
        <div className="flex items-center gap-2">
          <span
            className="inline-block h-2 w-2 rounded-full shrink-0"
            style={{ backgroundColor: clusterColor, boxShadow: `0 0 8px ${clusterColor}` }}
          />
          <span className="text-[10px] font-headline uppercase tracking-[0.18em] text-c-text-muted">
            {label}
          </span>
        </div>
        <button type="button" onClick={onClose} className="text-c-text-faint hover:text-ink transition" aria-label="Close detail">
          <svg viewBox="0 0 20 20" fill="currentColor" className="h-4 w-4">
            <path fillRule="evenodd" d="M4.3 4.3a1 1 0 011.4 0L10 8.6l4.3-4.3a1 1 0 111.4 1.4L11.4 10l4.3 4.3a1 1 0 01-1.4 1.4L10 11.4l-4.3 4.3a1 1 0 01-1.4-1.4L8.6 10 4.3 5.7a1 1 0 010-1.4z" clipRule="evenodd" />
          </svg>
        </button>
      </div>

      <p className="font-serif italic text-[20px] leading-snug text-ink">{node.text}</p>

      {node.context && (
        <div className="rounded-xl border border-c-border bg-c-surface p-3">
          <p className="text-[10px] font-headline uppercase tracking-[0.18em] text-c-text-faint mb-1.5">
            How it surfaced
          </p>
          <p className="text-[12.5px] leading-relaxed text-c-text-muted">{node.context}</p>
        </div>
      )}

      <div className="flex flex-wrap gap-3 text-[11px] text-c-text-faint">
        <span>{formatDate(node.created_at)}</span>
        <span>&middot;</span>
        <span className="capitalize">from {node.source_type || "journal"}</span>
        {node.source_ref && (
          <>
            <span>&middot;</span>
            <span className="truncate max-w-[160px]">{node.source_ref}</span>
          </>
        )}
      </div>

      {node.tags.length > 0 && (
        <div className="flex flex-wrap gap-1.5">
          {node.tags.map((t) => (
            <span key={t} className="rounded-full border border-c-border bg-c-surface px-2 py-0.5 text-[11px] text-c-text-muted">
              #{t}
            </span>
          ))}
        </div>
      )}

      {connections.length > 0 && (
        <div className="pt-2 border-t border-c-border">
          <p className="text-[10px] font-headline uppercase tracking-[0.18em] text-c-text-faint mb-2">
            Connected to {connections.length} {connections.length === 1 ? "star" : "stars"}
          </p>
          <div className="space-y-1">
            {connections.map(({ other, sim }) => {
              const strengthLabel = sim >= 0.75 ? "Strong" : sim >= 0.5 ? "Moderate" : "Weak";
              const barColor = sim >= 0.75 ? "#7C6BF0" : sim >= 0.5 ? "#4ECDC4" : "#475569";
              return (
                <button
                  key={other.id}
                  type="button"
                  onClick={() => onJump(other.id)}
                  className="w-full flex items-center gap-2 text-left p-1.5 rounded-lg hover:bg-surface-hover transition group"
                >
                  <span
                    className="inline-block h-1.5 w-1.5 rounded-full shrink-0"
                    style={{ backgroundColor: clusterNodeColor(other.cluster_id) }}
                  />
                  <span className="flex-1 truncate text-[12px] text-c-text-muted group-hover:text-ink transition">
                    {lessonTitle(other)}
                  </span>
                  <span className="flex items-center gap-1">
                    <span className="h-1 w-8 rounded-full bg-white/10 overflow-hidden">
                      <span
                        className="block h-full rounded-full"
                        style={{ width: `${sim * 100}%`, backgroundColor: barColor }}
                      />
                    </span>
                    <span className="text-[9px] uppercase tracking-wider text-c-text-faint w-12">
                      {strengthLabel}
                    </span>
                  </span>
                </button>
              );
            })}
          </div>
        </div>
      )}
    </div>
  );
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

  // Client-side clustering fallback: when API returns no clusters,
  // derive clusters from node tags so the visual shows differentiation.
  const effectiveData = useMemo(() => {
    const hasApiClusters = data.clusters.length > 0;
    const allUnclustered = data.nodes.length > 0 && data.nodes.every((n) => n.cluster_id == null);

    if (hasApiClusters || !allUnclustered) {
      return data; // API provided clusters, use them
    }

    // No clusters — derive from tags
    const { clusters, clusterMap } = clusterByTags(data.nodes);
    const nodes = data.nodes.map((n) => {
      const cid = clusterMap.get(n.id);
      return cid != null ? { ...n, cluster_id: cid } : n;
    });
    return { ...data, nodes, clusters };
  }, [data]);

  const query = searchText.trim().toLowerCase();
  const totalLessonCount = effectiveData.nodes.length;
  const isSparse = totalLessonCount < CLUSTER_THRESHOLD;

  const filteredNodes = useMemo(() => {
    return effectiveData.nodes.filter((node) => {
      const matchesSearch = query
        ? node.text.toLowerCase().includes(query) || node.tags.some((tag) => tag.toLowerCase().includes(query))
        : true;
      const matchesCluster = selectedClusterId == null ? true : node.cluster_id === selectedClusterId;
      return matchesSearch && matchesCluster;
    });
  }, [effectiveData.nodes, query, selectedClusterId]);

  const filteredNodeIds = useMemo(() => {
    return new Set(filteredNodes.map((node) => Number(node.id)));
  }, [filteredNodes]);

  const allEdges = useMemo(() => {
    return [...effectiveData.edges, ...effectiveData.affinity_edges].filter((edge) => {
      return filteredNodeIds.has(Number(edge.source)) && filteredNodeIds.has(Number(edge.target));
    });
  }, [effectiveData.edges, effectiveData.affinity_edges, filteredNodeIds]);

  const nodesById = useMemo(() => {
    const map = new Map<number, ConstellationNode>();
    effectiveData.nodes.forEach((node) => map.set(Number(node.id), node));
    return map;
  }, [effectiveData.nodes]);

  // Responsive spacing based on container width
  const spacing = useMemo(() => getSpacing(containerSize.width), [containerSize.width]);

  // Compute pixel positions with cluster-aware layout + collision avoidance
  const positionedNodes = useMemo(() => {
    const count = filteredNodes.length;
    const { nodePadding } = spacing;
    const w = containerSize.width;
    const h = containerSize.height;
    if (count === 0 || w === 0 || h === 0) return [];

    // Identify unique clusters in the filtered nodes
    const clusterIds = [...new Set(filteredNodes.map((n) => n.cluster_id))];
    const numClusters = clusterIds.length;
    const hasMultipleClusters = numClusters > 1;
    const hasPositions = filteredNodes.some((n) => n.x != null && n.y != null);

    // If the API gave us positions AND clusters, trust them
    if (hasPositions && hasMultipleClusters) {
      const raw = filteredNodes.map((node) => {
        const nx = node.x ?? 0;
        const ny = node.y ?? 0;
        const px = nodePadding + ((nx + 1) / 2) * (w - 2 * nodePadding);
        const py = nodePadding + ((ny + 1) / 2) * (h - 2 * nodePadding);
        return { ...node, px, py };
      });
      if (raw.length > 1) {
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
    }

    // Cluster-aware layout: arrange cluster centroids, then place nodes around them
    // Use a deterministic seed so layout doesn't jump on re-render
    let seed = 7;
    function rand() {
      seed = (seed * 1664525 + 1013904223) & 0x7fffffff;
      return (seed / 0x7fffffff) * 2 - 1; // returns -1 to 1
    }

    const usableW = w - 2 * nodePadding;
    const usableH = h - 2 * nodePadding;
    const cx = w / 2;
    const cy = h / 2;

    // Arrange cluster centroids in a circle
    const clusterCentroids = new Map<number | null, { x: number; y: number }>();
    if (hasMultipleClusters) {
      const orbitR = Math.min(usableW, usableH) * 0.32;
      clusterIds.forEach((cid, i) => {
        const angle = (2 * Math.PI * i) / numClusters - Math.PI / 2;
        clusterCentroids.set(cid, {
          x: cx + Math.cos(angle) * orbitR,
          y: cy + Math.sin(angle) * orbitR,
        });
      });
    } else {
      clusterCentroids.set(clusterIds[0] ?? null, { x: cx, y: cy });
    }

    // Place each node near its cluster centroid with jitter
    const nodesPerCluster = new Map<number | null, number>();
    clusterIds.forEach((cid) => {
      nodesPerCluster.set(cid, filteredNodes.filter((n) => n.cluster_id === cid).length);
    });

    const raw = filteredNodes.map((node) => {
      const centroid = clusterCentroids.get(node.cluster_id) || { x: cx, y: cy };
      const nInCluster = nodesPerCluster.get(node.cluster_id) || 1;
      const spread = Math.min(usableW, usableH) * (0.08 + Math.sqrt(nInCluster) * 0.035);
      const px = centroid.x + rand() * spread;
      const py = centroid.y + rand() * spread;
      return { ...node, px, py };
    });

    // Collision avoidance
    const resolved = resolveCollisions(
      raw.map((n) => ({ id: n.id, px: n.px, py: n.py, cluster_id: n.cluster_id })),
      w, h, spacing,
    );
    const posMap = new Map(resolved.map((r) => [r.id, r]));
    return raw.map((n) => {
      const p = posMap.get(n.id);
      return p ? { ...n, px: p.px, py: p.py } : n;
    });
  }, [filteredNodes, containerSize, spacing]);

  const nodePositions = useMemo(() => {
    const map = new Map<number, { px: number; py: number }>();
    positionedNodes.forEach((node) => map.set(node.id, { px: node.px, py: node.py }));
    return map;
  }, [positionedNodes]);

  const allTags = useMemo(() => {
    const tags = new Set<string>();
    effectiveData.nodes.forEach((n) => n.tags.forEach((t) => tags.add(t)));
    return Array.from(tags).sort();
  }, [effectiveData.nodes]);

  const selectedNode = useMemo(() => {
    if (selectedNodeId == null) return null;
    return nodesById.get(selectedNodeId) ?? null;
  }, [nodesById, selectedNodeId]);

  // Nodes directly connected to the selected node via edges
  const connectedNodeIds = useMemo(() => {
    if (selectedNodeId == null) return new Set<number>();
    const connected = new Set<number>();
    connected.add(selectedNodeId);
    for (const edge of [...effectiveData.edges, ...effectiveData.affinity_edges]) {
      const src = Number(edge.source);
      const tgt = Number(edge.target);
      if (src === selectedNodeId) connected.add(tgt);
      if (tgt === selectedNodeId) connected.add(src);
    }
    return connected;
  }, [selectedNodeId, effectiveData.edges, effectiveData.affinity_edges]);

  // Cluster bounds for nebulae rendering (V1 Safe)
  const clusterBounds = useMemo(
    () => computeClusterBounds(positionedNodes),
    [positionedNodes],
  );

  // Node connection weight for star sizing
  const nodeWeights = useMemo(() => {
    const w = new Map<number, number>();
    allEdges.forEach((e) => {
      w.set(Number(e.source), (w.get(Number(e.source)) || 0) + 1);
      w.set(Number(e.target), (w.get(Number(e.target)) || 0) + 1);
    });
    return w;
  }, [allEdges]);

  function starRadius(nodeId: number): number {
    return Math.min(6, 2.5 + (nodeWeights.get(nodeId) || 1) * 0.6);
  }

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
        label: getClusterLabel(clusterId, effectiveData.clusters),
        count: nodes.length,
        nodes,
      }))
      .sort((a, b) => {
        if (a.id === null && b.id !== null) return 1;
        if (a.id !== null && b.id === null) return -1;
        return a.label.toLowerCase().localeCompare(b.label.toLowerCase());
      });
  }, [filteredNodes, effectiveData.clusters]);

  const clustersForSidebar = useMemo(() => {
    const items = effectiveData.clusters.map((cluster) => ({
      id: cluster.id as number | null,
      label: cluster.label || `Cluster ${cluster.id}`,
      count: filteredNodes.filter((node) => node.cluster_id === cluster.id).length,
    }));
    const unclusteredCount = filteredNodes.filter((node) => node.cluster_id == null).length;
    if (unclusteredCount > 0) {
      const label = effectiveData.clusters.length === 0 ? "Your Lessons" : "Unclustered";
      items.push({ id: null, label, count: unclusteredCount });
    }
    return items.filter((item) => item.count > 0);
  }, [effectiveData.clusters, filteredNodes]);

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

  // Ref for auto-scrolling to detail panel on mobile
  const detailRef = useRef<HTMLDivElement>(null);
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

  // ── Constellation view (V1 Safe) ──
  if (viewMode === "constellation") {
    const { width: stageW, height: stageH } = containerSize;

    return (
      <div className="flex flex-col flex-1 min-h-0 -mt-4">
        {/* ── Header bar ── */}
        <header className="flex items-center justify-between gap-3 px-5 sm:px-8 py-4 border-b border-c-border relative z-10">
          <div className="flex items-center gap-3 min-w-0">
            <div
              className="h-9 w-9 rounded-full flex items-center justify-center shrink-0"
              style={{
                background: "radial-gradient(circle at 35% 30%, #9B8DF5 0%, #7C6BF0 40%, #3f2fbf 100%)",
                boxShadow: "0 0 16px rgba(124,107,240,0.55)",
              }}
            >
              <svg viewBox="0 0 24 24" className="h-4 w-4 text-white" fill="currentColor" aria-hidden="true">
                <path d="M12 2L13.09 8.26L18 4L14.74 9.91L21 10L14.74 12.09L18 18L13.09 13.74L12 20L10.91 13.74L6 18L9.26 12.09L3 10L9.26 9.91L6 4L10.91 8.26L12 2Z" />
              </svg>
            </div>
            <div className="min-w-0">
              <h1 className="text-[15px] font-semibold font-headline truncate" style={{ letterSpacing: "-0.01em" }}>
                Lessons Constellation
              </h1>
              <p className="text-[11px] text-c-text-muted truncate">
                {totalLessonCount} lessons &middot; {effectiveData.clusters.length} constellations
              </p>
            </div>
          </div>
          <div className="flex items-center gap-2">
            <div className="hidden sm:flex rounded-full border border-c-border bg-c-surface text-[11px] overflow-hidden">
              <button
                type="button"
                onClick={() => handleSetViewMode("constellation")}
                className="px-3 py-1.5 bg-white/10 text-white font-medium"
              >
                Constellation
              </button>
              <button
                type="button"
                onClick={() => handleSetViewMode("list")}
                className="px-3 py-1.5 text-c-text-muted hover:text-white transition"
              >
                List
              </button>
            </div>
            {pendingCount > 0 && (
              <Link
                href="/constellation/pending"
                className="rounded-full border border-accent/40 bg-accent/10 px-3 py-1.5 text-[11px] font-semibold text-accent-hover"
              >
                {pendingCount} waiting
              </Link>
            )}
          </div>
        </header>

        {/* ── Progress (sparse) ── */}
        {isSparse && totalLessonCount > 0 && (
          <div className="px-5 sm:px-8 py-3">
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

        {/* ── Stage + Detail panel ── */}
        <div className="flex-1 flex relative min-h-0">
          <section
            ref={containerRef}
            className="flex-1 relative overflow-hidden min-h-[400px]"
            style={{
              background: "radial-gradient(ellipse 80% 60% at 50% 40%, #0e1420 0%, #0a0d14 60%, #05070b 100%)",
            }}
          >
            <Starfield width={stageW} height={stageH} />

            {/* SVG overlay: nebulae + edges + nodes + labels */}
            {stageW > 0 && stageH > 0 && (
              <svg
                className="absolute inset-0 w-full h-full"
                viewBox={`0 0 ${stageW} ${stageH}`}
                aria-hidden="true"
              >
                <defs>
                  {/* Nebula gradients per cluster */}
                  {effectiveData.clusters.map((c) => (
                    <radialGradient key={`neb-${c.id}`} id={`neb-${c.id}`} cx="50%" cy="50%" r="50%">
                      <stop offset="0%" stopColor={clusterNodeColor(c.id)} stopOpacity="0.22" />
                      <stop offset="40%" stopColor={clusterNodeColor(c.id)} stopOpacity="0.08" />
                      <stop offset="100%" stopColor={clusterNodeColor(c.id)} stopOpacity="0" />
                    </radialGradient>
                  ))}
                  <radialGradient id="node-glow" cx="50%" cy="50%" r="50%">
                    <stop offset="0%" stopColor="#fff" stopOpacity="1" />
                    <stop offset="40%" stopColor="#fff" stopOpacity="0.5" />
                    <stop offset="100%" stopColor="#fff" stopOpacity="0" />
                  </radialGradient>
                  <filter id="starblur"><feGaussianBlur stdDeviation="0.6" /></filter>
                </defs>

                {/* Nebulae (cluster clouds) */}
                {Array.from(clusterBounds.entries()).map(([key, b]) => {
                  const cid = key === "null" ? null : Number(key);
                  const isDim = selectedClusterId != null && cid !== selectedClusterId;
                  return (
                    <g key={`nebg-${key}`} style={{ opacity: isDim ? 0.25 : 1, transition: "opacity 400ms" }}>
                      <circle cx={b.cx} cy={b.cy} r={b.r * 1.15} fill={cid != null ? `url(#neb-${cid})` : "none"} />
                    </g>
                  );
                })}

                {/* Dashed constellation rings */}
                {Array.from(clusterBounds.entries()).map(([key, b]) => {
                  const cid = key === "null" ? null : Number(key);
                  const color = clusterNodeColor(cid);
                  const isSel = selectedClusterId === cid;
                  return (
                    <g key={`ring-${key}`} style={{ opacity: isSel ? 0.9 : 0.35, transition: "opacity 300ms" }}>
                      <circle
                        cx={b.cx} cy={b.cy} r={b.r * 0.95}
                        fill="none" stroke={color} strokeOpacity={isSel ? 0.5 : 0.18}
                        strokeWidth={1} strokeDasharray="2 6"
                      />
                    </g>
                  );
                })}

                {/* Edges with per-cluster color gradients */}
                {allEdges.map((edge, i) => {
                  const sPos = nodePositions.get(Number(edge.source));
                  const tPos = nodePositions.get(Number(edge.target));
                  if (!sPos || !tPos) return null;
                  const sNode = nodesById.get(Number(edge.source));
                  const tNode = nodesById.get(Number(edge.target));
                  if (!sNode || !tNode) return null;
                  const sameCluster = sNode.cluster_id === tNode.cluster_id;
                  const sim = edge.similarity ?? 0;
                  const isConn = selectedNodeId != null && (Number(edge.source) === selectedNodeId || Number(edge.target) === selectedNodeId);
                  const dimmed = selectedNodeId != null && !isConn;
                  const c1 = clusterNodeColor(sNode.cluster_id);
                  const c2 = clusterNodeColor(tNode.cluster_id);
                  const gradId = `edge-g-${i}`;
                  const opacity = dimmed ? 0.04 : isConn ? 0.75 : sim >= 0.75 ? 0.42 : sim >= 0.5 ? 0.24 : 0.13;
                  return (
                    <g key={`edge-${i}`}>
                      <defs>
                        <linearGradient id={gradId} x1={sPos.px} y1={sPos.py} x2={tPos.px} y2={tPos.py} gradientUnits="userSpaceOnUse">
                          <stop offset="0%" stopColor={c1} stopOpacity="0.9" />
                          <stop offset="100%" stopColor={c2} stopOpacity="0.9" />
                        </linearGradient>
                      </defs>
                      <line
                        x1={sPos.px} y1={sPos.py} x2={tPos.px} y2={tPos.py}
                        stroke={`url(#${gradId})`}
                        strokeWidth={isConn ? 2 : sim >= 0.75 ? 1.4 : 1}
                        strokeDasharray={!sameCluster && !isConn ? "3 6" : undefined}
                        opacity={opacity}
                        style={{ transition: "opacity 300ms" }}
                      />
                    </g>
                  );
                })}

                {/* Nodes (stars with glow halos) */}
                {positionedNodes.map((node) => {
                  const r = starRadius(node.id);
                  const color = clusterNodeColor(node.cluster_id);
                  const isSel = selectedNodeId === node.id;
                  const isConn = connectedNodeIds.has(node.id);
                  const dimmed = selectedNodeId != null && !isConn;
                  const opacity = dimmed ? 0.25 : 1;
                  return (
                    <g
                      key={`star-${node.id}`}
                      style={{ transition: "opacity 300ms", opacity }}
                    >
                      {/* Outer glow halo */}
                      <circle cx={node.px} cy={node.py} r={r * 3} fill={color} opacity={isSel ? 0.35 : 0.12} filter="url(#starblur)" />
                      <circle cx={node.px} cy={node.py} r={r * 1.8} fill={color} opacity={isSel ? 0.4 : 0.2} filter="url(#starblur)" />
                      {/* Core */}
                      <circle cx={node.px} cy={node.py} r={r} fill="#fff" />
                      <circle cx={node.px} cy={node.py} r={r * 0.6} fill={color} />
                      {/* Selection ring */}
                      {isSel && (
                        <circle cx={node.px} cy={node.py} r={r + 6} fill="none" stroke="#fff" strokeOpacity={0.7} strokeWidth={1} />
                      )}
                    </g>
                  );
                })}

                {/* Constellation labels (Instrument Serif italic) */}
                {Array.from(clusterBounds.entries()).map(([key, b]) => {
                  const cid = key === "null" ? null : Number(key);
                  const label = getClusterLabel(cid, effectiveData.clusters);
                  const color = clusterNodeColor(cid);
                  const isDim = selectedClusterId != null && cid !== selectedClusterId;
                  // Place label above nebula, flip below if near top
                  const aboveY = b.cy - b.r - 16;
                  const belowY = b.cy + b.r + 22;
                  const labelY = aboveY < 20 ? belowY : aboveY;
                  const displayLabel = label.replace(/\b\w/g, (c) => c.toUpperCase());
                  return (
                    <g
                      key={`lbl-${key}`}
                      style={{ cursor: "pointer", opacity: isDim ? 0.3 : 1, transition: "opacity 300ms" }}
                      onClick={() => setSelectedClusterId((c) => (c === cid ? null : cid))}
                    >
                      <text
                        x={b.cx} y={labelY}
                        textAnchor="middle"
                        fill={color}
                        stroke="rgba(5,7,11,0.85)"
                        strokeWidth={4}
                        strokeLinejoin="round"
                        style={{ fontFamily: "var(--font-serif)", fontSize: 21, fontStyle: "italic", letterSpacing: "0.02em", paintOrder: "stroke fill" }}
                      >
                        {displayLabel}
                      </text>
                    </g>
                  );
                })}
              </svg>
            )}

            {/* Accessible node hit targets (HTML buttons over SVG) */}
            {stageW > 0 && positionedNodes.map((node) => {
              const r = Math.max(22, starRadius(node.id) * 3);
              return (
                <button
                  key={`hit-${node.id}`}
                  type="button"
                  className="absolute focus-visible:outline-none"
                  style={{
                    left: node.px - r,
                    top: node.py - r,
                    width: r * 2,
                    height: r * 2,
                    borderRadius: "50%",
                  }}
                  aria-label={`Lesson: ${lessonTitle(node)}`}
                  onClick={() => setSelectedNodeId((p) => (p === node.id ? null : node.id))}
                />
              );
            })}

            {/* Floating legend — bottom-left */}
            {clustersForSidebar.length > 0 && (
              <div className="absolute bottom-4 left-4 p-3 rounded-2xl border border-c-border bg-black/40 backdrop-blur-md text-[11px] max-w-[240px] z-10">
                <p className="text-[9px] uppercase tracking-[0.18em] text-c-text-faint mb-2 font-headline">
                  Constellations
                </p>
                <div className="flex flex-col gap-1.5">
                  {clustersForSidebar.map((c) => {
                    const isSel = selectedClusterId === c.id;
                    const color = clusterNodeColor(c.id);
                    return (
                      <button
                        key={`leg-${c.id}`}
                        type="button"
                        onClick={() => setSelectedClusterId((cur) => (cur === c.id ? null : c.id))}
                        className="flex items-center gap-2 text-left group"
                      >
                        <span
                          className="inline-block h-2 w-2 rounded-full shrink-0"
                          style={{ backgroundColor: color, boxShadow: `0 0 6px ${color}` }}
                        />
                        <span className={`flex-1 truncate ${isSel ? "text-white" : "text-c-text-muted group-hover:text-white"} transition`}>
                          {c.label}
                        </span>
                        <span className="text-c-text-faint text-[10px]">{c.count}</span>
                      </button>
                    );
                  })}
                </div>
              </div>
            )}

            {/* Help hint — top-right */}
            <div className="absolute top-4 right-4 p-2.5 px-3 rounded-full border border-c-border bg-black/40 backdrop-blur-md text-[10px] text-c-text-muted hidden sm:flex items-center gap-3 z-10">
              <span><span className="text-white/70">Click</span> a star</span>
              <span className="h-3 w-px bg-white/15" />
              <span><span className="text-white/70">Tap</span> a name to focus</span>
            </div>

            {/* Empty state */}
            {filteredNodes.length === 0 && (
              <div className="absolute inset-0 flex items-center justify-center">
                <div className="text-center max-w-sm px-4">
                  <p className="text-lg font-headline font-bold text-ink">Your constellation begins here</p>
                  <p className="mt-2 text-sm text-ink-muted">As you chat with your assistant, lessons will appear as stars in your personal sky.</p>
                </div>
              </div>
            )}
          </section>

          {/* Desktop detail panel */}
          {selectedNode && (
            <aside className="hidden md:flex w-[360px] shrink-0 flex-col border-l border-c-border bg-[#0d1218]/95 backdrop-blur-xl overflow-y-auto">
              <StarDetailPanel
                node={selectedNode}
                clusters={effectiveData.clusters}
                allEdges={allEdges}
                nodesById={nodesById}
                onClose={() => setSelectedNodeId(null)}
                onJump={setSelectedNodeId}
              />
            </aside>
          )}
        </div>

        {/* Mobile detail panel */}
        {selectedNode && (
          <div ref={detailRef} className="md:hidden border-t border-c-border bg-[#0d1218] max-h-[50vh] overflow-y-auto">
            <StarDetailPanel
              node={selectedNode}
              clusters={effectiveData.clusters}
              allEdges={allEdges}
              nodesById={nodesById}
              onClose={() => setSelectedNodeId(null)}
              onJump={setSelectedNodeId}
            />
          </div>
        )}
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
