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
  proto: { size: 7, color: 0x5b6b8c, glow: 0.55, pulse: false },
  ignited: { size: 11, color: 0x4ecdc4, glow: 0.95, pulse: false },
  radiant: { size: 16, color: 0x7c6bf0, glow: 1.25, pulse: true },
  supernova: { size: 24, color: 0xf5c878, glow: 1.75, pulse: true },
};
const STAGE_FALLBACK = STAGE.proto;
const SHADOW = 0xff5d7e;
const BEAM = 0x9bd9ff;
const EDGE = 0x6f7fb8;

function clusterColor(cid: number | null): number {
  if (cid === null || cid === undefined) return 0x9fb0c8;
  const h = (cid * 73) % 360;
  return Phaser.Display.Color.HSVToRGB(h / 360, 0.5, 1).color;
}

function hashSeed(n: number): number {
  return ((Math.abs(n) * 2654435761) % 1000) / 1000;
}

/**
 * Cluster-galaxy layout: each constellation is a tight group, and the groups are
 * spread far apart across a large world so flying between them is an actual
 * journey (not all crammed on one screen). Intra-cluster UMAP micro-structure is
 * preserved; lone (uncluster) stars drift in the deep space between groups.
 */
function layoutStars(stars: GalaxyStar[]): {
  pos: Record<number, { x: number; y: number }>;
  world: { w: number; h: number };
} {
  const CELL = 1700; // distance between cluster centres — the void you cross
  const LOCAL_R = 300; // how tight a constellation sits around its centre
  const pos: Record<number, { x: number; y: number }> = {};
  const hasXY = (s: GalaxyStar) =>
    s.x !== null && s.y !== null && isFinite(s.x as number) && isFinite(s.y as number);

  const byCluster = new Map<number, GalaxyStar[]>();
  const lone: GalaxyStar[] = [];
  for (const s of stars) {
    if (s.cluster_id === null || s.cluster_id === undefined) {
      lone.push(s);
    } else {
      const arr = byCluster.get(s.cluster_id) ?? [];
      arr.push(s);
      byCluster.set(s.cluster_id, arr);
    }
  }

  const clusterIds = [...byCluster.keys()];
  const gridN = clusterIds.length || Math.max(1, Math.ceil(lone.length / 8));
  const cols = Math.max(1, Math.ceil(Math.sqrt(gridN)));
  const rows = Math.max(1, Math.ceil(gridN / cols));
  const world = { w: cols * CELL, h: rows * CELL };

  clusterIds.forEach((cid, k) => {
    const members = byCluster.get(cid) as GalaxyStar[];
    const col = k % cols;
    const row = Math.floor(k / cols);
    const cx = (col + 0.5) * CELL + (hashSeed(cid + 1) - 0.5) * CELL * 0.3;
    const cy = (row + 0.5) * CELL + (hashSeed(cid + 7) - 0.5) * CELL * 0.3;

    const withXY = members.filter(hasXY);
    if (withXY.length >= 2) {
      const xs = withXY.map((s) => s.x as number);
      const ys = withXY.map((s) => s.y as number);
      const mx = (Math.min(...xs) + Math.max(...xs)) / 2;
      const my = (Math.min(...ys) + Math.max(...ys)) / 2;
      const span = Math.max(Math.max(...xs) - Math.min(...xs), Math.max(...ys) - Math.min(...ys)) || 1;
      const scale = (LOCAL_R * 2) / span;
      for (const s of members) {
        if (hasXY(s)) {
          pos[s.id] = { x: cx + ((s.x as number) - mx) * scale, y: cy + ((s.y as number) - my) * scale };
        } else {
          const a = hashSeed(s.id) * Math.PI * 2;
          pos[s.id] = { x: cx + Math.cos(a) * LOCAL_R * 0.6, y: cy + Math.sin(a) * LOCAL_R * 0.6 };
        }
      }
    } else {
      members.forEach((s, i) => {
        const a = (i / Math.max(1, members.length)) * Math.PI * 2 + hashSeed(cid) * Math.PI * 2;
        const r = members.length === 1 ? 0 : LOCAL_R * (0.45 + 0.4 * hashSeed(s.id));
        pos[s.id] = { x: cx + Math.cos(a) * r, y: cy + Math.sin(a) * r };
      });
    }
  });

  for (const s of lone) {
    pos[s.id] = { x: hashSeed(s.id * 3 + 1) * world.w, y: hashSeed(s.id * 5 + 2) * world.h };
  }

  return { pos, world };
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

  private ship!: any;
  private flame!: Phaser.GameObjects.Particles.ParticleEmitter;
  private ring!: Phaser.GameObjects.Graphics;
  private prompt!: Phaser.GameObjects.Text;
  private cursors!: Phaser.Types.Input.Keyboard.CursorKeys;
  private keys!: any;
  private landBtn!: HTMLElement | null;
  private encEl!: HTMLElement | null;

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
    this.buildEdges(pos);
    this.buildStars(pos);
    this.buildConstellationLabels();
    this.buildShip(world.w / 2, world.h / 2);
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
    // Declutter: cross-cluster links span the whole view and read as a spiderweb,
    // so draw only links WITHIN a constellation, capped to each star's few
    // strongest, and faint. (Cross-cluster relations still exist in the data and
    // still feed the encounter's reframe relevance — they're just not drawn.)
    const clusterById: Record<number, number | null> = {};
    for (const s of this.galaxy.stars) clusterById[s.id] = s.cluster_id;
    const K = 3;
    const perNode: Record<number, { other: number; sim: number }[]> = {};
    for (const e of this.galaxy.edges) {
      const cs = clusterById[e.source];
      if (cs === null || cs === undefined || cs !== clusterById[e.target]) continue;
      (perNode[e.source] ??= []).push({ other: e.target, sim: e.similarity ?? 0.4 });
      (perNode[e.target] ??= []).push({ other: e.source, sim: e.similarity ?? 0.4 });
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
        g.lineStyle(1, EDGE, 0.08 + sim * 0.14);
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
      }
      const entry: StarEntry = { id: data.id, data, x: p.x, y: p.y, r: cfg.size, glow, core, label, visited: !!data.last_visited_at };
      if (entry.visited) this.markVisited(entry);
      this.stars.push(entry);
    }
  }

  private markVisited(entry: StarEntry) {
    const g = this.add.graphics().setDepth(3);
    g.lineStyle(1.5, 0x8be0ff, 0.5);
    g.strokeCircle(entry.x, entry.y, entry.r + 10);
  }

  // Faint constellation names floating behind each cluster's stars.
  private buildConstellationLabels() {
    const groups: Record<number, { x: number; y: number; n: number; label: string }> = {};
    for (const s of this.stars) {
      const cid = s.data.cluster_id;
      if (cid === null || cid === undefined || !s.data.cluster_label) continue;
      const g = groups[cid] ?? { x: 0, y: 0, n: 0, label: s.data.cluster_label };
      g.x += s.x;
      g.y += s.y;
      g.n += 1;
      groups[cid] = g;
    }
    for (const key of Object.keys(groups)) {
      const g = groups[Number(key)];
      const hex = "#" + clusterColor(Number(key)).toString(16).padStart(6, "0");
      this.add
        .text(g.x / g.n, g.y / g.n, g.label.toUpperCase(), { fontFamily: "serif", fontSize: "26px", color: hex })
        .setOrigin(0.5)
        .setAlpha(0.14)
        .setDepth(1);
    }
  }

  private buildShip(x: number, y: number) {
    this.ship = this.physics.add.image(x, y, "ship").setDepth(8);
    this.ship.body.setDamping(false);
    this.ship.body.setDrag(CONFIG.SHIP.drag, CONFIG.SHIP.drag);
    this.ship.body.setMaxVelocity(CONFIG.SHIP.maxVel);
    this.ship.body.setAllowGravity(false);
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
    this.cameras.main.startFollow(this.ship, true, 0.08, 0.08);
    this.cameras.main.setZoom(1);
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
      this.encounters.push({ gap, cx: c.x / c.n, cy: c.y / c.n, resolved: false, cooldownUntil: 0, nega: null });
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
  // can see how big the map is and never lose yourself.
  private buildMinimap() {
    const MAX = 170;
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
    for (const s of this.stars) {
      dots.fillStyle(clusterColor(s.data.cluster_id), 0.85);
      dots.fillCircle(pad + s.x * scale, pad + s.y * scale, 1.4);
    }
    cont.add(dots);

    this.miniView = this.add.graphics();
    cont.add(this.miniView);
    this.miniMarker = this.add.graphics();
    cont.add(this.miniMarker);

    this.miniContainer = cont;
    this.positionMinimap();
    this.scale.on("resize", this.positionMinimap, this);
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
        if (Phaser.Math.Distance.Between(ship.x, ship.y, enc.cx, enc.cy) < CONFIG.TRIGGER_RADIUS) {
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
