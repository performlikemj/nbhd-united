/**
 * Constellation game — Phaser scene. Ported from the standalone prototype,
 * re-skinned to the product design system and driven by injected GalaxyData
 * (no fetch / no mock). The DOM overlay (landing panel + nega-self sheet) is
 * rendered by the React host and queried within its subtree — no global ids.
 */
import Phaser from "phaser";

import {
  buildTaunt,
  chooseReframes,
  detectGaps,
  type GalaxyData,
  type GalaxyStar,
  type Gap,
  type StarStage,
} from "./encounter-logic";

const CONFIG = {
  WORLD: { w: 3600, h: 2400 },
  DOCK_RADIUS: 100,
  TRIGGER_RADIUS: 260,
  SHIP: { accel: 380, turn: 3.3, drag: 95, maxVel: 360 },
};

// Star stages re-skinned to the product palette: slate → teal → purple → gold.
const STAGE: Record<StarStage, { size: number; color: number; glow: number; pulse: boolean }> = {
  proto: { size: 9, color: 0x5b6b8c, glow: 0.78, pulse: false },
  ignited: { size: 12, color: 0x4ecdc4, glow: 1.0, pulse: false },
  radiant: { size: 16, color: 0x7c6bf0, glow: 1.25, pulse: true },
  supernova: { size: 24, color: 0xf5c878, glow: 1.75, pulse: true },
};
const STAGE_FALLBACK = STAGE.proto;
const SHADOW = 0xff5d7e;
const BEAM = 0x9bd9ff;
const EDGE = 0x6f7fb8; // within-cluster link (cool, dim)
const SIMILAR = 0x8b7cf0; // cross-cluster SIMILAR_TO bridge (the semantic web — matches the graph's purple)

// Curated, high-separation palette — the SAME hues the 2D constellation graph
// uses, so a cluster reads as the same colour family in both views. HSV hue
// stepping produced muddy, near-identical neighbours; a fixed palette doesn't.
const CLUSTER_PALETTE = [0x7c6bf0, 0xe8b4b8, 0x4ecdc4, 0xfbbf24, 0x60a5fa, 0xf472b6, 0x34d399, 0xfb923c];
function clusterColor(cid: number | null): number {
  if (cid === null || cid === undefined) return 0x9fb0c8;
  return CLUSTER_PALETTE[Math.abs(cid) % CLUSTER_PALETTE.length];
}

// Lift a colour toward white by `amt` (0..1) — for the luminous inner wisps of a
// nebula, where the cloud glows hottest.
function lighten(color: number, amt: number): number {
  const c = Phaser.Display.Color.IntegerToColor(color);
  return Phaser.Display.Color.GetColor(
    Math.round(c.red + (255 - c.red) * amt),
    Math.round(c.green + (255 - c.green) * amt),
    Math.round(c.blue + (255 - c.blue) * amt),
  );
}

/**
 * Semantic galaxy layout. The backend already computes a 2D PCA projection of the
 * lesson embeddings (normalised ~[-1,1], with inter-cluster spacing) — the SAME
 * structure the constellation graph draws. We map those coordinates into world
 * space so the galaxy is meaningful: similar lessons sit together, each
 * constellation lands where its embedding centroid is, and lone stars drift to
 * their own real spot — instead of a mechanical grid + a hashed lattice (which
 * scattered unclustered stars onto diagonal rows because the old `hashSeed`,
 * `id × 761 mod 1000`, is an arithmetic progression for sequential ids).
 */
