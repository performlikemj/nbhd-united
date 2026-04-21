"use client";

import Link from "next/link";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { fetchConstellation, fetchPendingLessons } from "@/lib/api";
import {
  ConstellationData,
  ConstellationNode,
  GraphData,
  GraphEdge,
  GraphNode,
  GraphNodeKind,
  GraphRelType,
} from "@/lib/types";

// ── Constants ────────────────────────────────────────────────────────────────

const KIND_COLORS: Record<GraphNodeKind, string> = {
  Lesson: "#7C6BF0", Cluster: "#E8B4B8", Evidence: "#4ECDC4", Tag: "#FBBF24",
};
const REL_COLORS: Record<GraphRelType, string> = {
  IN_CLUSTER: "#E8B4B8", SIMILAR_TO: "#7C6BF0", EVIDENCED_BY: "#4ECDC4", TAGGED_WITH: "#FBBF24", REFINES: "#F472B6",
};
const ALL_KINDS: GraphNodeKind[] = ["Lesson", "Cluster", "Evidence", "Tag"];
const ALL_RELS: GraphRelType[] = ["IN_CLUSTER", "SIMILAR_TO", "EVIDENCED_BY", "TAGGED_WITH", "REFINES"];
const CLUSTER_PALETTE = ["#7C6BF0", "#E8B4B8", "#4ECDC4", "#FBBF24", "#60A5FA", "#F472B6", "#34D399", "#FB923C"];
const VW = 2400;
const VH = 1600;

// ── Helpers ──────────────────────────────────────────────────────────────────

function clusterColor(id: number): string { return CLUSTER_PALETTE[Math.abs(id) % CLUSTER_PALETTE.length]; }

function nodeLabel(n: GraphNode): string {
  if (n.kind === "Lesson") { const t = n.text?.split(".")[0]; return t ? (t.length > 80 ? t.slice(0, 77) + "\u2026" : t + ".") : String(n.id); }
  if (n.kind === "Cluster") return n.constellation || n.label;
  if (n.kind === "Evidence") return n.label;
  if (n.kind === "Tag") return `#${n.label}`;
  return String(n.id);
}

function nodeRadius(n: GraphNode, zoom: number): number {
  const base = n.kind === "Cluster" ? 24 : n.kind === "Lesson" ? 12 + (n.weight || 2) * 1.6 : n.kind === "Evidence" ? 10 : 8;
  return base * Math.max(0.7, Math.min(1.4, zoom * 0.95));
}

function cypherPathFor(node: GraphNode): string {
  if (node.kind === "Lesson") return `MATCH (l:Lesson {id: ${node.id}})-[r]-(n) RETURN l, r, n`;
  if (node.kind === "Cluster") return `MATCH (c:Cluster {id: ${String(node.id).split(":")[1]}})<-[:IN_CLUSTER]-(l:Lesson) RETURN c, l`;
  if (node.kind === "Evidence") return `MATCH (l:Lesson)-[:EVIDENCED_BY]->(e:Evidence {id: "${node.id}"}) RETURN l, e`;
  if (node.kind === "Tag") return `MATCH (l:Lesson)-[:TAGGED_WITH]->(t:Tag {name: "${node.label}"}) RETURN l, t`;
  return `MATCH (n) RETURN n LIMIT 25`;
}

// ── Data transformation ──────────────────────────────────────────────────────

function detectRefines(nodes: ConstellationNode[]): Array<{ from: number; to: number }> {
  const byCluster = new Map<number, ConstellationNode[]>();
  for (const n of nodes) { if (n.cluster_id != null) { const arr = byCluster.get(n.cluster_id) || []; arr.push(n); byCluster.set(n.cluster_id, arr); } }
  const result: Array<{ from: number; to: number }> = [];
  for (const group of byCluster.values()) {
    if (group.length < 2) continue;
    const sorted = [...group].sort((a, b) => new Date(a.created_at).getTime() - new Date(b.created_at).getTime());
    for (let i = 0; i < sorted.length - 1; i++) {
      const days = (new Date(sorted[i + 1].created_at).getTime() - new Date(sorted[i].created_at).getTime()) / 864e5;
      if (days > 30) result.push({ from: sorted[i].id, to: sorted[i + 1].id });
    }
  }
  return result;
}

function buildGraphData(data: ConstellationData): GraphData {
  const { nodes, edges, affinity_edges, clusters } = data;
  const lessonNodes: GraphNode[] = nodes.map((n) => ({ id: n.id, kind: "Lesson" as const, label: n.tags.slice(0, 2).join(" / ") || n.text.split(/\s+/).slice(0, 5).join(" "), text: n.text, context: n.context, tags: n.tags, source_type: n.source_type, source_ref: n.source_ref, created_at: n.created_at, cluster_id: n.cluster_id ?? undefined, weight: 3 }));
  const clusterNodes: GraphNode[] = clusters.map((c) => ({ id: `c:${c.id}`, kind: "Cluster" as const, label: c.label, constellation: c.label, theme: c.label, color: clusterColor(c.id) }));
  const evidenceNodes: GraphNode[] = nodes.filter((n) => n.source_ref).map((n) => ({ id: `ev:${n.id}`, kind: "Evidence" as const, label: n.source_ref!, source_type: n.source_type, source_ref: n.source_ref, created_at: n.created_at }));
  const tagSet = new Set<string>(); nodes.forEach((n) => n.tags.forEach((t) => tagSet.add(t)));
  const tagNodes: GraphNode[] = [...tagSet].map((t) => ({ id: `tag:${t}`, kind: "Tag" as const, label: t }));
  const seen = new Set<string>();
  const uniqueEdges = [...edges, ...affinity_edges].filter((e) => { const k = `${Math.min(Number(e.source), Number(e.target))}-${Math.max(Number(e.source), Number(e.target))}`; if (seen.has(k)) return false; seen.add(k); return true; });
  const similarEdges: GraphEdge[] = uniqueEdges.map((e) => ({ source: Number(e.source), target: Number(e.target), type: "SIMILAR_TO" as const, similarity: e.similarity }));
  const clusterEdges: GraphEdge[] = nodes.filter((n) => n.cluster_id != null).map((n) => ({ source: n.id, target: `c:${n.cluster_id}`, type: "IN_CLUSTER" as const }));
  const evidenceEdges: GraphEdge[] = nodes.filter((n) => n.source_ref).map((n) => ({ source: n.id, target: `ev:${n.id}`, type: "EVIDENCED_BY" as const }));
  const tagEdges: GraphEdge[] = nodes.flatMap((n) => n.tags.map((t) => ({ source: n.id, target: `tag:${t}`, type: "TAGGED_WITH" as const })));
  const refinesEdges: GraphEdge[] = detectRefines(nodes).map((r) => ({ source: r.from, target: r.to, type: "REFINES" as const }));
  return { nodes: [...lessonNodes, ...clusterNodes, ...evidenceNodes, ...tagNodes], edges: [...clusterEdges, ...similarEdges, ...evidenceEdges, ...tagEdges, ...refinesEdges], kindColors: KIND_COLORS, relColors: REL_COLORS };
}

