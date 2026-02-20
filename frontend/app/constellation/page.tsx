"use client";

import dynamic from "next/dynamic";
import Link from "next/link";
import { useEffect, useMemo, useState } from "react";

import { SectionCard } from "@/components/section-card";
import {
  fetchConstellation,
  fetchPendingLessons,
} from "@/lib/api";
import { ConstellationData, ConstellationNode } from "@/lib/types";

type ConstellationNodeLike = ConstellationNode & {
  context?: string;
};

type ConstellationGraphNode = ConstellationNodeLike;

type ConstellationGraphData = {
  nodes: ConstellationGraphNode[];
  links: Array<{
    source: number;
    target: number;
    similarity: number;
    connection_type: string;
  }>;
};

const ForceGraph2D = dynamic(() => import("react-force-graph-2d"), {
  ssr: false,
});

function formatDate(dateString: string): string {
  const date = new Date(dateString);
  if (Number.isNaN(date.getTime())) return dateString;
  return date.toLocaleString();
}

function clusterLabelColor(id: number | null): string {
  if (id == null) {
    return "#7f8b9c";
  }
  const hue = (id * 42) % 360;
  return `hsl(${hue}, 65%, 52%)`;
}

export default function ConstellationPage() {
  const [data, setData] = useState<ConstellationData>({ nodes: [], edges: [], clusters: [] });
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string>("");

  const [searchText, setSearchText] = useState("");
  const [selectedClusterId, setSelectedClusterId] = useState<number | null>(null);
  const [hoveredNode, setHoveredNode] = useState<ConstellationNode | null>(null);
  const [selectedNode, setSelectedNode] = useState<ConstellationNodeLike | null>(null);
  const [pendingCount, setPendingCount] = useState(0);

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
        if (!mounted) return;
        setError(err instanceof Error ? err.message : "Failed to load constellation.");
      } finally {
        if (mounted) {
          setLoading(false);
        }
      }
    }

    loadData();

    return () => {
      mounted = false;
    };
  }, []);

  const clusters = data.clusters ?? [];
  const query = searchText.trim().toLowerCase();

  const filteredNodes = useMemo(() => {
    return data.nodes.filter((node) => {
      const matchesSearch = query
        ? node.text.toLowerCase().includes(query) || node.tags.some((tag) => tag.toLowerCase().includes(query))
        : true;
      const matchesCluster = selectedClusterId == null ? true : node.cluster_id === selectedClusterId;
      return matchesSearch && matchesCluster;
    });
  }, [data.nodes, query, selectedClusterId]);

  const filteredNodeIds = useMemo(
    () => new Set(filteredNodes.map((node) => Number(node.id))),
    [filteredNodes],
  );

  const filteredGraphData: ConstellationGraphData = useMemo(() => {
    const links = data.edges.filter((edge) => {
      return filteredNodeIds.has(Number(edge.source)) && filteredNodeIds.has(Number(edge.target));
    });

    return {
      nodes: filteredNodes,
      links,
    };
  }, [data.edges, filteredNodes, filteredNodeIds]);

  if (error) {
    return (
      <SectionCard title="Constellation" subtitle="A visual learning map of approved lessons">
        <div className="rounded-panel border border-rose-border bg-rose-bg px-3 py-2 text-sm text-rose-text">{error}</div>
      </SectionCard>
    );
  }

  if (loading) {
    return <SectionCard title="Constellation" subtitle="Loading your learning graph...">Loading...</SectionCard>;
  }

  return (
    <div className="grid gap-4 lg:grid-cols-[minmax(0,1fr)_320px]">
      <SectionCard title="Lessons Constellation" subtitle="Navigate how your approved lessons connect over time">
        <div className="relative h-[66vh] min-h-[420px] overflow-hidden rounded-panel border border-border bg-surface">
          <ForceGraph2D
            graphData={filteredGraphData as unknown as ConstellationGraphData}
            nodeId="id"
            nodeLabel={(node) => (node as ConstellationNode).text}
            nodeColor={(node) => clusterLabelColor((node as ConstellationNode).cluster_id)}
            nodeVal={1}
            nodeRelSize={7}
            backgroundColor="transparent"
            linkCurvature={0}
            linkDirectionalArrowLength={0}
            linkWidth={() => 1}
            linkColor={(link) => {
              const similarity = Number((link as { similarity?: number } ).similarity ?? 0);
              const opacity = Math.max(0.08, Math.min(0.9, Number(similarity) || 0.2));
              return `rgba(120, 130, 142, ${opacity})`;
            }}
            onNodeClick={(node: unknown) => setSelectedNode(node as ConstellationNodeLike)}
            onNodeHover={(node: unknown) => setHoveredNode((node as ConstellationNode | null) ?? null)}
            cooldownTicks={120}
            enableZoomInteraction
            enableNodeDrag={false}
            d3AlphaDecay={0.025}
            d3VelocityDecay={0.32}
          />

          {hoveredNode ? (
            <div className="pointer-events-none absolute left-4 top-4 max-w-sm rounded-panel border border-border bg-surface/95 px-3 py-2 text-sm text-ink shadow-lg">
              {hoveredNode.text}
            </div>
          ) : null}

          {selectedNode ? (
            <div className="absolute right-4 top-4 w-72 max-w-[80%] rounded-panel border border-border bg-surface/95 p-3 shadow-lg">
              <h3 className="text-sm font-semibold text-ink">Selected Lesson</h3>
              <p className="mt-2 text-sm text-ink">{selectedNode.text}</p>
              <p className="mt-2 text-xs text-ink-muted">{selectedNode.context || "No context"}</p>
              <p className="mt-2 text-xs text-ink-faint">Date: {formatDate(selectedNode.created_at)}</p>
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
      </SectionCard>

      <aside className="space-y-4">
        <SectionCard title="Filters" subtitle="Search and cluster visibility">
          <label className="mb-3 block text-sm text-ink-muted">
            <span className="mb-2 block text-xs uppercase tracking-[0.1em] text-ink-faint">Search lessons</span>
            <input
              value={searchText}
              onChange={(e) => setSearchText(e.target.value)}
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

        <SectionCard title="Cluster List" subtitle="Color indicates cluster grouping">
          {clusters.length === 0 ? (
            <p className="text-sm text-ink-muted">No clusters yet.</p>
          ) : (
            <ul className="space-y-2">
              {clusters.map((cluster) => (
                <li key={cluster.id}>
                  <button
                    type="button"
                    onClick={() =>
                      setSelectedClusterId((current) => (current === cluster.id ? null : cluster.id))
                    }
                    className={`flex w-full items-center justify-between rounded-panel border border-border p-2 text-left transition ${
                      selectedClusterId === cluster.id
                        ? "bg-surface-hover"
                        : "bg-surface"
                    }`}
                  >
                    <div className="flex items-center gap-2 min-w-0">
                      <span
                        className="inline-block h-2.5 w-2.5 shrink-0 rounded-full"
                        style={{ backgroundColor: clusterLabelColor(cluster.id) }}
                      />
                      <span className="truncate text-sm text-ink">{cluster.label || `Cluster ${cluster.id}`}</span>
                    </div>
                    <span className="text-xs text-ink-muted">{cluster.count}</span>
                  </button>
                </li>
              ))}
            </ul>
          )}
        </SectionCard>
      </aside>
    </div>
  );
}