function layoutStars(stars: GalaxyStar[]): {
  pos: Record<number, { x: number; y: number }>;
  world: { w: number; h: number };
} {
  const pos: Record<number, { x: number; y: number }> = {};
  const hasXY = (s: GalaxyStar) =>
    s.x !== null && s.y !== null && isFinite(s.x as number) && isFinite(s.y as number);
  const valid = stars.filter(hasXY);

  // World scaled to the star count so there's room to fly between groups.
  const BASE = Math.max(3200, Math.round(Math.sqrt(stars.length) * 560));

  // Degenerate fallback (no coordinates at all): a calm ring, never a lattice.
  if (valid.length < 2) {
    const world = { w: BASE, h: Math.max(2200, Math.round(BASE * 0.66)) };
    const R = Math.min(world.w, world.h) * 0.32;
    stars.forEach((s, i) => {
      const a = (i / Math.max(1, stars.length)) * Math.PI * 2;
      pos[s.id] = { x: world.w / 2 + Math.cos(a) * R, y: world.h / 2 + Math.sin(a) * R };
    });
    return { pos, world };
  }

  // PCA bounds → world mapping (preserve the data's aspect ratio, with margin).
  const xs = valid.map((s) => s.x as number);
  const ys = valid.map((s) => s.y as number);
  const minX = Math.min(...xs);
  const maxX = Math.max(...xs);
  const minY = Math.min(...ys);
  const maxY = Math.max(...ys);
  const spanX = maxX - minX || 1;
  const spanY = maxY - minY || 1;
  const worldW = BASE;
  const worldH = Math.max(2200, Math.round(BASE * Math.min(1.5, Math.max(0.6, spanY / spanX))));
  const PAD = 0.1;
  const toWorld = (x: number, y: number) => ({
    x: (((x - minX) / spanX) * (1 - 2 * PAD) + PAD) * worldW,
    y: (((y - minY) / spanY) * (1 - 2 * PAD) + PAD) * worldH,
  });

  // Cluster PCA centroids + a per-cluster scale so every constellation reads at a
  // consistent on-screen size regardless of how tight/loose it is in PCA space.
  const byCluster = new Map<number, GalaxyStar[]>();
  for (const s of valid) {
    if (s.cluster_id === null || s.cluster_id === undefined) continue;
    const arr = byCluster.get(s.cluster_id) ?? [];
    arr.push(s);
    byCluster.set(s.cluster_id, arr);
  }
  const INTRA = Math.max(340, Math.round(Math.min(worldW, worldH) * 0.1));
  const centroid = new Map<number, { x: number; y: number }>();
  const localScale = new Map<number, number>();
  for (const [cid, arr] of byCluster) {
    let mx = 0;
    let my = 0;
    for (const s of arr) {
      mx += s.x as number;
      my += s.y as number;
    }
    const c = { x: mx / arr.length, y: my / arr.length };
    centroid.set(cid, c);
    let maxD = 0;
    for (const s of arr) maxD = Math.max(maxD, Math.hypot((s.x as number) - c.x, (s.y as number) - c.y));
    localScale.set(cid, maxD > 1e-6 ? INTRA / maxD : 0);
  }

  // Deliberately separate cluster centroids so each neighbourhood owns a region
  // of space with void around it. PCA centroids alone often sit cramped near the
  // middle, which blurs the constellations into one cloud. Seed from the PCA
  // centroid (keeps "similar clusters near each other"), then relax pairwise to a
  // minimum gap, so even a tightly-embedded galaxy reads as distinct neighbourhoods.
  const cids = [...byCluster.keys()];
  const cw = new Map<number, { x: number; y: number }>();
  for (const cid of cids) { const c = centroid.get(cid)!; cw.set(cid, toWorld(c.x, c.y)); }
  const SEP = INTRA * 3.7; // centre-to-centre floor — generous void for long glides between neighbourhoods
  for (let it = 0; it < 80; it++) {
    for (let i = 0; i < cids.length; i++) for (let j = i + 1; j < cids.length; j++) {
      const a = cw.get(cids[i])!, b = cw.get(cids[j])!;
      const dx = a.x - b.x, dy = a.y - b.y, d = Math.hypot(dx, dy) || 0.01;
      if (d < SEP) { const push = (SEP - d) * 0.5, nx = dx / d, ny = dy / d; a.x += nx * push; a.y += ny * push; b.x -= nx * push; b.y -= ny * push; }
    }
  }

  for (const s of stars) {
    if (!hasXY(s)) {
      // No coordinates — tuck near the middle deterministically (rare).
      pos[s.id] = { x: worldW * 0.5 + (((s.id * 37) % 11) - 5) * 34, y: worldH * 0.5 + (((s.id * 53) % 11) - 5) * 34 };
      continue;
    }
    const cid = s.cluster_id;
    if (cid !== null && cid !== undefined && cw.has(cid)) {
      const c = centroid.get(cid) as { x: number; y: number };
      const center = cw.get(cid) as { x: number; y: number };
      const k = localScale.get(cid) as number;
      pos[s.id] = { x: center.x + ((s.x as number) - c.x) * k, y: center.y + ((s.y as number) - c.y) * k };
    } else {
      // Lone star at its own embedding position — its place in the galaxy is real.
      pos[s.id] = toWorld(s.x as number, s.y as number);
    }
  }

  // Re-fit the world to the separated layout (+ margin) so nothing clips the
  // bounds and the minimap frames the whole galaxy.
  let minPX = Infinity, minPY = Infinity, maxPX = -Infinity, maxPY = -Infinity;
  for (const id in pos) {
    const p = pos[id];
    if (p.x < minPX) minPX = p.x;
    if (p.y < minPY) minPY = p.y;
    if (p.x > maxPX) maxPX = p.x;
    if (p.y > maxPY) maxPY = p.y;
  }
  const MARGIN = INTRA * 0.8;
  for (const id in pos) { pos[id].x += MARGIN - minPX; pos[id].y += MARGIN - minPY; }
  const finalW = Math.max(BASE, Math.round(maxPX - minPX + MARGIN * 2));
  const finalH = Math.max(2200, Math.round(maxPY - minPY + MARGIN * 2));

  return { pos, world: { w: finalW, h: finalH } };
}

const truncate = (str: string, n: number) => (str && str.length > n ? str.slice(0, n - 1) + "…" : str || "");

interface StarEntry {
  id: number;
  data: GalaxyStar;
  x: number;
  y: number;
  r: number;
  glow: Phaser.GameObjects.Image;
  core: Phaser.GameObjects.Image;
  label: Phaser.GameObjects.Text;
  visited: boolean;
}

interface Encounter {
  gap: Gap;
  cx: number;
  cy: number;
  resolved: boolean;
  cooldownUntil: number;
  nega: any;
  // Arms only once the ship has been clear of the trigger radius — so spawning
  // inside a neighbourhood never insta-fires the duel before you've oriented.
  armed: boolean;
}

export class GalaxyScene extends Phaser.Scene {
  private galaxy: GalaxyData;
  private overlayRoot: HTMLElement;
  private stars: StarEntry[] = [];
  private candidate: StarEntry | null = null;
  private paused = false;
  private autopilot: { x: number; y: number; star: StarEntry | null } | null = null;
  private touch = { active: false, anchorX: 0, anchorY: 0, curX: 0, curY: 0, downTime: 0, moved: 0 };
  private encounters: Encounter[] = [];
  private encounterActive: Encounter | null = null;
  private encPrevZoom = 1;
  private world = { w: 3600, h: 2400 };
  private miniContainer?: Phaser.GameObjects.Container;
  private miniMarker?: Phaser.GameObjects.Graphics;
  private miniView?: Phaser.GameObjects.Graphics;
  private miniScale = 0;
  private miniPad = 8;
  private miniW = 0;
  private miniH = 0;
  private miniMaxCur = 0;
  private miniResizeBound = false;

  private ship!: any;
  private flame!: Phaser.GameObjects.Particles.ParticleEmitter;
  private ring!: Phaser.GameObjects.Graphics;
  private prompt!: Phaser.GameObjects.Text;
  private cursors!: Phaser.Types.Input.Keyboard.CursorKeys;
  private keys!: any;
  private landBtn!: HTMLElement | null;
  private encEl!: HTMLElement | null;
  // Each cluster as a place: a wispy nebula cloud (many soft puffs), a floating
  // name beacon, and its member stars — so proximity can brighten the whole
  // neighbourhood (cloud + its stars) as the ship nears.
  private neighborhoods: { cid: number; cx: number; cy: number; r: number; n: number; color: number; cloud: Phaser.GameObjects.Image[]; name: Phaser.GameObjects.Text; stars: StarEntry[] }[] = [];
  private neighborhoodR = 400;

  constructor(galaxy: GalaxyData, overlayRoot: HTMLElement) {
    super("galaxy");
    this.galaxy = galaxy;
    this.overlayRoot = overlayRoot;
  }

  private q<T extends HTMLElement = HTMLElement>(sel: string): T | null {
    return this.overlayRoot.querySelector<T>(sel);
  }