// ── Tag-based clustering fallback ────────────────────────────────────────────

function clusterByTags(nodes: ConstellationNode[]): { clusters: ConstellationData["clusters"]; clusterMap: Map<number, number> } {
  if (nodes.length === 0) return { clusters: [], clusterMap: new Map() };
  const tagCounts = new Map<string, number>();
  for (const n of nodes) for (const t of n.tags) tagCounts.set(t, (tagCounts.get(t) || 0) + 1);
  const seedTags = [...tagCounts.entries()].filter(([, c]) => c >= 2).sort((a, b) => b[1] - a[1]).slice(0, 8).map(([t]) => t);
  if (seedTags.length < 2) { seedTags.length = 0; seedTags.push(...[...tagCounts.entries()].sort((a, b) => b[1] - a[1]).slice(0, 6).map(([t]) => t)); }
  const clusterMap = new Map<number, number>();
  for (const n of nodes) { const idx = n.tags.findIndex((t) => seedTags.includes(t)); if (idx >= 0) clusterMap.set(n.id, seedTags.indexOf(n.tags[idx])); }
  for (const n of nodes) { if (!clusterMap.has(n.id) && seedTags.length > 0) clusterMap.set(n.id, n.id % seedTags.length); }
  const counts = new Map<number, number>(); const tagSets = new Map<number, Set<string>>();
  for (const [nid, cid] of clusterMap) { counts.set(cid, (counts.get(cid) || 0) + 1); const nd = nodes.find((x) => x.id === nid); if (nd) { const s = tagSets.get(cid) || new Set<string>(); nd.tags.forEach((t) => s.add(t)); tagSets.set(cid, s); } }
  return { clusters: seedTags.map((tag, i) => ({ id: i, label: tag.charAt(0).toUpperCase() + tag.slice(1).replace(/_/g, " "), count: counts.get(i) || 0, tags: [...(tagSets.get(i) || [])].slice(0, 5) })).filter((c) => c.count > 0), clusterMap };
}

// ── Graph layout ─────────────────────────────────────────────────────────────

function layoutGraph(graphNodes: GraphNode[], _graphEdges: GraphEdge[], clusters: ConstellationData["clusters"]): Record<string, { x: number; y: number }> {
  const cx = VW / 2, cy = VH / 2, ringR = Math.min(VW, VH) * 0.36;
  const cc: Record<number, { x: number; y: number }> = {};
  clusters.forEach((c, i) => { const a = (i / clusters.length) * Math.PI * 2 - Math.PI / 2 + 0.3; cc[c.id] = { x: cx + Math.cos(a) * ringR, y: cy + Math.sin(a) * ringR }; });
  const pos: Record<string, { x: number; y: number }> = {};
  graphNodes.filter((n) => n.kind === "Cluster").forEach((n) => { const cid = parseInt(String(n.id).split(":")[1], 10); if (cc[cid]) pos[String(n.id)] = { ...cc[cid] }; });
  const byCluster: Record<number, GraphNode[]> = {};
  graphNodes.filter((n) => n.kind === "Lesson").forEach((n) => { (byCluster[n.cluster_id ?? -1] ||= []).push(n); });
  Object.entries(byCluster).forEach(([cidStr, list]) => {
    const cid = Number(cidStr), center = cc[cid];
    if (!center) { list.forEach((n, i) => { const a = (i / list.length) * Math.PI * 2; pos[String(n.id)] = { x: cx + Math.cos(a) * 100, y: cy + Math.sin(a) * 100 }; }); return; }
    const sorted = [...list].sort((a, b) => (b.weight || 0) - (a.weight || 0));
    const splitAt = sorted.length > 5 ? Math.ceil(sorted.length / 2) : sorted.length;
    sorted.forEach((n, i) => {
      const onOuter = i >= splitAt, ringList = onOuter ? sorted.slice(splitAt) : sorted.slice(0, splitAt);
      const ringIdx = onOuter ? i - splitAt : i, ringCount = ringList.length;
      const toC = Math.atan2(cy - center.y, cx - center.x), spread = Math.PI * 1.35, a0 = toC + Math.PI - spread / 2;
      const angle = a0 + (ringCount > 1 ? (ringIdx / (ringCount - 1)) * spread : spread / 2);
      pos[String(n.id)] = { x: center.x + Math.cos(angle) * (onOuter ? 230 : 150), y: center.y + Math.sin(angle) * (onOuter ? 230 : 150) };
    });
  });
  graphNodes.filter((n) => n.kind === "Evidence").forEach((n) => {
    const pid = String(n.id).split(":")[1], p = pos[pid], parent = graphNodes.find((x) => x.kind === "Lesson" && String(x.id) === pid);
    if (!p || !parent) { pos[String(n.id)] = { x: cx, y: cy }; return; }
    const center = cc[parent.cluster_id ?? -1]; if (!center) { pos[String(n.id)] = { x: p.x + 42, y: p.y }; return; }
    const dx = p.x - center.x, dy = p.y - center.y, d = Math.hypot(dx, dy) || 1;
    pos[String(n.id)] = { x: p.x + (dx / d) * 42, y: p.y + (dy / d) * 42 };
  });
  graphNodes.filter((n) => n.kind === "Tag").forEach((n, i) => { const a = (i / graphNodes.filter((x) => x.kind === "Tag").length) * Math.PI * 2; pos[String(n.id)] = { x: cx + Math.cos(a) * (70 + (i % 3) * 24), y: cy + Math.sin(a) * (70 + (i % 3) * 24) }; });
  // Repulsion
  const lids = graphNodes.filter((n) => n.kind === "Lesson").map((n) => String(n.id));
  const lc = Object.fromEntries(graphNodes.filter((n) => n.kind === "Lesson").map((n) => [String(n.id), n.cluster_id ?? -1]));
  for (let it = 0; it < 40; it++) for (let i = 0; i < lids.length; i++) for (let j = i + 1; j < lids.length; j++) {
    if (lc[lids[i]] !== lc[lids[j]]) continue;
    const pa = pos[lids[i]], pb = pos[lids[j]]; if (!pa || !pb) continue;
    const dx = pa.x - pb.x, dy = pa.y - pb.y, d = Math.sqrt(dx * dx + dy * dy + 0.01);
    if (d < 110) { const push = (110 - d) * 0.3, nx = dx / d, ny = dy / d; pa.x += nx * push; pa.y += ny * push; pb.x -= nx * push; pb.y -= ny * push; }
  }
  return pos;
}