  create() {
    this.makeTextures();
    const { pos, world } = layoutStars(this.galaxy.stars);
    this.world = world;
    this.physics.world.setBounds(0, 0, world.w, world.h);
    this.cameras.main.setBounds(0, 0, world.w, world.h);
    this.cameras.main.setBackgroundColor("#0b0f13");

    this.buildStarfield();
    this.buildNeighborhoods(pos);
    this.buildEdges(pos);
    this.buildStars(pos);
    this.attachStarsToNeighborhoods();
    const spawn = this.pickSpawn();
    this.buildShip(spawn.x, spawn.y);
    this.buildHUD();
    this.buildInput();
    this.buildMinimap();

    this.ring = this.add.graphics().setDepth(6);
    this.prompt = this.add
      .text(0, 0, "[E] Land", {
        fontSize: "13px",
        color: "#dbe7ff",
        fontStyle: "bold",
        backgroundColor: "rgba(11,15,19,0.72)",
        padding: { x: 7, y: 4 },
      })
      .setOrigin(0.5, 1)
      .setDepth(7)
      .setVisible(false);

    this.encEl = this.q("#cg-encounter");
    this.wirePanel();
    this.buildTouch();
    this.buildEncounters(pos);
  }

  private makeTextures() {
    const gg = this.make.graphics({ x: 0, y: 0 }, false);
    for (let i = 26; i > 0; i--) {
      gg.fillStyle(0xffffff, 0.035);
      gg.fillCircle(64, 64, i * 2.4);
    }
    gg.generateTexture("glow", 128, 128);
    gg.destroy();

    // Very soft, wide-falloff blob for nebula wisps — softer than "glow" so many
    // overlapping copies read as an organic cloud rather than stacked discs.
    const pf = this.make.graphics({ x: 0, y: 0 }, false);
    for (let i = 40; i > 0; i--) {
      pf.fillStyle(0xffffff, 0.018);
      pf.fillCircle(80, 80, i * 2);
    }
    pf.generateTexture("puff", 160, 160);
    pf.destroy();

    const cg = this.make.graphics({ x: 0, y: 0 }, false);
    cg.fillStyle(0xffffff, 1);
    cg.fillCircle(8, 8, 8);
    cg.generateTexture("core", 16, 16);
    cg.destroy();

    const sg = this.make.graphics({ x: 0, y: 0 }, false);
    sg.fillStyle(0xa5b4ff, 1);
    sg.fillTriangle(30, 12, 4, 3, 4, 21);
    sg.fillStyle(0x7c6bf0, 1);
    sg.fillTriangle(30, 12, 6, 7, 6, 17);
    sg.lineStyle(1, 0xffffff, 0.55);
    sg.strokeTriangle(30, 12, 4, 3, 4, 21);
    sg.generateTexture("ship", 34, 24);
    sg.destroy();

    const ng = this.make.graphics({ x: 0, y: 0 }, false);
    ng.fillStyle(0x1c0a15, 1);
    ng.fillTriangle(30, 12, 4, 1, 4, 23);
    ng.fillStyle(0x33121f, 1);
    ng.fillTriangle(28, 12, 8, 6, 8, 18);
    ng.lineStyle(2, SHADOW, 0.95);
    ng.strokeTriangle(30, 12, 4, 1, 4, 23);
    ng.fillStyle(0xff2d55, 1);
    ng.fillCircle(11, 12, 2.2);
    ng.generateTexture("nega", 34, 24);
    ng.destroy();
  }

  private buildStarfield() {
    const W = this.world.w;
    const H = this.world.h;
    const density = Math.min(6, (W * H) / (3600 * 2400)); // keep the void starry as the world grows
    const layers = [
      { n: Math.round(240 * density), alpha: 0.5, sf: 0.35, r: 1.2 },
      { n: Math.round(140 * density), alpha: 0.8, sf: 0.6, r: 1.6 },
    ];
    for (const L of layers) {
      const g = this.add.graphics().setScrollFactor(L.sf).setDepth(0);
      for (let i = 0; i < L.n; i++) {
        g.fillStyle(0xcfe0ff, L.alpha * (0.4 + Math.random() * 0.6));
        g.fillCircle(Math.random() * W * 1.4, Math.random() * H * 1.4, L.r * (0.6 + Math.random()));
      }
    }
  }

  private buildEdges(pos: Record<number, { x: number; y: number }>) {
    // Draw the SIMILAR_TO web — INCLUDING cross-cluster links. Those bridges are
    // exactly what makes the galaxy read as semantic (the way the constellation
    // graph does), so they're drawn brighter in the graph's purple; within-cluster
    // links stay cooler and dimmer. Capped per node to a few strongest to avoid a
    // hairball, and visible enough that the connections actually register.
    const clusterById: Record<number, number | null> = {};
    for (const s of this.galaxy.stars) clusterById[s.id] = s.cluster_id;
    const K = 3;
    const perNode: Record<number, { other: number; sim: number }[]> = {};
    for (const e of this.galaxy.edges) {
      const sim = e.similarity ?? 0.4;
      (perNode[e.source] ??= []).push({ other: e.target, sim });
      (perNode[e.target] ??= []).push({ other: e.source, sim });
    }
    const g = this.add.graphics().setDepth(2);
    const drawn = new Set<string>();
    for (const idStr of Object.keys(perNode)) {
      const id = Number(idStr);
      const top = perNode[id].sort((a, b) => b.sim - a.sim).slice(0, K);
      for (const { other, sim } of top) {
        const key = id < other ? `${id}:${other}` : `${other}:${id}`;
        if (drawn.has(key)) continue;
        drawn.add(key);
        const a = pos[id];
        const b = pos[other];
        if (!a || !b) continue;
        const cross = clusterById[id] !== clusterById[other];
        if (cross) {
          // Cross-cluster bridges are the long crisscross lines that read as a
          // spiderweb — keep them as a faint suggestion, not a structural mesh.
          g.lineStyle(1, SIMILAR, 0.07 + sim * 0.16);
        } else {
          g.lineStyle(1, EDGE, 0.05 + sim * 0.12);
        }
        g.lineBetween(a.x, a.y, b.x, b.y);
      }
    }
  }

  private buildStars(pos: Record<number, { x: number; y: number }>) {
    for (const data of this.galaxy.stars) {
      const p = pos[data.id];
      if (!p) continue;
      const cfg = STAGE[data.star_stage] || STAGE_FALLBACK;
      // Tint by CLUSTER (the constellation) so groups read as colour families;
      // stage drives size + glow intensity (development within a constellation).
      const tint = clusterColor(data.cluster_id);
      const glow = this.add
        .image(p.x, p.y, "glow")
        .setTint(tint)
        .setBlendMode(Phaser.BlendModes.ADD)
        .setScale((cfg.size * 2.9 * cfg.glow) / 62)
        .setDepth(3);
      const core = this.add.image(p.x, p.y, "core").setTint(tint).setScale(cfg.size / 8).setDepth(4);
      const label = this.add
        .text(p.x, p.y + cfg.size + 12, truncate(data.text, 26), { fontSize: "12px", color: "#cdd9f5", align: "center" })
        .setOrigin(0.5, 0)
        .setAlpha(0.55)
        .setDepth(4);
      label.setTint(tint);
      if (cfg.pulse) {
        this.tweens.add({
          targets: glow,
          scale: glow.scale * 1.18,
          duration: 850 + Math.random() * 500,
          yoyo: true,
          repeat: -1,
          ease: "Sine.inOut",
        });
      } else {
        // Gentle twinkle on the quiet stars so the field feels alive as you pass.
        this.tweens.add({
          targets: core,
          alpha: 0.6,
          duration: 1300 + Math.random() * 1700,
          delay: Math.random() * 1600,
          yoyo: true,
          repeat: -1,
          ease: "Sine.inOut",
        });
      }
      const entry: StarEntry = { id: data.id, data, x: p.x, y: p.y, r: cfg.size, glow, core, label, visited: !!data.last_visited_at };
      if (entry.visited) this.markVisited(entry);
      this.stars.push(entry);
    }
  }

  // Each cluster as a *place*: a soft coloured nebula (halo + core) sized to the
  // neighbourhood's footprint, plus a floating name. This is what turns a scatter
  // of dim dots into constellations you visit. The name + nebula brightness are
  // driven per-frame by the ship's distance (see updateNeighborhoods): a faint
  // beacon from afar that blooms as you arrive.
  private buildNeighborhoods(pos: Record<number, { x: number; y: number }>) {
    const groups = new Map<number, { x: number; y: number; n: number; label: string; pts: { x: number; y: number }[] }>();
    for (const s of this.galaxy.stars) {
      const cid = s.cluster_id;
      const p = pos[s.id];
      if (cid === null || cid === undefined || !p) continue;
      const g = groups.get(cid) ?? { x: 0, y: 0, n: 0, label: s.cluster_label || "", pts: [] };
      g.x += p.x; g.y += p.y; g.n += 1; g.pts.push(p);
      groups.set(cid, g);
    }
    let rSum = 0;
    for (const [cid, g] of groups) {
      const cx = g.x / g.n, cy = g.y / g.n;
      let maxD = 0;
      for (const p of g.pts) maxD = Math.max(maxD, Math.hypot(p.x - cx, p.y - cy));
      const r = Math.max(220, maxD + 120);
      rSum += r;
      const color = clusterColor(cid);
      const cloud = this.buildNebulaCloud(cx, cy, r, color);
      const hex = "#" + color.toString(16).padStart(6, "0");
      const name = this.add
        .text(cx, cy - r - 30, (g.label || "").toUpperCase(), { fontFamily: "serif", fontStyle: "italic", fontSize: "32px", color: hex })
        .setOrigin(0.5)
        .setAlpha(0.16)
        .setDepth(2);
      this.neighborhoods.push({ cid, cx, cy, r, n: g.n, color, cloud, name, stars: [] });
    }
    this.neighborhoodR = this.neighborhoods.length ? rSum / this.neighborhoods.length : 400;
  }

  // An irregular, wispy nebula built from a scatter of soft additive "puffs" — a
  // glowing cloud (think Crab Nebula), not a flat disc. A wide faint halo carries
  // it as a beacon across the void; a hot lightened heart; then scattered wisps,
  // some lifted toward white, a few breathing slowly. Returns the puffs so the
  // whole cloud can brighten on approach (each stores its base alpha in 'a0').
  private buildNebulaCloud(cx: number, cy: number, r: number, color: number): Phaser.GameObjects.Image[] {
    const cloud: Phaser.GameObjects.Image[] = [];
    const puff = (x: number, y: number, scale: number, alpha: number, tint: number) => {
      const img = this.add.image(x, y, "puff").setTint(tint).setBlendMode(Phaser.BlendModes.ADD).setDepth(1).setScale(scale).setAlpha(alpha);
      img.setData("a0", alpha);
      cloud.push(img);
      return img;
    };
    puff(cx, cy, (r * 2.6) / 80, 0.05, color); // wide beacon halo
    puff(cx, cy, (r * 0.9) / 80, 0.1, lighten(color, 0.45)); // glowing heart
    const PUFFS = 9;
    for (let i = 0; i < PUFFS; i++) {
      const ang = Math.random() * Math.PI * 2;
      const rad = Math.pow(Math.random(), 0.65) * r * 0.85; // bias toward the centre
      const px = cx + Math.cos(ang) * rad, py = cy + Math.sin(ang) * rad;
      const scale = (r * (0.5 + Math.random() * 0.7)) / 80;
      const bright = Math.random() < 0.33;
      const img = puff(px, py, scale, bright ? 0.085 : 0.05, bright ? lighten(color, 0.35) : color);
      if (i % 4 === 0) {
        // a few wisps breathe (scale only — alpha is owned by the proximity code)
        this.tweens.add({ targets: img, scale: img.scale * 1.18, duration: 4200 + Math.random() * 2600, yoyo: true, repeat: -1, ease: "Sine.inOut", delay: Math.random() * 2000 });
      }
    }
    return cloud;
  }

  // Start the journey *inside* the richest neighbourhood, not the empty centre —
  // so you open among stars with a nebula around you, not staring into the void.
  private pickSpawn(): { x: number; y: number } {
    if (!this.neighborhoods.length) return { x: this.world.w / 2, y: this.world.h / 2 };
    let best = this.neighborhoods[0];
    for (const n of this.neighborhoods) if (n.n > best.n) best = n;
    return { x: best.cx, y: best.cy };
  }