// ── SVG Glyphs ───────────────────────────────────────────────────────────────

function GlyphFor({ kind, color, r = 16, selected = false }: { kind: GraphNodeKind; color: string; r?: number; selected?: boolean }) {
  const stroke = selected ? "#ffffff" : color, strokeW = selected ? 2.5 : 1.5, fill = `${color}30`;
  if (kind === "Cluster") { const pts = Array.from({ length: 6 }, (_, i) => { const a = (Math.PI / 3) * i - Math.PI / 2; return `${Math.cos(a) * r},${Math.sin(a) * r}`; }).join(" "); return <polygon points={pts} fill={fill} stroke={stroke} strokeWidth={strokeW} />; }
  if (kind === "Evidence") return <rect x={-r} y={-r} width={r * 2} height={r * 2} rx={r * 0.35} fill={fill} stroke={stroke} strokeWidth={strokeW} />;
  if (kind === "Tag") return <polygon points={`0,${-r} ${r},0 0,${r} ${-r},0`} fill={fill} stroke={stroke} strokeWidth={strokeW} />;
  return <circle r={r} fill={fill} stroke={stroke} strokeWidth={strokeW} />;
}

// ── Property Inspector ───────────────────────────────────────────────────────

function Inspector({ node, neighbors, onClose, onJump }: { node: GraphNode; neighbors: Array<{ other: GraphNode; type: GraphRelType; dir: "in" | "out"; similarity?: number }>; onClose: () => void; onJump: (id: string | number) => void }) {
  const color = node.kind === "Cluster" ? node.color || KIND_COLORS[node.kind] : KIND_COLORS[node.kind];
  const cypher = cypherPathFor(node);
  const [copied, setCopied] = useState(false);
  const props = useMemo(() => {
    const out: Array<[string, string | number]> = [];
    if (node.kind === "Lesson") { out.push(["id", node.id as number], ["title", nodeLabel(node)]); if (node.cluster_id != null) out.push(["cluster_id", node.cluster_id]); if (node.weight) out.push(["weight", node.weight]); if (node.source_type) out.push(["source_type", node.source_type]); if (node.source_ref) out.push(["source_ref", node.source_ref]); if (node.created_at) out.push(["created_at", node.created_at.slice(0, 10)]); if (node.tags?.length) out.push(["tags", node.tags.join(", ")]); }
    else if (node.kind === "Cluster") { out.push(["id", String(node.id).split(":")[1] || String(node.id)]); if (node.constellation) out.push(["constellation", node.constellation]); if (node.theme) out.push(["theme", node.theme]); if (node.color) out.push(["color", node.color]); }
    else if (node.kind === "Evidence") { out.push(["id", String(node.id)]); if (node.source_type) out.push(["source_type", node.source_type]); if (node.source_ref) out.push(["source_ref", node.source_ref]); if (node.created_at) out.push(["created_at", node.created_at.slice(0, 10)]); }
    else if (node.kind === "Tag") { out.push(["id", String(node.id)], ["name", node.label]); }
    return out;
  }, [node]);
  return (
    <div className="p-6">
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <div className="flex items-center gap-2 mb-1">
            <span className="h-4 w-4 flex items-center justify-center"><svg viewBox="-10 -10 20 20" className="h-full w-full"><GlyphFor kind={node.kind} color={color} r={8} selected /></svg></span>
            <span className="text-[9px] uppercase tracking-[0.22em] font-headline" style={{ color }}>:{node.kind}</span>
          </div>
          <div className="font-serif italic text-[22px] leading-snug">{node.kind === "Lesson" ? `\u201C${node.text}\u201D` : nodeLabel(node)}</div>
        </div>
        <button type="button" onClick={onClose} className="h-7 w-7 rounded-full hover:bg-white/10 text-[#64748B] hover:text-white flex items-center justify-center shrink-0 text-lg leading-none">&times;</button>
      </div>
      {node.kind === "Lesson" && node.context && <p className="mt-4 text-[12px] text-[#94A3B8] leading-relaxed">{node.context}</p>}
      <div className="mt-6 rounded-lg border border-white/[0.08] bg-black/40">
        <div className="flex items-center justify-between px-3 py-2 border-b border-white/[0.06]">
          <span className="text-[9px] uppercase tracking-[0.22em] text-[#7C6BF0] font-headline">Cypher</span>
          <button type="button" onClick={() => { navigator.clipboard?.writeText(cypher); setCopied(true); setTimeout(() => setCopied(false), 1400); }} className="text-[10px] text-[#94A3B8] hover:text-white">{copied ? "copied \u2713" : "copy"}</button>
        </div>
        <pre className="p-3 text-[11px] leading-relaxed overflow-x-auto" style={{ fontFamily: "var(--font-mono, monospace)", color: "#CBD5E1" }}>{cypher}</pre>
      </div>
      <div className="mt-5">
        <div className="text-[9px] uppercase tracking-[0.22em] text-[#64748B] mb-2 font-headline">Properties</div>
        <div className="rounded-lg border border-white/[0.06] overflow-hidden">
          {props.map(([k, v], i) => (<div key={k} className={`flex items-start gap-3 px-3 py-2 text-[11px] ${i !== props.length - 1 ? "border-b border-white/[0.04]" : ""}`}><span className="text-[#64748B] w-24 shrink-0" style={{ fontFamily: "var(--font-mono, monospace)" }}>{k}</span><span className="text-[#E2E8F0] min-w-0 break-words" style={{ fontFamily: "var(--font-mono, monospace)" }}>{String(v ?? "\u2014")}</span></div>))}
        </div>
      </div>
      {neighbors.length > 0 && (<div className="mt-5">
        <div className="flex items-center justify-between mb-2"><span className="text-[9px] uppercase tracking-[0.22em] text-[#64748B] font-headline">Relationships</span><span className="text-[9px] text-[#64748B]">{neighbors.length}</span></div>
        <div className="space-y-1">{neighbors.map((nb, i) => { const relC = REL_COLORS[nb.type]; const oC = nb.other.kind === "Cluster" ? nb.other.color || KIND_COLORS[nb.other.kind] : KIND_COLORS[nb.other.kind]; return (
          <button key={`nb-${i}`} type="button" onClick={() => onJump(nb.other.id)} className="w-full text-left rounded-lg border border-white/[0.06] hover:border-white/20 bg-white/[0.02] hover:bg-white/[0.05] px-3 py-2 flex items-center gap-2 transition group">
            <span className="text-[9px] shrink-0" style={{ color: relC, fontFamily: "var(--font-mono, monospace)" }}>{nb.dir === "out" ? "\u2192" : "\u2190"} :{nb.type}{nb.similarity ? `:${nb.similarity.toFixed(2)}` : ""}</span>
            <span className="h-3 w-px bg-white/10 mx-1" /><span className="h-2 w-2 rounded-full shrink-0" style={{ backgroundColor: oC }} />
            <span className="text-[11px] text-[#CBD5E1] group-hover:text-white truncate min-w-0 flex-1">{nodeLabel(nb.other)}</span>
          </button>); })}</div>
      </div>)}
    </div>
  );
}