  // Link each star to its neighbourhood so a cluster's stars brighten together.
  private attachStarsToNeighborhoods() {
    const byCid = new Map<number, { stars: StarEntry[] }>();
    for (const nb of this.neighborhoods) byCid.set(nb.cid, nb);
    for (const s of this.stars) {
      const cid = s.data.cluster_id;
      if (cid === null || cid === undefined) continue;
      byCid.get(cid)?.stars.push(s);
    }
  }

  // Proximity drives the whole sense of place: distant neighbourhoods sit dim
  // (faint beacon), and as the ship nears, the nebula cloud glows up AND the
  // cluster's own stars swell and shine — so arriving somewhere feels like the
  // place lighting up around you. (We touch glow.alpha + core.scale, which no
  // tween owns; the twinkle/pulse tweens animate the other properties.)
  private updateNeighborhoods() {
    const ship = this.ship;
    for (const nb of this.neighborhoods) {
      const d = Phaser.Math.Distance.Between(ship.x, ship.y, nb.cx, nb.cy);
      const t = Phaser.Math.Clamp((d - nb.r) / (nb.r * 3), 0, 1); // 0 inside … 1 far
      nb.name.setAlpha(0.62 - 0.46 * t);
      const near = Phaser.Math.Clamp(1 - d / (nb.r * 1.8), 0, 1); // 0 far … 1 right on top
      for (const img of nb.cloud) img.setAlpha((img.getData("a0") as number) * (1 + near * 1.1));
      for (const s of nb.stars) {
        s.glow.setAlpha(0.78 + near * 0.22);
        s.core.setScale((s.r / 8) * (1 + near * 0.55));
      }
    }
  }

  private markVisited(entry: StarEntry) {
    const g = this.add.graphics().setDepth(3);
    g.lineStyle(1.5, 0x8be0ff, 0.5);
    g.strokeCircle(entry.x, entry.y, entry.r + 10);
  }

  private buildShip(x: number, y: number) {
    this.ship = this.physics.add.image(x, y, "ship").setDepth(8);
    this.ship.body.setDamping(false);
    this.ship.body.setDrag(CONFIG.SHIP.drag, CONFIG.SHIP.drag);
    this.ship.body.setMaxVelocity(CONFIG.SHIP.maxVel);
    this.ship.body.setAllowGravity(false);
    // You cannot fly off into the abyss and lose yourself — the edge of the mind
    // is a hard wall (with the camera bounded to the same world).
    this.ship.body.setCollideWorldBounds(true);
    this.flame = this.add
      .particles(0, 0, "core", {
        lifespan: 320,
        speed: { min: 20, max: 70 },
        scale: { start: 0.5, end: 0 },
        alpha: { start: 0.85, end: 0 },
        tint: 0x9bd9ff,
        blendMode: "ADD",
        emitting: false,
      })
      .setDepth(7);
    this.cameras.main.startFollow(this.ship, true, 0.09, 0.09);
    // Explorer's view: frame roughly ONE neighbourhood around the ship so you fly
    // among the stars (the whole-map overview lives in the fixed minimap). Derived
    // from the neighbourhood size so the framing is consistent at any galaxy scale.
    const vmin = Math.min(this.scale.width, this.scale.height);
    this.cameras.main.setZoom(Phaser.Math.Clamp(vmin / (this.neighborhoodR * 2.7), 0.8, 1.7));
  }

  private buildHUD() {
    const counts: Record<string, number> = { proto: 0, ignited: 0, radiant: 0, supernova: 0 };
    for (const s of this.galaxy.stars) if (counts[s.star_stage] !== undefined) counts[s.star_stage]++;
    this.add
      .text(
        16,
        16,
        `✦ ${this.galaxy.stars.length} stars\nproto ${counts.proto} · ignited ${counts.ignited} · radiant ${counts.radiant} · supernova ${counts.supernova}`,
        { fontSize: "13px", color: "#94a3b8", lineSpacing: 4 },
      )
      .setScrollFactor(0)
      .setDepth(20);
  }

  private buildInput() {
    this.cursors = this.input.keyboard!.createCursorKeys();
    this.keys = this.input.keyboard!.addKeys("W,A,S,D,E,ESC");
  }

  private wirePanel() {
    const panel = this.q("#cg-panel");
    const close = () => this.closePanel();
    const c1 = this.q("#cg-p-close");
    const c2 = this.q("#cg-p-close2");
    if (c1) (c1 as HTMLButtonElement).onclick = close;
    if (c2) (c2 as HTMLButtonElement).onclick = close;
    if (panel) panel.addEventListener("click", (ev) => { if (ev.target === panel) close(); });
  }

  private buildTouch() {
    this.landBtn = this.q("#cg-land-btn");
    if (this.landBtn) (this.landBtn as HTMLButtonElement).onclick = () => { if (this.candidate && !this.paused) this.openPanel(this.candidate); };

    this.input.on("pointerdown", (p: Phaser.Input.Pointer) => {
      if (this.paused) return;
      this.touch.active = true;
      this.touch.anchorX = p.x; this.touch.anchorY = p.y;
      this.touch.curX = p.x; this.touch.curY = p.y;
      this.touch.downTime = this.time.now;
      this.touch.moved = 0;
      this.autopilot = null;
    });
    this.input.on("pointermove", (p: Phaser.Input.Pointer) => {
      if (!this.touch.active) return;
      this.touch.curX = p.x; this.touch.curY = p.y;
      this.touch.moved = Math.max(this.touch.moved, Phaser.Math.Distance.Between(this.touch.anchorX, this.touch.anchorY, p.x, p.y));
    });
    this.input.on("pointerup", (p: Phaser.Input.Pointer) => {
      const tap = this.touch.active && this.touch.moved < 12 && this.time.now - this.touch.downTime < 320;
      this.touch.active = false;
      if (this.paused || !tap) return;
      this.handleTap(p);
    });
  }

  private steerToward(target: number, dt: number) {
    this.ship.rotation = Phaser.Math.Angle.RotateTo(this.ship.rotation, target, CONFIG.SHIP.turn * dt);
  }

  private handleTap(p: Phaser.Input.Pointer) {
    let near: StarEntry | null = null, nd = 140;
    for (const s of this.stars) {
      const d = Phaser.Math.Distance.Between(p.worldX, p.worldY, s.x, s.y);
      if (d < nd) { nd = d; near = s; }
    }
    if (near) {
      const ds = Phaser.Math.Distance.Between(this.ship.x, this.ship.y, near.x, near.y);
      if (ds < CONFIG.DOCK_RADIUS) this.openPanel(near);
      else this.autopilot = { x: near.x, y: near.y, star: near };
    } else {
      this.autopilot = { x: p.worldX, y: p.worldY, star: null };
    }
  }

  // ── landing panel ──
  openPanel(entry: StarEntry) {
    const d = entry.data;
    const cfg = STAGE[d.star_stage] || STAGE_FALLBACK;
    const hex = "#" + cfg.color.toString(16).padStart(6, "0");
    const set = (sel: string, txt: string) => { const el = this.q(sel); if (el) el.textContent = txt; };
    const badge = this.q("#cg-p-badge");
    if (badge) {
      badge.textContent = d.star_stage;
      badge.style.color = hex;
      badge.style.background = hex + "22";
      badge.style.border = "1px solid " + hex + "55";
    }
    set("#cg-p-cluster", d.cluster_label || "unclustered");
    set("#cg-p-text", d.text);
    const bits: string[] = [];
    if (d.source_type) bits.push("from " + d.source_type);
    if (d.connection_count) bits.push(d.connection_count + " connections");
    if (d.journal_count) bits.push(d.journal_count + " journal entries");
    set("#cg-p-meta", bits.join("  ·  "));
    const tags = this.q("#cg-p-tags");
    if (tags) {
      tags.innerHTML = "";
      (d.tags || []).forEach((t) => { const el = document.createElement("span"); el.className = "cg-tag"; el.textContent = t; tags.appendChild(el); });
    }
    const note = this.q("#cg-p-note");
    if (note) { if (d.galaxy_note) { note.style.display = "block"; note.textContent = "📌 " + d.galaxy_note; } else note.style.display = "none"; }
    set("#cg-p-copilot", `You flew all the way out here for this one — "${truncate(d.text, 80)}". Before anything else: how would you put it in your own words right now?`);
    if (this.q("#cg-panel")) this.q("#cg-panel")!.classList.add("open");
    this.paused = true;
    this.ship.body.setVelocity(0, 0);
    this.ship.body.setAcceleration(0, 0);
    if (!entry.visited) { entry.visited = true; this.markVisited(entry); }
  }

  closePanel() {
    const p = this.q("#cg-panel");
    if (p) p.classList.remove("open");
    this.paused = false;
  }

  // ── encounters ──
  private buildEncounters(pos: Record<number, { x: number; y: number }>) {
    const centroids: Record<number, { x: number; y: number; n: number }> = {};
    for (const s of this.stars) {
      const cid = s.data.cluster_id;
      if (cid === null || cid === undefined) continue;
      const c = centroids[cid] || { x: 0, y: 0, n: 0 };
      c.x += s.x; c.y += s.y; c.n += 1;
      centroids[cid] = c;
    }
    const gaps = detectGaps(this.galaxy.stars) || [];
    for (const gap of gaps) {
      if (this.encounters.length >= 2) break;
      const cid = gap.clusterId;
      if (cid === null || cid === undefined || !centroids[cid]) continue;
      const c = centroids[cid];
      this.encounters.push({ gap, cx: c.x / c.n, cy: c.y / c.n, resolved: false, cooldownUntil: 0, nega: null, armed: false });
    }
  }

  private startEncounter(enc: Encounter) {
    this.paused = true;
    this.encounterActive = enc;
    this.ship.body.setVelocity(0, 0);
    this.ship.body.setAcceleration(0, 0);
    this.autopilot = null;

    // stand the shadow off to one side so the two ships frame side-by-side
    const sx = this.ship.x, sy = this.ship.y;
    const side = enc.cx >= sx ? 1 : -1;
    const nx = sx + side * 200;
    const ny = sy;

    const nega: any = this.add.image(nx, ny, "nega").setDepth(9).setScale(0.1).setAlpha(0);
    nega.rotation = Phaser.Math.Angle.Between(nx, ny, sx, sy);
    nega.encBaseX = nx; nega.encBaseY = ny; nega.encBaseRot = nega.rotation;
    enc.nega = nega;
    this.tweens.add({ targets: nega, scale: 1.5, alpha: 1, duration: 360, ease: "Back.Out" });
    const halo = this.add.image(nx, ny, "glow").setTint(0xff2d6e).setBlendMode(Phaser.BlendModes.ADD).setScale(0.9).setAlpha(0).setDepth(8);
    this.tweens.add({ targets: halo, alpha: 0.5, scale: 1.4, duration: 420, ease: "Sine.Out" });
    nega.encHalo = halo;

    // cinematic framing: face the shadow, pull the camera back, hold the duel high
    this.ship.rotation = Phaser.Math.Angle.Between(sx, sy, nx, ny);
    const cam = this.cameras.main;
    cam.stopFollow();
    this.encPrevZoom = cam.zoom;
    const midX = (sx + nx) / 2, midY = (sy + ny) / 2;
    const sep = Phaser.Math.Distance.Between(sx, sy, nx, ny);
    const targetZoom = Phaser.Math.Clamp((this.scale.width * 0.5) / (sep + 90), 0.7, 1.25);
    const yShift = (this.scale.height * 0.28) / targetZoom;
    cam.pan(midX, midY + yShift, 700, "Sine.easeInOut");
    cam.zoomTo(targetZoom, 700, "Sine.easeInOut");

    // populate the sheet
    const name = this.q("#cg-enc-name");
    const taunt = this.q("#cg-enc-taunt");
    const choices = this.q("#cg-enc-choices");
    const outcome = this.q("#cg-enc-outcome");
    const skip = this.q("#cg-enc-skip");
    if (name) name.textContent = "Your shadow";
    if (taunt) taunt.textContent = buildTaunt(enc.gap);
    if (outcome) { outcome.classList.remove("show"); outcome.style.display = "none"; outcome.textContent = ""; }
    if (choices) {
      choices.innerHTML = "";
      const reframes = chooseReframes(this.galaxy.stars, enc.gap, this.galaxy.edges) || [];
      for (const star of reframes) {
        const btn = document.createElement("button");
        btn.className = "cg-enc-choice";
        const t = document.createElement("span");
        t.className = "cg-enc-choice-text";
        t.textContent = star.text;
        btn.appendChild(t);
        if (star.star_stage) {
          const sub = document.createElement("span");
          sub.className = "cg-enc-choice-sub";
          sub.textContent = star.star_stage + (star.galaxy_note ? " · 📌" : "");
          btn.appendChild(sub);
        }
        btn.onclick = () => this.resolveReframe(enc, star);
        choices.appendChild(btn);
      }
    }
    if (skip) (skip as HTMLButtonElement).onclick = () => this.escapeEncounter(enc);
    if (this.encEl) this.encEl.classList.add("open");
  }