// ── Main Page ────────────────────────────────────────────────────────────────

export default function ConstellationPage() {
  const [rawData, setRawData] = useState<ConstellationData>({ nodes: [], edges: [], affinity_edges: [], clusters: [] });
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [pendingCount, setPendingCount] = useState(0);

  useEffect(() => {
    let mounted = true;
    (async () => {
      try {
        const [cd, pl] = await Promise.all([fetchConstellation(), fetchPendingLessons()]);
        if (!mounted) return;
        setRawData(cd); setPendingCount(pl.length);
      } catch (err) { if (mounted) setError(err instanceof Error ? err.message : "Failed to load constellation."); }
      finally { if (mounted) setLoading(false); }
    })();
    return () => { mounted = false; };
  }, []);

  const effectiveData = useMemo(() => {
    if (rawData.clusters.length > 0 || !(rawData.nodes.length > 0 && rawData.nodes.every((n) => n.cluster_id == null))) return rawData;
    const { clusters, clusterMap } = clusterByTags(rawData.nodes);
    return { ...rawData, nodes: rawData.nodes.map((n) => { const cid = clusterMap.get(n.id); return cid != null ? { ...n, cluster_id: cid } : n; }), clusters };
  }, [rawData]);

  const graphData = useMemo(() => buildGraphData(effectiveData), [effectiveData]);
  const [kindFilter, setKindFilter] = useState<Set<GraphNodeKind>>(new Set(["Lesson", "Cluster"]));
  const [relFilter, setRelFilter] = useState<Set<GraphRelType>>(new Set(["IN_CLUSTER", "SIMILAR_TO", "REFINES"]));
  const [simThreshold] = useState(0.5);
  const [isolated, setIsolated] = useState<string | null>(null);
  const [showHint, setShowHint] = useState(true);

  const visibleNodes = useMemo(() => {
    let list = graphData.nodes.filter((n) => kindFilter.has(n.kind));
    if (isolated) {
      const isoCid = parseInt(String(isolated).split(":")[1], 10);
      list = list.filter((n) => {
        if (n.kind === "Cluster") return String(n.id) === String(isolated);
        if (n.kind === "Lesson") return n.cluster_id === isoCid;
        if (n.kind === "Evidence") { const p = graphData.nodes.find((x) => x.kind === "Lesson" && x.id === parseInt(String(n.id).split(":")[1], 10)); return p?.cluster_id === isoCid; }
        if (n.kind === "Tag") return graphData.edges.some((e) => e.type === "TAGGED_WITH" && String(e.target) === String(n.id) && graphData.nodes.some((x) => x.kind === "Lesson" && x.id === e.source && x.cluster_id === isoCid));
        return false;
      });
    }
    return list;
  }, [kindFilter, isolated, graphData]);

  const visibleIds = useMemo(() => new Set(visibleNodes.map((n) => String(n.id))), [visibleNodes]);
  const visibleEdges = useMemo(() => graphData.edges.filter((e) => relFilter.has(e.type) && visibleIds.has(String(e.source)) && visibleIds.has(String(e.target)) && !(e.type === "SIMILAR_TO" && (e.similarity || 0) < simThreshold)), [relFilter, visibleIds, simThreshold, graphData.edges]);

  // eslint-disable-next-line react-hooks/exhaustive-deps
  const baseLayout = useMemo(() => layoutGraph(visibleNodes, visibleEdges, effectiveData.clusters), [visibleNodes.length, visibleEdges.length, effectiveData.clusters]);
  const [positions, setPositions] = useState<Record<string, { x: number; y: number }>>(baseLayout);
  useEffect(() => { setPositions(baseLayout); }, [baseLayout]);

  const stageRef = useRef<HTMLElement>(null);
  const [stageSize, setStageSize] = useState({ w: 0, h: 0 });
  const [pan, setPan] = useState({ x: 0, y: 0 });
  const [zoom, setZoom] = useState(0.85);
  const dragRef = useRef<{ mode: "pan" | "node"; x: number; y: number; px: number; py: number; moved: number; nodeId?: string; offsetX?: number; offsetY?: number; sx?: number; sy?: number } | null>(null);

  // Measure stage — multiple strategies to guarantee non-zero dimensions
  useEffect(() => {
    function measure() {
      const el = stageRef.current;
      if (el) { const r = el.getBoundingClientRect(); if (r.width > 0 && r.height > 0) { setStageSize({ w: r.width, h: r.height }); return; } }
      setStageSize({ w: Math.max(400, window.innerWidth - 48), h: Math.max(400, window.innerHeight - 200) });
    }
    measure();
    const t1 = setTimeout(measure, 50), t2 = setTimeout(measure, 200);
    let ro: ResizeObserver | null = null;
    if (stageRef.current) { ro = new ResizeObserver(measure); ro.observe(stageRef.current); }
    window.addEventListener("resize", measure);
    return () => { clearTimeout(t1); clearTimeout(t2); ro?.disconnect(); window.removeEventListener("resize", measure); };
  }, []);

  const [selectedId, setSelectedId] = useState<string | number | null>(null);
  const [hover, setHover] = useState<string | number | null>(null);
  const [query, setQuery] = useState("");
  const baseScale = stageSize.w && stageSize.h ? Math.min(stageSize.w / VW, stageSize.h / VH) : 1;
  const scale = baseScale * zoom;
  const toScreen = useCallback((x: number, y: number) => ({ x: stageSize.w / 2 + (x - VW / 2) * scale + pan.x, y: stageSize.h / 2 + (y - VH / 2) * scale + pan.y }), [stageSize.w, stageSize.h, scale, pan.x, pan.y]);
  const screenToWorld = useCallback((sx: number, sy: number) => ({ x: (sx - stageSize.w / 2 - pan.x) / scale + VW / 2, y: (sy - stageSize.h / 2 - pan.y) / scale + VH / 2 }), [stageSize.w, stageSize.h, scale, pan.x, pan.y]);

  function onMouseDown(e: React.MouseEvent) {
    if ((e.target as HTMLElement).closest("[data-no-drag]")) return;
    const nodeEl = (e.target as SVGElement).closest("[data-graphnode]");
    if (nodeEl) { const nid = nodeEl.getAttribute("data-node-id") || ""; const sw = screenToWorld(e.clientX, e.clientY); const p = positions[nid] || { x: 0, y: 0 }; dragRef.current = { mode: "node", nodeId: nid, offsetX: p.x - sw.x, offsetY: p.y - sw.y, moved: 0, sx: e.clientX, sy: e.clientY, x: e.clientX, y: e.clientY, px: pan.x, py: pan.y }; }
    else { dragRef.current = { mode: "pan", x: e.clientX, y: e.clientY, px: pan.x, py: pan.y, moved: 0 }; }
  }
  function onMouseMove(e: React.MouseEvent) {
    const d = dragRef.current; if (!d) return;
    if (d.mode === "pan") { const dx = e.clientX - d.x, dy = e.clientY - d.y; d.moved = Math.max(d.moved, Math.hypot(dx, dy)); setPan({ x: d.px + dx, y: d.py + dy }); }
    else if (d.mode === "node" && d.nodeId) { const dx = e.clientX - (d.sx || 0), dy = e.clientY - (d.sy || 0); d.moved = Math.max(d.moved, Math.hypot(dx, dy)); if (d.moved > 3) { const w = screenToWorld(e.clientX, e.clientY); setPositions((prev) => ({ ...prev, [d.nodeId!]: { x: w.x + (d.offsetX || 0), y: w.y + (d.offsetY || 0) } })); } }
  }
  function onMouseUp() {
    const d = dragRef.current; dragRef.current = null; if (!d) return;
    if (d.mode === "pan" && d.moved < 4) { if (selectedId) setSelectedId(null); else if (isolated) setIsolated(null); }
    else if (d.mode === "node" && d.moved < 3 && d.nodeId) { const n = graphData.nodes.find((x) => String(x.id) === String(d.nodeId)); if (!n) return; if (n.kind === "Cluster") { setIsolated((prev) => (prev === d.nodeId! ? null : d.nodeId!)); setSelectedId(d.nodeId!); } else setSelectedId(d.nodeId!); }
  }
  function onWheel(e: React.WheelEvent) { e.preventDefault(); setZoom((z) => Math.max(0.3, Math.min(3, z * Math.exp(-e.deltaY * 0.0015)))); }
  function toggleKind(k: GraphNodeKind) { setKindFilter((s) => { const n = new Set(s); n.has(k) ? n.delete(k) : n.add(k); return n; }); setSelectedId(null); }
  function toggleRel(r: GraphRelType) { setRelFilter((s) => { const n = new Set(s); n.has(r) ? n.delete(r) : n.add(r); return n; }); }
  const selected = selectedId ? graphData.nodes.find((n) => String(n.id) === String(selectedId)) ?? null : null;
  const results = useMemo(() => { if (!query.trim()) return []; const q = query.toLowerCase(); return visibleNodes.filter((n) => nodeLabel(n).toLowerCase().includes(q) || (n.text || "").toLowerCase().includes(q) || String(n.id).includes(q)).slice(0, 8); }, [query, visibleNodes]);
  function focusNode(id: string | number) { const p = positions[String(id)]; if (!p) return; setPan({ x: -(p.x - VW / 2) * baseScale * 1.4, y: -(p.y - VH / 2) * baseScale * 1.4 }); setZoom(1.4); setSelectedId(id); }
  const neighbors = useMemo(() => { if (!selected) return []; return graphData.edges.filter((e) => String(e.source) === String(selected.id) || String(e.target) === String(selected.id)).map((e) => { const oid = String(e.source) === String(selected.id) ? e.target : e.source; const dir: "in" | "out" = String(e.source) === String(selected.id) ? "out" : "in"; const other = graphData.nodes.find((n) => String(n.id) === String(oid)); return other ? { other, type: e.type, dir, similarity: e.similarity } : null; }).filter((x): x is NonNullable<typeof x> => x != null); }, [selected, graphData]);

  if (error) return <div className="rounded-panel border border-rose-border bg-rose-bg px-3 py-2 text-sm text-rose-text">{error}</div>;
  if (loading) return <div className="flex items-center justify-center py-20"><p className="text-sm text-ink-muted">Loading your constellation...</p></div>;

  return (
    <div className="flex flex-col flex-1 -mt-4 relative text-[#E2E8F0]" style={{ background: "#04070b", minHeight: "calc(100vh - 120px)" }}>
      {/* Stage — flex child for real dimensions */}
      <section ref={stageRef} className="flex-1 relative overflow-hidden min-h-[500px] cursor-grab active:cursor-grabbing select-none"
        onMouseDown={onMouseDown} onMouseMove={onMouseMove} onMouseUp={onMouseUp} onMouseLeave={onMouseUp} onWheel={onWheel}>
        <div className="absolute inset-0 pointer-events-none" style={{ background: "radial-gradient(ellipse 80% 60% at 35% 40%, rgba(124,107,240,0.06) 0%, transparent 55%), radial-gradient(ellipse 70% 50% at 75% 65%, rgba(78,205,196,0.05) 0%, transparent 55%), radial-gradient(circle at 50% 100%, rgba(232,180,184,0.04) 0%, transparent 60%), #04070b" }} />
        <div className="absolute inset-0 pointer-events-none opacity-[0.04]" style={{ backgroundImage: "linear-gradient(#7C6BF0 1px, transparent 1px), linear-gradient(90deg, #7C6BF0 1px, transparent 1px)", backgroundSize: `${48 * zoom}px ${48 * zoom}px`, backgroundPosition: `${pan.x}px ${pan.y}px` }} />

        {/* Header */}
        <div data-no-drag className="absolute top-0 left-0 right-0 z-30 px-4 sm:px-6 pt-4 pb-3 flex items-start gap-3 flex-wrap">
          <div className="min-w-0">
            <div className="flex items-center gap-2"><span className="text-[10px] uppercase tracking-[0.24em] text-[#7C6BF0] font-headline">Constellation Graph</span></div>
            <div className="mt-1 flex items-center gap-4 text-[11px] text-[#64748B]">
              <span><span className="text-white">{visibleNodes.length}</span> nodes</span>
              <span><span className="text-white">{visibleEdges.length}</span> relationships</span>
              {pendingCount > 0 && <Link href="/constellation/pending" className="rounded-full border border-[#7C6BF0]/40 bg-[#7C6BF0]/10 px-2.5 py-0.5 text-[10px] font-semibold text-[#9B8DF5] hover:bg-[#7C6BF0]/20 transition">{pendingCount} waiting</Link>}
            </div>
          </div>
          <div className="flex-1 min-w-[200px] max-w-[420px] relative">
            <div className="rounded-xl border border-white/10 bg-black/50 backdrop-blur-xl px-3 py-2 flex items-center gap-2">
              <svg width="14" height="14" viewBox="0 0 24 24" fill="none" className="text-[#7C6BF0] shrink-0"><circle cx="11" cy="11" r="7" stroke="currentColor" strokeWidth="2"/><path d="M16 16l4.5 4.5" stroke="currentColor" strokeWidth="2" strokeLinecap="round"/></svg>
              <input value={query} onChange={(e) => setQuery(e.target.value)} placeholder="Search lessons, clusters, tags..." className="flex-1 bg-transparent outline-none text-[12px] text-white placeholder:text-[#475569]" style={{ fontFamily: "var(--font-mono, monospace)" }} />
              {query && <button type="button" onClick={() => setQuery("")} className="text-[#64748B] hover:text-white text-[10px]">clear</button>}
            </div>
            {results.length > 0 && (<div className="absolute left-0 right-0 top-full mt-1 rounded-xl border border-white/10 bg-black/85 backdrop-blur-xl overflow-hidden z-40">
              {results.map((n) => { const c = n.kind === "Cluster" ? n.color || KIND_COLORS[n.kind] : KIND_COLORS[n.kind]; return (<button key={String(n.id)} type="button" onClick={() => { focusNode(n.id); setQuery(""); }} className="w-full text-left px-3 py-2 hover:bg-white/[0.04] flex items-center gap-2 border-b border-white/[0.04] last:border-0">
                <span className="h-1.5 w-1.5 rounded-full shrink-0" style={{ backgroundColor: c, boxShadow: `0 0 6px ${c}` }} />
                <span className="text-[9px] uppercase tracking-[0.2em] text-[#64748B] w-16 shrink-0 font-headline">{n.kind}</span>
                <span className="text-[11px] text-white truncate flex-1">{nodeLabel(n)}</span>
              </button>); })}
            </div>)}
          </div>
          <div className="hidden sm:flex items-center gap-1 rounded-full border border-white/10 bg-black/50 backdrop-blur-xl p-1">
            <button type="button" onClick={() => setZoom((z) => Math.max(0.3, z * 0.8))} className="h-7 w-7 rounded-full hover:bg-white/10 text-[#94A3B8] hover:text-white flex items-center justify-center" aria-label="Zoom out">&minus;</button>
            <span className="text-[10px] text-[#64748B] w-10 text-center tabular-nums" style={{ fontFamily: "var(--font-mono, monospace)" }}>{(zoom * 100).toFixed(0)}%</span>
            <button type="button" onClick={() => setZoom((z) => Math.min(3, z * 1.25))} className="h-7 w-7 rounded-full hover:bg-white/10 text-[#94A3B8] hover:text-white flex items-center justify-center" aria-label="Zoom in">+</button>
            <span className="h-4 w-px bg-white/10 mx-0.5" />
            <button type="button" onClick={() => setPositions(baseLayout)} title="Relayout" className="px-2.5 h-7 rounded-full hover:bg-white/10 text-[#94A3B8] hover:text-white text-[10px] uppercase tracking-wider flex items-center gap-1.5 font-headline">
              <svg width="10" height="10" viewBox="0 0 10 10" fill="none" aria-hidden="true"><path d="M8 2v3H5M2 8V5h3" stroke="currentColor" strokeWidth="1" strokeLinecap="round" strokeLinejoin="round" /><path d="M8 5a3 3 0 11-5.2-2M2 5a3 3 0 015.2 2" stroke="currentColor" strokeWidth="1" strokeLinecap="round" /></svg>Relayout</button>
            <button type="button" onClick={() => { setPan({ x: 0, y: 0 }); setZoom(0.85); setSelectedId(null); setIsolated(null); }} className="px-2.5 h-7 rounded-full hover:bg-white/10 text-[#94A3B8] hover:text-white text-[10px] uppercase tracking-wider font-headline">Reset view</button>
          </div>
        </div>

        {/* Hint / isolation breadcrumb */}
        {(isolated || showHint) && (<div data-no-drag className="absolute top-[72px] sm:top-[76px] left-4 sm:left-6 z-30 flex items-center gap-2 flex-wrap">
          {isolated && (() => { const isoNode = graphData.nodes.find((n) => String(n.id) === String(isolated)); if (!isoNode) return null; const c = isoNode.color || KIND_COLORS.Cluster; return (
            <button type="button" onClick={() => setIsolated(null)} className="group flex items-center gap-2 pl-2 pr-2.5 h-7 rounded-full border bg-black/70 backdrop-blur-xl hover:bg-white/10 transition" style={{ borderColor: c + "66" }}>
              <svg width="10" height="10" viewBox="0 0 10 10" className="text-[#94A3B8] group-hover:text-white" aria-hidden="true"><path d="M3 3l4 4M7 3l-4 4" stroke="currentColor" strokeWidth="1.2" strokeLinecap="round" /></svg>
              <span className="text-[9px] uppercase tracking-[0.22em] font-headline" style={{ color: c }}>Focused</span>
              <span className="text-[11px] text-white font-serif italic">{isoNode.constellation || isoNode.label}</span>
            </button>); })()}
          {showHint && !isolated && (<div className="flex items-center gap-2 pl-2.5 pr-2 h-7 rounded-full border border-white/10 bg-black/60 backdrop-blur-xl text-[10px] text-[#94A3B8]">
            <span style={{ fontFamily: "var(--font-mono, monospace)" }}>Positions are approximate &middot; drag to rearrange &middot; click a cluster to focus</span>
            <button type="button" onClick={() => setShowHint(false)} className="ml-1 text-[#64748B] hover:text-white px-1">&times;</button>
          </div>)}
        </div>)}

        {/* SVG graph */}
        {stageSize.w > 0 && (<svg className="absolute inset-0 w-full h-full" style={{ overflow: "visible" }}>
          <defs>{Object.entries(REL_COLORS).map(([type, color]) => (<marker key={`m-${type}`} id={`arrow-${type}`} viewBox="0 -5 10 10" refX={9} refY={0} markerWidth={6} markerHeight={6} orient="auto"><path d="M0,-4 L9,0 L0,4" fill={color} opacity="0.75" /></marker>))}</defs>
          <g>{visibleEdges.map((e, i) => {
            const sP = positions[String(e.source)], tP = positions[String(e.target)]; if (!sP || !tP) return null;
            const s = toScreen(sP.x, sP.y), t = toScreen(tP.x, tP.y), color = REL_COLORS[e.type] || "#64748B";
            const isTouching = selectedId != null && (String(e.source) === String(selectedId) || String(e.target) === String(selectedId));
            const dimmed = selectedId != null && !isTouching;
            const op = dimmed ? 0.15 : isTouching ? 1 : e.type === "SIMILAR_TO" ? Math.max(0.4, e.similarity || 0.5) : 0.7;
            const mx = (s.x + t.x) / 2, my = (s.y + t.y) / 2, dx = t.x - s.x, dy = t.y - s.y, d = Math.hypot(dx, dy) || 1;
            const nx = -dy / d, ny = dx / d, curve = 10 + ((i * 7) % 14), qx = mx + nx * curve, qy = my + ny * curve;
            return (<g key={`e-${i}`} style={{ transition: "opacity 200ms", opacity: op }}>
              <path d={`M ${s.x} ${s.y} Q ${qx} ${qy} ${t.x} ${t.y}`} fill="none" stroke={color} strokeWidth={isTouching ? 1.5 : e.type === "IN_CLUSTER" ? 1 : 1.1} strokeDasharray={e.type === "TAGGED_WITH" ? "2 4" : e.type === "REFINES" ? "6 3" : undefined} markerEnd={e.type !== "SIMILAR_TO" ? `url(#arrow-${e.type})` : undefined} />
              {isTouching && (<g><rect x={qx - e.type.length * 3.2} y={qy - 7} width={e.type.length * 6.4 + 2} height={14} rx={4} fill="#04070b" opacity="0.85" /><text x={qx} y={qy + 3} textAnchor="middle" fill={color} fontSize="9" style={{ fontFamily: "var(--font-mono, monospace)", letterSpacing: "0.08em" }}>{e.type}{e.type === "SIMILAR_TO" && e.similarity ? `:${e.similarity.toFixed(2)}` : ""}</text></g>)}
            </g>);
          })}</g>
          <g>{visibleNodes.map((n) => {
            const p = positions[String(n.id)]; if (!p) return null;
            const s = toScreen(p.x, p.y), color = n.kind === "Cluster" ? n.color || KIND_COLORS[n.kind] : KIND_COLORS[n.kind];
            const r = nodeRadius(n, zoom), isSel = String(selectedId) === String(n.id), isHov = String(hover) === String(n.id);
            const isTouching = selected && neighbors.some((x) => String(x.other.id) === String(n.id));
            const dimmed = selected && !isSel && !isTouching;
            const label = nodeLabel(n), showLabel = isSel || isHov || isTouching || n.kind === "Cluster";
            return (<g key={`n-${n.id}`} transform={`translate(${s.x} ${s.y})`} data-graphnode data-node-id={String(n.id)} style={{ cursor: "grab", opacity: dimmed ? 0.3 : 1, transition: "opacity 200ms" }} onMouseEnter={() => setHover(n.id)} onMouseLeave={() => setHover(null)}>
              {(isSel || isHov) && <circle r={r * 1.8} fill={color} opacity={0.3} style={{ filter: "blur(6px)" }} />}
              <GlyphFor kind={n.kind} color={color} r={r} selected={isSel} />
              {n.kind === "Cluster" && <text y="3" textAnchor="middle" fill={color} fontSize="9" className="font-headline" style={{ letterSpacing: "0.12em", textTransform: "uppercase" }}>{n.theme?.split(" ")[0] || "CL"}</text>}
              {showLabel && (<g transform={`translate(0 ${r + 14})`}><rect x={-(Math.min(label.length, 40) * 3.2) - 4} y={-9} width={Math.min(label.length, 40) * 6.4 + 8} height={16} rx={4} fill="#04070b" opacity={isSel ? 0.95 : 0.75} stroke={isSel ? color : "transparent"} strokeWidth="0.5" /><text textAnchor="middle" y="3" fill={isSel ? "#fff" : "#CBD5E1"} fontSize="10" className={n.kind === "Lesson" ? "font-serif" : "font-headline"} fontStyle={n.kind === "Lesson" ? "italic" : "normal"}>{label.length > 40 ? label.slice(0, 38) + "\u2026" : label}</text></g>)}
            </g>);
          })}</g>
        </svg>)}

        {/* Legend */}
        <div data-no-drag className="absolute left-4 bottom-4 z-30 rounded-xl border border-white/10 bg-black/70 backdrop-blur-xl p-3 text-[11px] max-w-[240px] hidden sm:block">
          <div className="text-[9px] uppercase tracking-[0.22em] text-[#64748B] mb-2 font-headline">Labels</div>
          <div className="space-y-1.5 mb-3">{ALL_KINDS.map((k) => { const c = KIND_COLORS[k]; const on = kindFilter.has(k); return (
            <button key={k} type="button" onClick={() => toggleKind(k)} className="w-full flex items-center gap-2 text-left group">
              <span className="h-3.5 w-3.5 flex items-center justify-center shrink-0"><svg viewBox="-10 -10 20 20" className="h-full w-full"><GlyphFor kind={k} color={c} r={7.5} selected={false} /></svg></span>
              <span className="text-[10px]" style={{ fontFamily: "var(--font-mono, monospace)", color: on ? c : "#475569" }}>:{k}</span>
              <span className={`ml-auto text-[9px] ${on ? "text-[#94A3B8]" : "text-[#475569]"}`}>{graphData.nodes.filter((x) => x.kind === k).length}</span>
            </button>); })}</div>
          <div className="text-[9px] uppercase tracking-[0.22em] text-[#64748B] mb-2 font-headline">Relationships</div>
          <div className="space-y-1.5">{ALL_RELS.map((r) => { const c = REL_COLORS[r]; const on = relFilter.has(r); return (
            <button key={r} type="button" onClick={() => toggleRel(r)} className="w-full flex items-center gap-2 text-left group">
              <span className="w-5 h-[2px] rounded-full shrink-0" style={{ background: c, opacity: on ? 1 : 0.3, boxShadow: on ? `0 0 4px ${c}` : "none" }} />
              <span className="text-[10px]" style={{ fontFamily: "var(--font-mono, monospace)", color: on ? c : "#475569" }}>:{r}</span>
            </button>); })}</div>
        </div>

        {/* Inspector */}
        {selected && (<aside data-no-drag className="absolute md:relative right-0 top-0 bottom-0 z-30 w-full sm:w-[380px] md:w-[400px] border-l border-white/[0.08] bg-[#06090e]/95 backdrop-blur-2xl overflow-y-auto animate-slide-in" style={{ boxShadow: "-24px 0 60px rgba(0,0,0,0.5)" }}>
          <Inspector node={selected} neighbors={neighbors} onClose={() => setSelectedId(null)} onJump={focusNode} />
        </aside>)}

        {/* Empty state */}
        {effectiveData.nodes.length === 0 && (<div className="absolute inset-0 flex items-center justify-center z-20 pointer-events-none"><div className="text-center max-w-sm px-4">
          <p className="text-lg font-headline font-bold text-white">Your constellation begins here</p>
          <p className="mt-2 text-sm text-[#94A3B8]">As you chat with your assistant, lessons will appear as nodes in your personal graph.</p>
        </div></div>)}

        <style jsx>{`@keyframes slide-in { from { transform: translateX(24px); opacity: 0; } to { transform: translateX(0); opacity: 1; } } .animate-slide-in { animation: slide-in 260ms ease-out both; }`}</style>
      </section>
    </div>
  );
}