  private resolveReframe(enc: Encounter, star: { text: string; galaxy_note: string }) {
    enc.resolved = true;
    const outcome = this.q("#cg-enc-outcome");
    if (outcome) {
      let line = `You fire back: "${star.text}."`;
      if (star.galaxy_note) line += ` ${star.galaxy_note}`;
      line += " The shadow has no answer for that — and steps into the light.";
      outcome.textContent = line;
      outcome.style.display = "block";
      outcome.classList.add("show");
    }
    const choices = this.q("#cg-enc-choices");
    if (choices) choices.querySelectorAll("button").forEach((b) => ((b as HTMLButtonElement).disabled = true));
    const skip = this.q("#cg-enc-skip");
    if (skip) (skip as HTMLButtonElement).disabled = true;

    const nega = enc.nega;
    if (nega) {
      const beam = this.add.graphics().setDepth(10);
      beam.lineStyle(3, BEAM, 0.95);
      beam.lineBetween(this.ship.x, this.ship.y, nega.x, nega.y);
      this.tweens.add({ targets: beam, alpha: 0, duration: 150, onComplete: () => beam.destroy() });
    }
    this.time.delayedCall(150, () => {
      if (nega && nega.scene) {
        const flash = this.add.image(nega.x, nega.y, "glow").setTint(0xffffff).setBlendMode(Phaser.BlendModes.ADD).setScale(0.4).setAlpha(0.9).setDepth(11);
        this.tweens.add({ targets: flash, scale: 2.2, alpha: 0, duration: 420, ease: "Sine.Out", onComplete: () => flash.destroy() });
        this.tweens.add({ targets: nega, x: this.ship.x, y: this.ship.y, scale: 0, alpha: 0, duration: 520, ease: "Quad.In" });
        if (nega.encHalo) this.tweens.add({ targets: nega.encHalo, scale: 0, alpha: 0, duration: 520, ease: "Quad.In" });
      }
      this.brightenCluster(enc.gap.clusterId);
    });
    this.time.delayedCall(1200, () => {
      if (this.encEl) this.encEl.classList.remove("open");
      const sk = this.q("#cg-enc-skip");
      if (sk) (sk as HTMLButtonElement).disabled = false;
      this.clearNega(enc);
      this.restoreCamera();
      this.encounterActive = null;
      this.paused = false;
    });
  }

  private escapeEncounter(enc: Encounter) {
    const nega = enc.nega;
    if (nega) {
      this.tweens.add({ targets: nega, alpha: 0, scale: 0.4, duration: 300, ease: "Sine.In" });
      if (nega.encHalo) this.tweens.add({ targets: nega.encHalo, alpha: 0, duration: 300 });
    }
    enc.cooldownUntil = this.time.now + 30000;
    if (this.encEl) this.encEl.classList.remove("open");
    this.time.delayedCall(320, () => this.clearNega(enc));
    this.restoreCamera();
    this.encounterActive = null;
    this.paused = false;
  }

  private wobbleNega(time: number) {
    const enc = this.encounterActive;
    if (!enc || enc.resolved || !enc.nega || !enc.nega.scene) return;
    const n = enc.nega;
    if (n.encBaseX === undefined) return;
    n.x = n.encBaseX + Math.sin(time / 240) * 4;
    n.y = n.encBaseY + Math.cos(time / 300) * 4;
    n.rotation = n.encBaseRot + Math.sin(time / 360) * 0.05;
    if (n.encHalo) { n.encHalo.x = n.x; n.encHalo.y = n.y; }
  }

  private brightenCluster(clusterId: number | null) {
    if (clusterId === null || clusterId === undefined) return;
    for (const s of this.stars) {
      if (s.data.cluster_id !== clusterId) continue;
      this.tweens.add({ targets: s.glow, scale: s.glow.scale * 1.35, alpha: 1, duration: 600, yoyo: true, ease: "Sine.inOut" });
      this.tweens.add({ targets: s.core, alpha: 1, duration: 600 });
      this.tweens.add({ targets: s.label, alpha: 0.95, duration: 600 });
    }
  }

  private clearNega(enc: Encounter) {
    if (enc.nega) {
      if (enc.nega.encHalo) { enc.nega.encHalo.destroy(); enc.nega.encHalo = null; }
      enc.nega.destroy();
      enc.nega = null;
    }
  }

  private restoreCamera() {
    const cam = this.cameras.main;
    cam.zoomTo(this.encPrevZoom || 1, 520, "Sine.easeInOut");
    cam.startFollow(this.ship, true, 0.08, 0.08);
  }

  // Fixed bottom-right minimap: the whole world outline + cluster-coloured star
  // dots, with a live marker for the ship and a box for the current view, so you
  // can see how big the map is and never lose yourself. Sized off the viewport's
  // short edge (clamped) so it stays a glanceable corner inset on phones instead
  // of eating the bottom-right quadrant.
  private miniTargetMax(): number {
    const vmin = Math.min(this.scale.width, this.scale.height);
    return Phaser.Math.Clamp(Math.round(vmin * 0.22), 84, 168);
  }

  private buildMinimap() {
    if (this.miniContainer) {
      this.miniContainer.destroy();
      this.miniContainer = undefined;
    }
    const MAX = this.miniTargetMax();
    this.miniMaxCur = MAX;
    this.miniPad = MAX < 120 ? 6 : 8;
    const pad = this.miniPad;
    const scale = Math.min(MAX / this.world.w, MAX / this.world.h);
    this.miniScale = scale;
    this.miniW = this.world.w * scale;
    this.miniH = this.world.h * scale;

    const cont = this.add.container(0, 0).setScrollFactor(0).setDepth(19);
    cont.add(
      this.add
        .rectangle(0, 0, this.miniW + pad * 2, this.miniH + pad * 2, 0x0b0f13, 0.66)
        .setOrigin(0)
        .setStrokeStyle(1, 0x4a5570, 0.5),
    );

    const dots = this.add.graphics();
    const dotR = MAX < 120 ? 1.1 : 1.4;
    for (const s of this.stars) {
      dots.fillStyle(clusterColor(s.data.cluster_id), 0.85);
      dots.fillCircle(pad + s.x * scale, pad + s.y * scale, dotR);
    }
    cont.add(dots);

    this.miniView = this.add.graphics();
    cont.add(this.miniView);
    this.miniMarker = this.add.graphics();
    cont.add(this.miniMarker);

    this.miniContainer = cont;
    this.positionMinimap();
    if (!this.miniResizeBound) {
      this.scale.on("resize", this.onMinimapResize, this);
      this.miniResizeBound = true;
    }
  }

  // Re-fit the map when the viewport changes enough to matter (e.g. a portrait↔
  // landscape rotation); otherwise just re-pin it to the corner.
  private onMinimapResize() {
    if (Math.abs(this.miniTargetMax() - this.miniMaxCur) >= 4) {
      this.buildMinimap();
    } else {
      this.positionMinimap();
    }
  }

  private positionMinimap() {
    if (!this.miniContainer) return;
    const m = 16;
    this.miniContainer.setPosition(
      this.scale.width - (this.miniW + this.miniPad * 2) - m,
      this.scale.height - (this.miniH + this.miniPad * 2) - m,
    );
  }

  private updateMinimap() {
    if (!this.miniMarker || !this.miniView) return;
    const pad = this.miniPad;
    const s = this.miniScale;
    const mx = pad + this.ship.x * s;
    const my = pad + this.ship.y * s;
    this.miniMarker.clear();
    this.miniMarker.fillStyle(0xffffff, 1);
    this.miniMarker.fillCircle(mx, my, 2.6);
    this.miniMarker.lineStyle(1, 0x9bd9ff, 0.95);
    this.miniMarker.strokeCircle(mx, my, 4.6);

    const v = this.cameras.main.worldView;
    this.miniView.clear();
    this.miniView.lineStyle(1, 0xffffff, 0.22);
    this.miniView.strokeRect(pad + v.x * s, pad + v.y * s, v.width * s, v.height * s);
  }

  update(time: number, delta: number) {
    const dt = delta / 1000;
    const ship = this.ship;
    if (this.paused) {
      this.ring.clear();
      this.prompt.setVisible(false);
      if (this.landBtn) this.landBtn.classList.remove("show");
      this.wobbleNega(time);
      return;
    }

    if (this.encounterActive === null && this.encounters.length) {
      for (const enc of this.encounters) {
        if (enc.resolved || time <= enc.cooldownUntil) continue;
        const d = Phaser.Math.Distance.Between(ship.x, ship.y, enc.cx, enc.cy);
        if (!enc.armed) {
          if (d > CONFIG.TRIGGER_RADIUS * 1.4) enc.armed = true; // left its orbit → now it can surprise you
          continue;
        }
        if (d < CONFIG.TRIGGER_RADIUS) {
          this.startEncounter(enc);
          this.ring.clear();
          this.prompt.setVisible(false);
          if (this.landBtn) this.landBtn.classList.remove("show");
          return;
        }
      }
    }

    let thrusting = false;
    const left = this.cursors.left.isDown || this.keys.A.isDown;
    const right = this.cursors.right.isDown || this.keys.D.isDown;
    const keyThrust = this.cursors.up.isDown || this.keys.W.isDown;

    if (left || right || keyThrust) {
      this.autopilot = null;
      if (left) ship.rotation -= CONFIG.SHIP.turn * dt;
      if (right) ship.rotation += CONFIG.SHIP.turn * dt;
      thrusting = keyThrust;
    } else if (this.touch.active && this.touch.moved > 12) {
      this.steerToward(Phaser.Math.Angle.Between(this.touch.anchorX, this.touch.anchorY, this.touch.curX, this.touch.curY), dt);
      thrusting = true;
    } else if (this.autopilot) {
      const t = this.autopilot;
      const ang = Phaser.Math.Angle.Between(ship.x, ship.y, t.x, t.y);
      const dist = Phaser.Math.Distance.Between(ship.x, ship.y, t.x, t.y);
      this.steerToward(ang, dt);
      thrusting = dist > 50;
      if (t.star && dist < t.star.r + 34) {
        const arrived = t.star;
        this.autopilot = null;
        ship.body.velocity.scale(0.25);
        this.openPanel(arrived);
      } else if (dist < 34) {
        this.autopilot = null;
      }
    }

    if (thrusting) {
      this.physics.velocityFromRotation(ship.rotation, CONFIG.SHIP.accel, ship.body.acceleration);
      this.flame.emitParticleAt(ship.x - Math.cos(ship.rotation) * 16, ship.y - Math.sin(ship.rotation) * 16, 2);
    } else {
      ship.body.setAcceleration(0, 0);
    }

    this.updateNeighborhoods();

    let best: StarEntry | null = null, bestD = CONFIG.DOCK_RADIUS;
    for (const s of this.stars) {
      const d = Phaser.Math.Distance.Between(ship.x, ship.y, s.x, s.y);
      if (d < bestD) { bestD = d; best = s; }
    }
    this.candidate = best;
    this.ring.clear();
    if (best) {
      this.ring.lineStyle(2, 0xffffff, 0.85);
      this.ring.strokeCircle(best.x, best.y, best.r + 16);
      this.prompt.setPosition(best.x, best.y - best.r - 22).setVisible(true);
      if (this.landBtn) this.landBtn.classList.add("show");
      if (Phaser.Input.Keyboard.JustDown(this.keys.E)) this.openPanel(best);
    } else {
      this.prompt.setVisible(false);
      if (this.landBtn) this.landBtn.classList.remove("show");
    }
    this.updateMinimap();
    if (Phaser.Input.Keyboard.JustDown(this.keys.ESC)) this.closePanel();
  }
}

export function mountGalaxyGame(canvasParent: HTMLElement, overlayRoot: HTMLElement, galaxy: GalaxyData): Phaser.Game {
  return new Phaser.Game({
    type: Phaser.AUTO,
    parent: canvasParent,
    backgroundColor: "#0b0f13",
    scale: { mode: Phaser.Scale.RESIZE, width: "100%", height: "100%" },
    physics: { default: "arcade", arcade: { debug: false } },
    scene: new GalaxyScene(galaxy, overlayRoot),
  });
}
