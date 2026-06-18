/**
 * Constellation game — Phaser scene. Ported from the standalone prototype,
 * re-skinned to the product design system and driven by injected GalaxyData
 * (no fetch / no mock). The DOM overlay (landing panel + nega-self sheet) is
 * rendered by the React host and queried within its subtree — no global ids.
 */
import Phaser from "phaser";

import {
  type CopilotPoint,
  createStarNote,
  fetchStarNotes,
  reflectGalaxy,
  type StarNote,
  tutorEnd,
  tutorMessage,
  tutorStart,
} from "@/lib/api";

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
  // Flight feel — all visual-only (SHIP above is the physics): how far the camera
  // leads the velocity, how much the zoom breathes out at full speed, how hard the
  // sprite banks into a turn, and the S/↓ brake strength.
  FEEL: { look: 0.22, zoomOut: 0.11, bank: 0.24, brake: 3.2 },
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

  // ── Co-pilot: the spatially-aware assistant line (Phases 1–3) ──
  private recentStarIds: number[] = []; // flight path, newest first, capped
  private reflectToken = 0; // race guard: drop a resolved line if you've moved on
  private openStarId: number | null = null; // which star the panel is showing
  private copilotPoint: CopilotPoint | null = null; // where the co-pilot is gesturing
  private pointBtn!: HTMLElement | null;
  private toastEl!: HTMLElement | null;
  private toastTimer = 0;
  // Phase 3 — waypoint from ship → a star the co-pilot named
  private waypointStar: StarEntry | null = null;
  private waypointGfx?: Phaser.GameObjects.Graphics;
  private waypointPulse?: Phaser.GameObjects.Image;
  private waypointEdge?: Phaser.GameObjects.Graphics; // screen-space off-screen indicator
  private waypointEdgeText?: Phaser.GameObjects.Text;
  private flightZoom = 1; // the normal follow-the-ship zoom (restored after a reveal)
  private revealTimer?: Phaser.Time.TimerEvent; // the "zoom back" timer from a reveal
  // Phase 2 — ambient dwell detection
  private dwellSince = 0;
  private lastAmbientAt = 0;
  private ambientInFlight = false;
  // Star notes — free-text context the user attaches to a star
  private noteInput!: HTMLElement | null;
  private noteSaveBtn!: HTMLElement | null;
  private notesToken = 0; // race guard for the async notes load
  private savingNote = false; // concurrency guard against double-save
  // Survey / map mode — park the ship, roam the galaxy, pick a course
  private mapMode = false;
  private mapBtn!: HTMLElement | null;
  private mapGfx?: Phaser.GameObjects.Graphics;
  private mapSelected: StarEntry | null = null;
  private mapMinZoom = 0.1;
  private mapDrag = { active: false, moved: 0, lastX: 0, lastY: 0 };
  private mapPinch = { active: false, dist: 0 };
  // Tutoring ("go deeper") — the 5-phase conversation that grows a star
  private tutorStarEntry: StarEntry | null = null;
  private tutorSessionId: string | null = null;
  private tutorBusy = false;
  private tutorInput!: HTMLElement | null;
  private destroyed = false; // set on teardown — guards async resolves touching the scene
  // ── Flight feel (visual-only; none of this touches the physics) ──
  private trail?: Phaser.GameObjects.Particles.ParticleEmitter; // comet tail behind the ship
  private touchGfx?: Phaser.GameObjects.Graphics; // the on-screen thumbstick (screen space)
  private touchMag = 1; // 0..1 throttle from the stick's throw
  private camLook = { x: 0, y: 0 }; // smoothed velocity lookahead for the camera
  private zoomFxUntil = 0; // while a deliberate zoom tween runs, the speed zoom stands down
  private prevRot = 0; // last-frame heading, for banking
  private docking = false; // mid touchdown-glide (input stays paused)
  private meteorTimer?: Phaser.Time.TimerEvent;
  private rmQuery: MediaQueryList | null = null; // cached prefers-reduced-motion (read per frame)
  // ── Visual quality + screen-space overlays (Phase 3) ──
  private lowQuality = false; // weak-device heuristic; gates the per-object ship glow
  private shipGlow: any; // the ship's lit-from-within engine glow (a Phaser Glow filter controller)
  private vignette?: Phaser.GameObjects.Image; // depth-layered edge darkening (NOT a camera filter → HUD stays crisp)
  private spill?: Phaser.GameObjects.Image; // ambient cluster-colour wash around the ship

  constructor(galaxy: GalaxyData, overlayRoot: HTMLElement) {
    super("galaxy");
    this.galaxy = galaxy;
    this.overlayRoot = overlayRoot;
  }

  private q<T extends HTMLElement = HTMLElement>(sel: string): T | null {
    return this.overlayRoot.querySelector<T>(sel);
  }

  create() {
    this.lowQuality = this.detectLowQuality();
    this.makeTextures();
    const { pos, world } = layoutStars(this.galaxy.stars);
    this.world = world;
    this.physics.world.setBounds(0, 0, world.w, world.h);
    this.cameras.main.setBounds(0, 0, world.w, world.h);
    this.cameras.main.setBackgroundColor("#0b0f13");
    this.applyColorGrade();

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
    this.buildOverlays();

    this.ring = this.add.graphics().setDepth(6);
    this.waypointGfx = this.add.graphics().setDepth(5);
    // Off-screen waypoint indicator lives in screen space (scrollFactor 0): an edge
    // arrow + label that always tells you which way (and how far) the target is.
    this.waypointEdge = this.add.graphics().setScrollFactor(0).setDepth(9);
    this.waypointEdgeText = this.add
      .text(0, 0, "", { fontSize: "12px", color: "#cfeaff", fontStyle: "bold", backgroundColor: "rgba(11,15,19,0.72)", padding: { x: 6, y: 3 } })
      .setOrigin(0.5)
      .setScrollFactor(0)
      .setDepth(9)
      .setVisible(false);
    this.mapGfx = this.add.graphics().setDepth(6); // survey-mode overlays (ship ring + selection)
    this.touchGfx = this.add.graphics().setScrollFactor(0).setDepth(15); // touch thumbstick (drawn per-frame)
    this.scheduleMeteor();
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

    // Lifecycle cleanup: Phaser doesn't auto-kill our repeating waypoint tween or
    // the DOM toast timer on teardown — clear both so nothing fires/loops on a
    // destroyed scene after the React host unmounts the game.
    this.events.once(Phaser.Scenes.Events.SHUTDOWN, this.teardownCopilot, this);
    this.events.once(Phaser.Scenes.Events.DESTROY, this.teardownCopilot, this);
  }

  private teardownCopilot() {
    this.destroyed = true;
    if (this.toastTimer) { window.clearTimeout(this.toastTimer); this.toastTimer = 0; }
    this.meteorTimer?.remove();
    this.meteorTimer = undefined;
    this.clearWaypoint();
    // If we tore down while the note textarea was focused (keyboard disabled),
    // restore it so a remount never starts with flight controls dead.
    this.setGameKeyboard(true);
    // Reset shared DOM (the React host reuses #cg-help etc. when the game is
    // rebuilt on a galaxy change) so a teardown mid-map-mode doesn't leave the
    // help bar hidden / the map button stuck on "Exit".
    this.mapMode = false;
    this.setMapUI(false);
    // Close the tutoring overlay if the game is rebuilt mid-session (shared DOM).
    this.tutorStarEntry = null;
    this.tutorSessionId = null;
    const tutor = this.q("#cg-tutor");
    if (tutor) tutor.classList.remove("open");
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

    // A clean stroked circle for ripples (touchdown / course pings) — graphics
    // strokes can't tween, an image of one can.
    const rg = this.make.graphics({ x: 0, y: 0 }, false);
    rg.lineStyle(4, 0xffffff, 1);
    rg.strokeCircle(40, 40, 36);
    rg.generateTexture("ring", 80, 80);
    rg.destroy();

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

    // Vignette texture: a smooth radial gradient (clear centre → dark edges)
    // painted on a 2D canvas. Used as a depth-layered overlay (buildOverlays),
    // NOT a camera filter, so it darkens the void without dimming the HUD/minimap
    // (which render at a higher depth). Radial gradients aren't drawable with
    // Phaser Graphics, hence the canvas.
    try {
      const VS = 256;
      const vc = document.createElement("canvas");
      vc.width = VS;
      vc.height = VS;
      const vctx = vc.getContext("2d");
      if (vctx) {
        const grad = vctx.createRadialGradient(VS / 2, VS / 2, VS * 0.32, VS / 2, VS / 2, VS * 0.62);
        grad.addColorStop(0, "rgba(0,0,0,0)");
        grad.addColorStop(1, "rgba(0,0,0,0.55)");
        vctx.fillStyle = grad;
        vctx.fillRect(0, 0, VS, VS);
        this.textures.addCanvas("vignette", vc);
      }
    } catch {
      // No 2D canvas (e.g. headless) — buildOverlays guards on texture existence.
    }
  }

  // A single cinematic colour grade over the whole frame — one cheap full-screen
  // GPU pass (already compiled into Phaser, 0 added bytes): richer saturation,
  // gentle filmic contrast, and a subtle cool/teal cast that pulls the flat
  // "#0b0f13 + coloured dots" toward a deliberate deep-space palette. WebGL-only;
  // skipped on the (rare) Canvas fallback. Not animated, so it always applies.
  private applyColorGrade() {
    if (this.game.renderer.type !== Phaser.WEBGL) return;
    const cm = this.cameras.main.filters?.internal.addColorMatrix();
    if (!cm) return;
    cm.colorMatrix.saturate(0.12);
    cm.colorMatrix.contrast(0.06, true);
    // Subtle cool cast: trim red a touch, lift blue a touch (brightness-preserving).
    cm.colorMatrix.multiply(
      [0.97, 0, 0, 0, 0, 0, 1.0, 0, 0, 0, 0, 0, 1.05, 0, 0, 0, 0, 0, 1, 0],
      true,
    );
  }

  // Conservative weak-device heuristic (defaults to high quality — iOS reports no
  // deviceMemory and a healthy core count, so iPhones stay on). Only the clearest
  // low-end signals drop it. Gates the per-object ship glow (and future passes).
  private detectLowQuality(): boolean {
    try {
      const nav = navigator as Navigator & { deviceMemory?: number };
      if (typeof nav.deviceMemory === "number" && nav.deviceMemory > 0 && nav.deviceMemory <= 2) return true;
      if (typeof nav.hardwareConcurrency === "number" && nav.hardwareConcurrency > 0 && nav.hardwareConcurrency <= 2) return true;
      return false;
    } catch {
      return false;
    }
  }

  // Screen-space overlays (scrollFactor 0): an ambient cluster-colour spill that
  // washes the space around the ship, and a vignette that darkens the edges. Both
  // are depth-layered sprites, NOT camera filters — so the HUD/minimap (higher
  // depth) stay crisp. Vignette sits above the world (depth 16) but below the HUD
  // (19–20); the spill sits low (depth 1) so stars/ship draw over it.
  private buildOverlays() {
    this.spill = this.add
      .image(0, 0, "glow")
      .setScrollFactor(0)
      .setBlendMode(Phaser.BlendModes.ADD)
      .setDepth(1)
      .setAlpha(0);
    if (this.textures.exists("vignette")) {
      this.vignette = this.add.image(0, 0, "vignette").setScrollFactor(0).setDepth(16).setAlpha(0.8);
    }
    this.fitOverlays();
  }

  private fitOverlays() {
    const w = this.scale.width;
    const h = this.scale.height;
    if (this.vignette) this.vignette.setPosition(w / 2, h / 2).setDisplaySize(w * 1.04, h * 1.04);
    if (this.spill) {
      const r = Math.max(w, h) * 1.5;
      this.spill.setPosition(w / 2, h / 2).setDisplaySize(r, r);
    }
  }

  // Colour as navigation: the neighbourhood you're closest to bleeds its colour
  // into the ship's engine glow and the ambient spill, so you FEEL which
  // constellation you're in. `near` (0 far … 1 on top) comes from
  // updateNeighborhoods(); both effects fade to nothing out in the void.
  private applySpill(near: number, color: number) {
    if (this.shipGlow) {
      const base = 0x9bd9ff;
      const t = near * 0.7;
      const r = Math.round(((base >> 16) & 255) + (((color >> 16) & 255) - ((base >> 16) & 255)) * t);
      const g = Math.round(((base >> 8) & 255) + (((color >> 8) & 255) - ((base >> 8) & 255)) * t);
      const b = Math.round((base & 255) + ((color & 255) - (base & 255)) * t);
      this.shipGlow.color = (r << 16) | (g << 8) | b;
    }
    if (this.spill) this.spill.setTint(color).setAlpha(near * 0.1);
  }

  private buildStarfield() {
    const W = this.world.w;
    const H = this.world.h;
    const density = Math.min(6, (W * H) / (3600 * 2400)); // keep the void starry as the world grows
    // Star colour temperatures — mostly white-blue, some warm amber, a few hot
    // blue — so the field reads as a real sky instead of uniform noise dots.
    const STAR_HUES = [0xcfe0ff, 0xcfe0ff, 0xcfe0ff, 0xe9f1ff, 0xfff0d8, 0xffe6c4, 0xaecbff];
    const layers = [
      // The deep field barely scrolls — it's what makes the nearer layers read as
      // motion. Three planes of parallax turn "panning a picture" into depth.
      { n: Math.round(320 * density), alpha: 0.32, sf: 0.14, r: 0.9 },
      { n: Math.round(240 * density), alpha: 0.5, sf: 0.35, r: 1.2 },
      { n: Math.round(140 * density), alpha: 0.8, sf: 0.6, r: 1.6 },
    ];
    for (const L of layers) {
      const g = this.add.graphics().setScrollFactor(L.sf).setDepth(0);
      for (let i = 0; i < L.n; i++) {
        g.fillStyle(STAR_HUES[(Math.random() * STAR_HUES.length) | 0], L.alpha * (0.4 + Math.random() * 0.6));
        g.fillCircle(Math.random() * W * 1.4, Math.random() * H * 1.4, L.r * (0.6 + Math.random()));
      }
    }
    this.buildMilkyWay(W, H, density);
  }

  // A faint diagonal galactic band: a tilted river of dim stars concentrated on a
  // spine, plus a few large soft haze puffs along it — structure and a sense of
  // scale instead of an even sprinkle. Drawn once into the deep parallax field.
  private buildMilkyWay(W: number, H: number, density: number) {
    const cx = W / 2;
    const cy = H / 2;
    const ang = -0.5; // band tilt (radians)
    const ca = Math.cos(ang);
    const sa = Math.sin(ang);
    const len = Math.hypot(W, H) * 0.75;
    const halfW = Math.min(W, H) * 0.16;
    const g = this.add.graphics().setScrollFactor(0.1).setDepth(0);
    const N = Math.round(420 * density);
    for (let i = 0; i < N; i++) {
      const t = (Math.random() - 0.5) * len; // along the band
      // Bias toward the spine (sum-of-uniforms ≈ gaussian) so the band has a core.
      const off = ((Math.random() + Math.random() + Math.random()) / 3 - 0.5) * 2 * halfW;
      const x = cx + t * ca - off * sa;
      const y = cy + t * sa + off * ca;
      const fall = 1 - Math.min(1, Math.abs(off) / halfW);
      g.fillStyle(0xdfe9ff, 0.05 + 0.1 * fall * Math.random());
      g.fillCircle(x, y, 0.6 + Math.random() * 0.8);
    }
    // A few large, very faint haze clouds along the spine (reuse the puff texture).
    for (let i = 0; i < 5; i++) {
      const t = (i / 4 - 0.5) * len * 0.85;
      this.add
        .image(cx + t * ca, cy + t * sa, "puff")
        .setScrollFactor(0.1)
        .setDepth(0)
        .setBlendMode(Phaser.BlendModes.ADD)
        .setTint(0x9fb6e8)
        .setScale((halfW * 2.4) / 80)
        .setAlpha(0.045);
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
    let bestNear = 0;
    let bestColor = 0x9bd9ff;
    for (const nb of this.neighborhoods) {
      const d = Phaser.Math.Distance.Between(ship.x, ship.y, nb.cx, nb.cy);
      const t = Phaser.Math.Clamp((d - nb.r) / (nb.r * 3), 0, 1); // 0 inside … 1 far
      nb.name.setAlpha(0.62 - 0.46 * t);
      const near = Phaser.Math.Clamp(1 - d / (nb.r * 1.8), 0, 1); // 0 far … 1 right on top
      if (near > bestNear) {
        bestNear = near;
        bestColor = nb.color;
      }
      for (const img of nb.cloud) img.setAlpha((img.getData("a0") as number) * (1 + near * 1.1));
      for (const s of nb.stars) {
        s.glow.setAlpha(0.78 + near * 0.22);
        s.core.setScale((s.r / 8) * (1 + near * 0.55));
      }
    }
    // Spill the nearest neighbourhood's colour onto the ship + the space around it.
    this.applySpill(bestNear, bestColor);
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
    // Real lit-from-within engine glow — a per-object GPU Glow filter on the ship
    // ALONE (one render target, not the hundreds a per-star glow would need). Its
    // colour drifts toward the neighbourhood you're flying through (see applySpill).
    // WebGL-only and skipped on weak devices.
    if (!this.lowQuality && this.game.renderer.type === Phaser.WEBGL) {
      this.ship.enableFilters?.();
      this.shipGlow = this.ship.filters?.internal.addGlow(0x9bd9ff, 3, 0, 1, false, 6, 12);
    }
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
    // Comet tail — the flight path made visible. Emission follows speed (see
    // flightFeel), so a drift leaves a whisper and a full burn a streak.
    this.trail = this.add
      .particles(0, 0, "glow", {
        lifespan: 520,
        scale: { start: 0.085, end: 0.012 },
        alpha: { start: 0.5, end: 0 },
        tint: [0x7c6bf0, 0x9bd9ff],
        blendMode: "ADD",
        emitting: false,
      })
      .setDepth(7);
    this.prevRot = this.ship.rotation;
    this.cameras.main.startFollow(this.ship, true, 0.09, 0.09);
    // Explorer's view: frame roughly ONE neighbourhood around the ship so you fly
    // among the stars (the whole-map overview lives in the fixed minimap). Derived
    // from the neighbourhood size so the framing is consistent at any galaxy scale.
    const vmin = Math.min(this.scale.width, this.scale.height);
    this.flightZoom = Phaser.Math.Clamp(vmin / (this.neighborhoodR * 2.7), 0.8, 1.7);
    this.cameras.main.setZoom(this.flightZoom);
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
    this.keys = this.input.keyboard!.addKeys("W,A,S,D,E,ESC,M");
    this.input.addPointer(1); // a 2nd touch pointer so map mode can pinch-zoom
  }

  private wirePanel() {
    const panel = this.q("#cg-panel");
    const close = () => this.closePanel();
    const c1 = this.q("#cg-p-close");
    const c2 = this.q("#cg-p-close2");
    if (c1) (c1 as HTMLButtonElement).onclick = close;
    if (c2) (c2 as HTMLButtonElement).onclick = close;
    if (panel) panel.addEventListener("click", (ev) => { if (ev.target === panel) close(); });

    // Co-pilot affordances: the ambient toast + the "show me where" waypoint button.
    this.toastEl = this.q("#cg-copilot-toast");
    this.pointBtn = this.q("#cg-p-point");
    if (this.pointBtn) {
      (this.pointBtn as HTMLButtonElement).onclick = () => {
        const pt = this.copilotPoint;
        this.closePanel();
        if (pt) this.setWaypoint(pt.star_id);
      };
    }

    // Star notes composer.
    this.noteInput = this.q("#cg-p-note-input");
    this.noteSaveBtn = this.q("#cg-p-note-save");
    if (this.noteSaveBtn) (this.noteSaveBtn as HTMLButtonElement).onclick = () => void this.saveStarNote();
    if (this.noteInput) {
      // The game binds W/A/S/D/E/Esc as flight keys. While typing a note those
      // must reach the textarea, not steer the ship — so suspend the game's
      // keyboard on focus (the game is paused anyway) and stop key events from
      // bubbling to Phaser's window listener. Re-enabled on blur / panel close.
      this.noteInput.addEventListener("focus", () => this.setGameKeyboard(false));
      this.noteInput.addEventListener("blur", () => this.setGameKeyboard(true));
      this.noteInput.addEventListener("keydown", (e) => e.stopPropagation());
      this.noteInput.addEventListener("keyup", (e) => e.stopPropagation());
      // Cmd/Ctrl+Enter saves without reaching for the mouse.
      this.noteInput.addEventListener("keydown", (e) => {
        const ke = e as KeyboardEvent;
        if ((ke.metaKey || ke.ctrlKey) && ke.key === "Enter") { e.preventDefault(); void this.saveStarNote(); }
      });
    }

    // Tutoring ("go deeper") — launch + conversation controls.
    const deepen = this.q("#cg-p-deepen");
    if (deepen) {
      (deepen as HTMLButtonElement).onclick = () => {
        const entry = this.stars.find((s) => s.id === this.openStarId);
        if (entry) void this.openTutor(entry);
      };
    }
    this.tutorInput = this.q("#cg-tutor-input");
    const tSend = this.q("#cg-tutor-send");
    if (tSend) (tSend as HTMLButtonElement).onclick = () => void this.tutorSend();
    const tSkip = this.q("#cg-tutor-skip");
    if (tSkip) (tSkip as HTMLButtonElement).onclick = () => void this.tutorSkipPhase();
    const tEnd = this.q("#cg-tutor-end");
    if (tEnd) (tEnd as HTMLButtonElement).onclick = () => void this.closeTutor(true);
    if (this.tutorInput) {
      this.tutorInput.addEventListener("focus", () => this.setGameKeyboard(false));
      this.tutorInput.addEventListener("blur", () => this.setGameKeyboard(true));
      this.tutorInput.addEventListener("keydown", (e) => e.stopPropagation());
      this.tutorInput.addEventListener("keyup", (e) => e.stopPropagation());
      this.tutorInput.addEventListener("keydown", (e) => {
        const ke = e as KeyboardEvent;
        if ((ke.metaKey || ke.ctrlKey) && ke.key === "Enter") { e.preventDefault(); void this.tutorSend(); }
      });
    }
  }

  private setGameKeyboard(enabled: boolean) {
    // Suspend Phaser key PROCESSING while a note is being typed. The textarea's
    // own keydown/keyup handlers stopPropagation (Phaser listens in the bubble
    // phase on window), so the flight keys reach the textarea, not the ship.
    if (this.input.keyboard) this.input.keyboard.enabled = enabled;
  }

  private buildTouch() {
    this.landBtn = this.q("#cg-land-btn");
    if (this.landBtn) (this.landBtn as HTMLButtonElement).onclick = () => { if (this.candidate && !this.paused && !this.mapMode) this.dockAndOpen(this.candidate); };

    // Survey/map-mode controls.
    this.mapBtn = this.q("#cg-map-btn");
    if (this.mapBtn) (this.mapBtn as HTMLButtonElement).onclick = () => { if (this.mapMode) this.exitMapMode(null); else this.enterMapMode(); };
    const fly = this.q("#cg-map-fly");
    if (fly) (fly as HTMLButtonElement).onclick = () => this.exitMapMode(this.mapSelected);
    const cancel = this.q("#cg-map-cancel");
    if (cancel) (cancel as HTMLButtonElement).onclick = () => this.deselectMapStar();
    this.input.on("wheel", (_p: unknown, _o: unknown, _dx: number, dy: number) => {
      if (!this.mapMode) return;
      const cam = this.cameras.main;
      cam.setZoom(Phaser.Math.Clamp(cam.zoom * (dy > 0 ? 0.9 : 1.1), this.mapMinZoom, this.flightZoom));
    });

    this.input.on("pointerdown", (p: Phaser.Input.Pointer) => {
      if (this.mapMode) { this.mapPointerDown(p); return; }
      if (this.paused) return;
      this.touch.active = true;
      this.touch.anchorX = p.x; this.touch.anchorY = p.y;
      this.touch.curX = p.x; this.touch.curY = p.y;
      this.touch.downTime = this.time.now;
      this.touch.moved = 0;
      this.autopilot = null;
    });
    this.input.on("pointermove", (p: Phaser.Input.Pointer) => {
      if (this.mapMode) { this.mapPointerMove(p); return; }
      if (!this.touch.active) return;
      this.touch.curX = p.x; this.touch.curY = p.y;
      this.touch.moved = Math.max(this.touch.moved, Phaser.Math.Distance.Between(this.touch.anchorX, this.touch.anchorY, p.x, p.y));
    });
    this.input.on("pointerup", (p: Phaser.Input.Pointer) => {
      if (this.mapMode) { this.mapPointerUp(p); return; }
      const tap = this.touch.active && this.touch.moved < 12 && this.time.now - this.touch.downTime < 320;
      this.touch.active = false;
      if (this.paused || !tap) return;
      this.handleTap(p);
    });
  }

  // Every deliberate camera zoom (map, reveal, duel) goes through here so the
  // per-frame speed zoom (flightFeel) stands down until the tween — plus any
  // hold the caller needs (e.g. a reveal's dwell) — has finished. Without this
  // the two write cam.zoom on the same frames and the camera judders.
  private tweenZoom(target: number, ms: number, hold = 0) {
    this.zoomFxUntil = this.time.now + ms + hold;
    this.cameras.main.zoomTo(target, ms, "Sine.easeInOut");
  }

  // ── Survey / map mode ──
  // Park the ship and roam: drag to pan, scroll/pinch to zoom, tap a star to set a
  // course. Picking a destination flies you there (the arrive-steering autopilot);
  // exiting returns you to the ship. Decouples "decide where to go" from flying.
  private enterMapMode() {
    if (this.mapMode || this.paused || this.encounterActive) return;
    this.mapMode = true;
    this.autopilot = null;
    this.deselectMapStar();
    this.touch.active = false;
    if (this.touchGfx) this.touchGfx.clear();
    this.ship.body.setVelocity(0, 0);
    this.ship.body.setAcceleration(0, 0);
    const cam = this.cameras.main;
    cam.stopFollow();
    const fit = Math.min(cam.width / this.world.w, cam.height / this.world.h);
    this.mapMinZoom = Math.max(fit * 0.9, 0.05);
    const survey = Phaser.Math.Clamp(this.flightZoom * 0.42, this.mapMinZoom, this.flightZoom);
    const ms = this.reducedMotion() ? 0 : 450;
    cam.pan(this.ship.x, this.ship.y, ms, "Sine.easeInOut");
    this.tweenZoom(survey, ms);
    this.setMapUI(true);
  }

  private exitMapMode(travelTo: StarEntry | null) {
    if (!this.mapMode) return;
    this.mapMode = false;
    this.deselectMapStar();
    if (this.mapGfx) this.mapGfx.clear();
    this.setMapUI(false);
    const cam = this.cameras.main;
    cam.startFollow(this.ship, true, 0.08, 0.08);
    this.tweenZoom(this.flightZoom, this.reducedMotion() ? 0 : 450);
    if (travelTo) {
      this.autopilot = { x: travelTo.x, y: travelTo.y, star: travelTo };
      this.pingTarget(travelTo.x, travelTo.y); // confirm the course visually
    }
  }

  private setMapUI(on: boolean) {
    const btn = this.mapBtn as HTMLButtonElement | null;
    if (btn) { btn.textContent = on ? "✕ Exit map" : "🗺 Map"; btn.classList.toggle("active", on); }
    const hint = this.q("#cg-map-hint");
    if (hint) hint.classList.toggle("show", on);
    const help = this.q("#cg-help");
    if (help) help.style.visibility = on ? "hidden" : "visible";
    if (!on) { const c = this.q("#cg-map-confirm"); if (c) c.classList.remove("show"); }
    if (on && this.landBtn) this.landBtn.classList.remove("show");
  }

  private mapPointerDown(p: Phaser.Input.Pointer) {
    if (this.mapDrag.active) return; // a 2nd finger going down — pinch handles it
    this.mapDrag.active = true;
    this.mapDrag.moved = 0;
    this.mapDrag.lastX = p.x;
    this.mapDrag.lastY = p.y;
  }

  private mapPointerMove(p: Phaser.Input.Pointer) {
    const cam = this.cameras.main;
    const p1 = this.input.pointer1;
    const p2 = this.input.pointer2;
    if (p1?.isDown && p2?.isDown) {
      // Pinch-zoom around the centre.
      const dist = Phaser.Math.Distance.Between(p1.x, p1.y, p2.x, p2.y);
      if (this.mapPinch.active && this.mapPinch.dist > 0) {
        cam.setZoom(Phaser.Math.Clamp(cam.zoom * (dist / this.mapPinch.dist), this.mapMinZoom, this.flightZoom));
      }
      this.mapPinch.active = true;
      this.mapPinch.dist = dist;
      this.mapDrag.moved += 99; // a pinch is never a tap
      return;
    }
    if (this.mapPinch.active) {
      // Just lifted one of two fingers — re-anchor the drag to the survivor so the
      // next pan frame doesn't jump by the stale delta.
      this.mapPinch.active = false;
      this.mapPinch.dist = 0;
      this.mapDrag.lastX = p.x;
      this.mapDrag.lastY = p.y;
      return;
    }
    if (!this.mapDrag.active) return;
    const dx = p.x - this.mapDrag.lastX, dy = p.y - this.mapDrag.lastY;
    this.mapDrag.moved += Math.abs(dx) + Math.abs(dy);
    cam.scrollX -= dx / cam.zoom; // camera is world-bounded, so this clamps itself
    cam.scrollY -= dy / cam.zoom;
    this.mapDrag.lastX = p.x;
    this.mapDrag.lastY = p.y;
  }

  private mapPointerUp(p: Phaser.Input.Pointer) {
    const tap = this.mapDrag.active && this.mapDrag.moved < 14;
    this.mapDrag.active = false;
    this.mapPinch.active = false;
    this.mapPinch.dist = 0;
    if (!tap) return;
    let near: StarEntry | null = null;
    let best = 46 / this.cameras.main.zoom; // ~46px on screen → world units
    for (const s of this.stars) {
      const d = Phaser.Math.Distance.Between(p.worldX, p.worldY, s.x, s.y);
      if (d < best) { best = d; near = s; }
    }
    if (near) this.selectMapStar(near);
    else this.deselectMapStar();
  }

  private selectMapStar(star: StarEntry) {
    this.mapSelected = star;
    const name = this.q("#cg-map-confirm-name");
    if (name) name.textContent = truncate(star.data.text, 44);
    const c = this.q("#cg-map-confirm");
    if (c) c.classList.add("show");
  }

  private deselectMapStar() {
    this.mapSelected = null;
    const c = this.q("#cg-map-confirm");
    if (c) c.classList.remove("show");
  }

  private updateMapMode() {
    // Suppress the in-flight waypoint overlay while surveying (the minimap marker,
    // drawn by updateMinimap below, still shows the target on the map).
    if (this.waypointGfx) this.waypointGfx.clear();
    if (this.waypointEdge) this.waypointEdge.clear();
    if (this.waypointEdgeText) this.waypointEdgeText.setVisible(false);
    const g = this.mapGfx;
    if (g) {
      g.clear();
      // "Your ship" ring so the parked ship is findable when zoomed right out.
      g.lineStyle(2, 0x9bd9ff, 0.75);
      g.strokeCircle(this.ship.x, this.ship.y, 28);
      if (this.mapSelected) {
        g.lineStyle(3, 0x7c6bf0, 0.95);
        g.strokeCircle(this.mapSelected.x, this.mapSelected.y, this.mapSelected.r + 16);
      }
    }
    this.updateMinimap();
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
      if (ds < CONFIG.DOCK_RADIUS) {
        this.dockAndOpen(near);
      } else {
        this.autopilot = { x: near.x, y: near.y, star: near };
        this.pingTarget(near.x, near.y);
      }
    } else {
      this.autopilot = { x: p.worldX, y: p.worldY, star: null };
      this.pingTarget(p.worldX, p.worldY);
    }
  }

  // ── landing: a touch of ceremony ──
  // Glide the last few pixels onto the star's surface, ring out a touchdown
  // ripple, then open the panel — landing on a thought should feel like a
  // landing, not a modal popping. Reduced motion goes straight to the panel.
  private dockAndOpen(entry: StarEntry) {
    if (this.docking || this.paused) return;
    if (this.reducedMotion()) { this.openPanel(entry); return; }
    this.docking = true;
    this.paused = true; // freezes steering/encounters while we glide in
    const ship = this.ship;
    ship.body.setVelocity(0, 0);
    ship.body.setAcceleration(0, 0);
    this.touch.active = false;
    if (this.touchGfx) this.touchGfx.clear();
    this.ring.clear();
    this.prompt.setVisible(false);
    if (this.landBtn) this.landBtn.classList.remove("show");
    // Touchdown point just off the star's surface, along the approach line; face
    // the star the short way round (a raw rotation tween can spin the long way).
    const ang = Phaser.Math.Angle.Between(entry.x, entry.y, ship.x, ship.y);
    const face = ship.rotation + Phaser.Math.Angle.Wrap(ang + Math.PI - ship.rotation);
    this.tweens.add({
      targets: ship,
      x: entry.x + Math.cos(ang) * (entry.r + 26),
      y: entry.y + Math.sin(ang) * (entry.r + 26),
      rotation: face,
      scaleX: 1,
      scaleY: 1,
      duration: 340,
      ease: "Sine.Out",
      onComplete: () => {
        if (this.destroyed) return;
        this.landingRipple(entry);
        this.time.delayedCall(140, () => {
          this.docking = false;
          if (!this.destroyed) this.openPanel(entry);
        });
      },
    });
  }

  // Touchdown ripple in the star's own constellation colour; a first-ever visit
  // also gets a small starburst — discovering a thought should feel rewarded.
  private landingRipple(entry: StarEntry) {
    const tint = clusterColor(entry.data.cluster_id);
    const base = (entry.r + 14) / 36;
    const ring = this.add
      .image(entry.x, entry.y, "ring")
      .setTint(tint)
      .setBlendMode(Phaser.BlendModes.ADD)
      .setScale(base)
      .setAlpha(0.85)
      .setDepth(6);
    this.tweens.add({ targets: ring, scale: base * 2.3, alpha: 0, duration: 620, ease: "Sine.Out", onComplete: () => ring.destroy() });
    if (!entry.visited) {
      const burst = this.add
        .particles(entry.x, entry.y, "core", {
          speed: { min: 50, max: 150 },
          scale: { start: 0.42, end: 0 },
          alpha: { start: 0.9, end: 0 },
          lifespan: 650,
          blendMode: "ADD",
          tint,
          emitting: false,
        })
        .setDepth(6);
      burst.explode(14);
      this.time.delayedCall(800, () => burst.destroy());
    }
  }

  // A quick ring ping where a course was set (tap-to-travel / map "Fly here") so
  // the input always lands somewhere visible.
  private pingTarget(x: number, y: number) {
    if (this.reducedMotion()) return;
    const ring = this.add
      .image(x, y, "ring")
      .setTint(BEAM)
      .setBlendMode(Phaser.BlendModes.ADD)
      .setScale(0.5)
      .setAlpha(0.8)
      .setDepth(6);
    this.tweens.add({ targets: ring, scale: 1.5, alpha: 0, duration: 520, ease: "Sine.Out", onComplete: () => ring.destroy() });
  }

  // The thumbstick the finger is already making: a ring at the touch anchor, a
  // knob at the (clamped) drag point. Pure feedback — input is unchanged — but
  // it makes touch flight legible: you can SEE your heading and throttle.
  private drawTouchStick() {
    const g = this.touchGfx;
    if (!g) return;
    g.clear();
    if (!this.touch.active || this.touch.moved <= 12 || this.paused || this.mapMode) return;
    const ax = this.touch.anchorX;
    const ay = this.touch.anchorY;
    const dx = this.touch.curX - ax;
    const dy = this.touch.curY - ay;
    const d = Math.hypot(dx, dy) || 1;
    const R = 56;
    const kx = ax + (dx / d) * Math.min(d, R);
    const ky = ay + (dy / d) * Math.min(d, R);
    g.fillStyle(0x9bd9ff, 0.07);
    g.fillCircle(ax, ay, R);
    g.lineStyle(1.5, 0x9bd9ff, 0.35);
    g.strokeCircle(ax, ay, R);
    g.lineStyle(2, 0x9bd9ff, 0.5);
    g.lineBetween(ax, ay, kx, ky);
    g.fillStyle(0xcfeaff, 0.85);
    g.fillCircle(kx, ky, 13);
  }

  // ── Flight feel: every frame, all visual-only (the arcade physics is untouched) ──
  private flightFeel(time: number, dt: number) {
    const ship = this.ship;
    const rm = this.reducedMotion();
    const speed = ship.body.velocity.length();
    const speedT = Phaser.Math.Clamp(speed / CONFIG.SHIP.maxVel, 0, 1);

    // Bank into the turn (squash across the hull), stretch a touch with speed.
    // Exponential smoothing so the feel is identical at 60 and 120 Hz.
    const dRot = Phaser.Math.Angle.Wrap(ship.rotation - this.prevRot);
    this.prevRot = ship.rotation;
    const bank = rm ? 0 : Phaser.Math.Clamp(Math.abs(dRot) / (CONFIG.SHIP.turn * Math.max(dt, 0.001)), 0, 1);
    const ease = 1 - Math.exp(-9 * dt);
    ship.scaleY = Phaser.Math.Linear(ship.scaleY, 1 - CONFIG.FEEL.bank * bank, ease);
    ship.scaleX = Phaser.Math.Linear(ship.scaleX, 1 + (rm ? 0 : 0.07 * speedT), ease);

    // Comet tail — emission follows speed.
    if (this.trail && !rm && speed > 36) {
      const back = 14 + speedT * 5;
      this.trail.emitParticleAt(
        ship.x - Math.cos(ship.rotation) * back + (Math.random() - 0.5) * 4,
        ship.y - Math.sin(ship.rotation) * back + (Math.random() - 0.5) * 4,
        speedT > 0.6 ? 2 : 1,
      );
    }

    // Camera: lead the velocity so you can see where you're going, and breathe
    // the zoom out at speed so fast feels fast. The zoom lerp stands down while
    // a deliberate zoom tween owns the camera (see tweenZoom).
    if (rm) return;
    const cam = this.cameras.main;
    const lookEase = 1 - Math.exp(-3.5 * dt);
    this.camLook.x = Phaser.Math.Linear(this.camLook.x, ship.body.velocity.x * CONFIG.FEEL.look, lookEase);
    this.camLook.y = Phaser.Math.Linear(this.camLook.y, ship.body.velocity.y * CONFIG.FEEL.look, lookEase);
    cam.setFollowOffset(-this.camLook.x, -this.camLook.y);
    if (time > this.zoomFxUntil) {
      cam.setZoom(Phaser.Math.Linear(cam.zoom, this.flightZoom * (1 - CONFIG.FEEL.zoomOut * speedT), 1 - Math.exp(-2.2 * dt)));
    }
  }

  // ── Meteors: pure atmosphere ──
  // A faint shooting star every so often, only over live flight (never a panel /
  // duel / the map, where it would distract) and never under reduced motion.
  private scheduleMeteor() {
    this.meteorTimer = this.time.delayedCall(Phaser.Math.Between(7000, 16000), () => {
      if (this.destroyed) return;
      if (!this.reducedMotion() && !this.paused && !this.encounterActive && !this.mapMode) this.spawnMeteor();
      this.scheduleMeteor();
    });
  }

  private spawnMeteor() {
    const view = this.cameras.main.worldView;
    const x = view.x + Math.random() * view.width;
    const y = view.y + Math.random() * view.height * 0.7;
    let ang = Phaser.Math.FloatBetween(0.35, 0.85); // gentle diagonal, falling
    if (Math.random() < 0.5) ang = Math.PI - ang;
    const streak = this.add
      .image(x, y, "glow")
      .setScale(2.6, 0.14)
      .setRotation(ang)
      .setAlpha(0.7)
      .setBlendMode(Phaser.BlendModes.ADD)
      .setTint(0xcfe0ff)
      .setDepth(1);
    const dist = 520 + Math.random() * 360;
    this.tweens.add({
      targets: streak,
      x: x + Math.cos(ang) * dist,
      y: y + Math.sin(ang) * dist,
      alpha: 0,
      duration: 900 + Math.random() * 500,
      ease: "Sine.In",
      onComplete: () => streak.destroy(),
    });
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
    // Provenance: where this lesson came from — the grounding for adding your own notes.
    const ctxEl = this.q("#cg-p-context");
    if (ctxEl) {
      const prov = this.provenanceLine(d);
      if (prov) { ctxEl.textContent = prov; ctxEl.style.display = "block"; }
      else ctxEl.style.display = "none";
    }
    // Notes: reset the composer and load any existing notes for this star.
    this.resetNoteComposer();
    this.loadStarNotes(entry.id, d.journal_count || 0);
    // Optimistic line: a grounded static line shows instantly (and stays as the
    // graceful fallback if the request fails); the co-pilot's real, spatially-
    // aware line swaps in when it resolves.
    // Show the co-pilot "reading" state (twinkling dots); requestCopilotLine swaps
    // in the real line on resolve. No optimistic sentence that mutates as you read.
    this.setCopilotLoading(true);
    this.hidePointAffordance();
    if (this.toastEl) this.toastEl.classList.remove("show");
    if (this.q("#cg-panel")) this.q("#cg-panel")!.classList.add("open");
    this.paused = true;
    this.openStarId = entry.id;
    this.ship.body.setVelocity(0, 0);
    this.ship.body.setAcceleration(0, 0);
    if (!entry.visited) { entry.visited = true; this.markVisited(entry); }
    // Reached the star the co-pilot pointed at → retire the waypoint.
    if (this.waypointStar && this.waypointStar.id === entry.id) this.clearWaypoint();
    // Track the flight path (newest first, deduped, capped) so the co-pilot knows
    // where you came from.
    this.recentStarIds = [entry.id, ...this.recentStarIds.filter((id) => id !== entry.id)].slice(0, 8);
    this.requestCopilotLine(entry);
  }

  // ── Co-pilot line: ask the backend for a spatially-aware line for this star.
  // The backend computes the evidence; the panel only renders the phrasing. A
  // race token guards against the user landing elsewhere before this resolves.
  // Toggle the "reading" loader (twinkling dots) vs the actual line element.
  private setCopilotLoading(on: boolean) {
    const loading = this.q("#cg-p-copilot-loading");
    const line = this.q("#cg-p-copilot");
    if (loading) loading.style.display = on ? "flex" : "none";
    if (line) line.style.display = on ? "none" : "block";
  }

  // Reveal a line in the co-pilot slot with a soft fade-in (restarts the anim).
  private revealCopilotLine(text: string) {
    this.setCopilotLoading(false);
    const line = this.q("#cg-p-copilot");
    if (!line) return;
    line.textContent = text;
    line.classList.remove("cg-line-in");
    void line.offsetWidth; // force reflow so the animation replays each land
    line.classList.add("cg-line-in");
  }

  private async requestCopilotLine(entry: StarEntry) {
    const token = ++this.reflectToken;
    try {
      const res = await reflectGalaxy({
        star_id: entry.id,
        recent_star_ids: this.recentStarIds.filter((id) => id !== entry.id).slice(0, 8),
        ship: { x: Math.round(this.ship.x), y: Math.round(this.ship.y) },
        mode: "land",
      });
      // Stale — the user has landed elsewhere or closed the panel. Drop it (a newer
      // requestCopilotLine owns the loader now).
      if (token !== this.reflectToken || this.openStarId !== entry.id) return;
      if (res.line) this.revealCopilotLine(res.line);
      else this.setCopilotLoading(false);
      this.copilotPoint = res.point;
      this.showPointAffordance(res.point);
    } catch {
      // Couldn't reach the server — never leave a spinner stuck; show a graceful,
      // grounded line so the panel is never empty. (The server returns its own
      // deterministic line on LLM trouble, so this only fires on a real outage.)
      if (token !== this.reflectToken || this.openStarId !== entry.id) return;
      this.revealCopilotLine(
        `"${truncate(entry.data.text, 80)}" — sit with this one a moment. What does it mean to you now?`,
      );
    }
  }

  private showPointAffordance(point: CopilotPoint | null) {
    const btn = this.pointBtn as HTMLButtonElement | null;
    if (!btn) return;
    // Only offer to fly somewhere real that isn't the star you're already on.
    if (point && point.star_id !== this.openStarId && this.stars.some((s) => s.id === point.star_id)) {
      btn.textContent = "Show me where →";
      btn.style.display = "inline-flex";
    } else {
      btn.style.display = "none";
    }
  }

  private hidePointAffordance() {
    const btn = this.pointBtn as HTMLButtonElement | null;
    if (btn) btn.style.display = "none";
  }

  // ── Star notes: free-text context the user adds to a star ──
  // "Where this came from" — the lesson's own origin (context + source ref), so a
  // note has something to anchor to even when the originating moment is gone.
  private provenanceLine(d: GalaxyStar): string {
    const bits: string[] = [];
    if (d.context) bits.push(d.context.trim());
    if (d.source_ref) bits.push(d.source_ref.trim());
    return bits.filter(Boolean).join("  ·  ");
  }

  private resetNoteComposer() {
    const input = this.noteInput as HTMLTextAreaElement | null;
    if (input) input.value = "";
    const btn = this.noteSaveBtn as HTMLButtonElement | null;
    if (btn) { btn.disabled = false; btn.textContent = "Save note"; }
  }

  private async loadStarNotes(starId: number, count: number) {
    const listEl = this.q("#cg-p-notes-list");
    if (listEl) listEl.innerHTML = "";
    const token = ++this.notesToken;
    if (count <= 0) return; // nothing stored yet — composer only, no request
    try {
      const notes = await fetchStarNotes(starId);
      if (token !== this.notesToken || this.openStarId !== starId) return; // landed elsewhere
      for (const n of notes) this.renderNote(n, false);
    } catch {
      // Best-effort — the composer still works if the list can't load.
    }
  }

  private renderNote(note: StarNote, prepend: boolean) {
    const listEl = this.q("#cg-p-notes-list");
    if (!listEl) return;
    const item = document.createElement("div");
    item.className = "cg-note-item";
    const body = document.createElement("div");
    body.className = "cg-note-text";
    body.textContent = note.text;
    const when = document.createElement("div");
    when.className = "cg-note-when";
    when.textContent = this.formatNoteDate(note.created_at);
    item.appendChild(body);
    item.appendChild(when);
    if (prepend) listEl.prepend(item);
    else listEl.appendChild(item);
  }

  private formatNoteDate(iso: string): string {
    const t = Date.parse(iso);
    if (Number.isNaN(t)) return "";
    try {
      return new Date(t).toLocaleDateString(undefined, { month: "short", day: "numeric", year: "numeric" });
    } catch {
      return "";
    }
  }

  private async saveStarNote() {
    if (this.savingNote) return; // guard against double-save (fast clicks / ⌘↵)
    const input = this.noteInput as HTMLTextAreaElement | null;
    const btn = this.noteSaveBtn as HTMLButtonElement | null;
    if (!input || this.openStarId === null) return;
    const text = input.value.trim();
    if (!text) return;
    const starId = this.openStarId;
    this.savingNote = true;
    if (btn) { btn.disabled = true; btn.textContent = "Saving…"; }
    try {
      const note = await createStarNote(starId, text);
      // Only touch the panel if it's still showing the same star.
      if (this.openStarId === starId) {
        input.value = "";
        this.renderNote(note, true);
        if (btn) { btn.disabled = false; btn.textContent = "Saved ✓"; }
      }
      // The note may have grown the star — promote its visual (any star, even if
      // the panel moved on).
      if (note.star_stage) {
        const entry = this.stars.find((s) => s.id === starId);
        if (entry) this.applyStarStage(entry, note.star_stage);
      }
    } catch {
      if (btn && this.openStarId === starId) { btn.disabled = false; btn.textContent = "Couldn't save — try again"; }
    } finally {
      this.savingNote = false;
    }
  }

  // ── Tutoring: the 5-phase "go deeper" conversation that grows a star ──
  private async openTutor(entry: StarEntry) {
    if (this.tutorBusy || this.tutorStarEntry) return; // already opening / in a session
    const panel = this.q("#cg-panel");
    if (panel) panel.classList.remove("open"); // swap the landing panel for the conversation
    this.tutorStarEntry = entry;
    this.tutorSessionId = null;
    this.tutorBusy = true;
    this.paused = true;
    const starEl = this.q("#cg-tutor-star");
    if (starEl) starEl.textContent = truncate(entry.data.text, 80);
    const thread = this.q("#cg-tutor-thread");
    if (thread) thread.innerHTML = "";
    this.resetTutorControls();
    this.setTutorPhase("", 0, 5);
    const overlay = this.q("#cg-tutor");
    if (overlay) overlay.classList.add("open");
    (this.tutorInput as HTMLTextAreaElement | null)?.focus(); // ready to type (also suspends flight keys)
    this.tutorThinking(true);
    try {
      const res = await tutorStart(entry.id);
      if (this.tutorStarEntry !== entry) return; // closed / switched
      this.tutorSessionId = res.session_id;
      this.tutorThinking(false);
      this.renderTutorMessage("copilot", res.message);
      this.setTutorPhase(res.current_phase, res.phase_index, res.total_phases);
    } catch {
      this.tutorThinking(false);
      this.renderTutorMessage("copilot", "I couldn't start just now — let's try again in a moment.");
    } finally {
      this.tutorBusy = false;
    }
  }

  private async tutorSend() {
    if (this.tutorBusy || !this.tutorSessionId || !this.tutorStarEntry) return;
    const input = this.tutorInput as HTMLTextAreaElement | null;
    if (!input) return;
    const text = input.value.trim();
    if (!text) return;
    const entry = this.tutorStarEntry;
    input.value = "";
    this.renderTutorMessage("you", text);
    await this.tutorTurn(entry, text, "continue");
  }

  private async tutorSkipPhase() {
    if (this.tutorBusy || !this.tutorSessionId || !this.tutorStarEntry) return;
    const entry = this.tutorStarEntry;
    this.renderTutorMessage("you", "(let's move on)");
    await this.tutorTurn(entry, "skip", "skip");
  }

  private async tutorTurn(entry: StarEntry, message: string, action: "continue" | "skip") {
    if (!this.tutorSessionId) return;
    const sid = this.tutorSessionId;
    this.tutorBusy = true;
    this.tutorThinking(true);
    try {
      const res = await tutorMessage(entry.id, sid, message, action);
      if (this.tutorStarEntry !== entry) return;
      this.tutorThinking(false);
      if (res.message) this.renderTutorMessage("copilot", res.message);
      this.setTutorPhase(res.current_phase, res.phase_index, res.total_phases);
      if (res.mastery_achieved || res.session_close) {
        const grew = res.session_close?.new_star_stage;
        this.tutorSessionId = null; // auto-ended server-side
        this.renderTutorClosing(grew);
        if (grew) this.applyStarStage(entry, grew);
      }
    } catch (err) {
      if (this.tutorStarEntry !== entry) return;
      this.tutorThinking(false);
      if ((err as { status?: number })?.status === 404) {
        // The cached session expired (long idle). Don't strand the user in a dead
        // conversation — close it out cleanly.
        this.tutorSessionId = null;
        this.renderTutorMessage("copilot", "This session timed out — come back any time to go deeper again.");
        this.lockTutorComposer();
      } else {
        this.renderTutorMessage("copilot", "Something hiccuped there — say that again?");
      }
    } finally {
      this.tutorBusy = false;
    }
  }

  private async closeTutor(endSession: boolean) {
    const entry = this.tutorStarEntry;
    const sid = this.tutorSessionId;
    const overlay = this.q("#cg-tutor");
    if (overlay) overlay.classList.remove("open");
    this.setGameKeyboard(true);
    this.tutorStarEntry = null;
    this.tutorSessionId = null;
    this.openStarId = null;
    this.paused = false; // back to flight
    if (endSession && sid && entry) {
      try {
        const res = await tutorEnd(entry.id, sid);
        // Only touch the star if the scene is still alive and the star still exists.
        if (!this.destroyed && res?.new_star_stage && this.stars.includes(entry)) {
          this.applyStarStage(entry, res.new_star_stage);
        }
      } catch {
        // best-effort — the session expires on its own
      }
    }
  }

  private lockTutorComposer() {
    const send = this.q("#cg-tutor-send") as HTMLButtonElement | null;
    if (send) send.disabled = true;
    const skip = this.q("#cg-tutor-skip") as HTMLButtonElement | null;
    if (skip) skip.disabled = true;
    const end = this.q("#cg-tutor-end");
    if (end) end.textContent = "Done";
  }

  private resetTutorControls() {
    const send = this.q("#cg-tutor-send") as HTMLButtonElement | null;
    if (send) send.disabled = false;
    const skip = this.q("#cg-tutor-skip") as HTMLButtonElement | null;
    if (skip) skip.disabled = false;
    const end = this.q("#cg-tutor-end");
    if (end) end.textContent = "End session";
  }

  private renderTutorMessage(role: "copilot" | "you", text: string) {
    const thread = this.q("#cg-tutor-thread");
    if (!thread) return;
    const msg = document.createElement("div");
    msg.className = `cg-msg cg-msg-${role}`;
    msg.textContent = text;
    thread.appendChild(msg);
    this.scrollThreadToEnd();
  }

  private tutorThinking(on: boolean) {
    const thread = this.q("#cg-tutor-thread");
    if (!thread) return;
    const existing = thread.querySelector(".cg-msg-thinking");
    if (on) {
      if (existing) return;
      const msg = document.createElement("div");
      msg.className = "cg-msg cg-msg-copilot cg-msg-thinking";
      msg.innerHTML = '<span class="cg-loading-stars"><i>✦</i><i>✦</i><i>✦</i></span>';
      thread.appendChild(msg);
      this.scrollThreadToEnd();
    } else if (existing) {
      existing.remove();
    }
  }

  private renderTutorClosing(grewStage?: string) {
    const thread = this.q("#cg-tutor-thread");
    if (thread) {
      const msg = document.createElement("div");
      msg.className = "cg-tutor-closing";
      msg.textContent = grewStage
        ? `Session complete — this star grew to ${grewStage}. ✦`
        : "Session complete. ✦";
      thread.appendChild(msg);
      this.scrollThreadToEnd();
    }
    this.lockTutorComposer();
  }

  // Scroll the thread on the next frame — on-screen keyboards (mobile) resize the
  // viewport a tick late, so an immediate scrollTop can land short.
  private scrollThreadToEnd() {
    const thread = this.q("#cg-tutor-thread");
    if (thread) requestAnimationFrame(() => { thread.scrollTop = thread.scrollHeight; });
  }

  private setTutorPhase(phase: string, index: number, total: number) {
    const label = this.q("#cg-tutor-phase");
    if (label) label.textContent = phase ? `${phase.replace(/_/g, " ")} · ${Math.min(index + 1, total)}/${total}` : "";
    const pips = this.q("#cg-tutor-pips");
    if (pips) {
      pips.innerHTML = "";
      for (let i = 0; i < total; i++) {
        const pip = document.createElement("i");
        pip.className = "cg-pip" + (i <= index ? " on" : "");
        pips.appendChild(pip);
      }
    }
  }

  // ── Visible growth: promote a star's look when it grows a stage ──
  private applyStarStage(entry: StarEntry, stage: string) {
    const rank: Record<string, number> = { proto: 0, ignited: 1, radiant: 2, supernova: 3 };
    if (!(stage in rank) || rank[stage] <= rank[entry.data.star_stage]) return; // promote only
    entry.data.star_stage = stage as StarStage;
    this.promoteStar(entry);
  }

  private promoteStar(entry: StarEntry) {
    if (this.destroyed || !this.sys?.isActive()) return; // a late async resolve after teardown
    const cfg = STAGE[entry.data.star_stage] || STAGE_FALLBACK;
    const tint = clusterColor(entry.data.cluster_id);
    entry.r = cfg.size;
    entry.glow.setTint(tint);
    entry.core.setTint(tint);
    entry.label.setTint(tint);
    this.tweens.killTweensOf(entry.glow);
    this.tweens.killTweensOf(entry.core);
    const glowScale = (cfg.size * 2.9 * cfg.glow) / 62;
    this.tweens.add({ targets: entry.core, scale: cfg.size / 8, duration: 650, ease: "Back.Out" });
    this.tweens.add({
      targets: entry.glow,
      scale: glowScale,
      duration: 650,
      ease: "Back.Out",
      onComplete: () => {
        // radiant+ stars pulse; (re)start it around the new size.
        if (cfg.pulse) {
          this.tweens.add({
            targets: entry.glow,
            scale: glowScale * 1.18,
            duration: 900 + Math.random() * 400,
            yoyo: true,
            repeat: -1,
            ease: "Sine.inOut",
          });
        }
      },
    });
    // One-shot bloom so the growth reads as a moment.
    const flash = this.add
      .image(entry.x, entry.y, "glow")
      .setTint(0xffffff)
      .setBlendMode(Phaser.BlendModes.ADD)
      .setScale(glowScale * 0.6)
      .setAlpha(0.85)
      .setDepth(5);
    this.tweens.add({ targets: flash, scale: glowScale * 1.9, alpha: 0, duration: 700, ease: "Sine.Out", onComplete: () => flash.destroy() });
  }

  // ── Phase 3: waypoint. A dashed heading drawn from the ship toward a star the
  // co-pilot named (its EXACT world coords — no vision, just a lookup), plus a
  // pulsing ring on the target. Cleared on arrival or when superseded.
  private setWaypoint(starId: number) {
    const star = this.stars.find((s) => s.id === starId) || null;
    this.clearWaypoint();
    if (!star) return;
    this.waypointStar = star;
    const pulse = this.add
      .image(star.x, star.y, "glow")
      .setTint(BEAM)
      .setBlendMode(Phaser.BlendModes.ADD)
      .setScale(0.6)
      .setAlpha(0.6)
      .setDepth(5);
    this.tweens.add({ targets: pulse, scale: 1.7, alpha: 0, duration: 1100, repeat: -1, ease: "Sine.Out" });
    this.waypointPulse = pulse;
    // Briefly reveal where it is — ease the zoom out so the target comes into view
    // around the (still-followed) ship, then ease back. Honors "show me".
    this.revealWaypoint();
  }

  private reducedMotion(): boolean {
    // Called per-frame now (flight feel) — cache the MediaQueryList; .matches
    // stays live if the OS setting changes mid-session.
    try {
      if (typeof window === "undefined") return false;
      this.rmQuery ??= window.matchMedia("(prefers-reduced-motion: reduce)");
      return this.rmQuery.matches;
    } catch {
      return false;
    }
  }

  private revealWaypoint() {
    const wp = this.waypointStar;
    if (!wp || this.reducedMotion()) return; // reduced-motion: the edge arrow + minimap carry it
    const cam = this.cameras.main;
    const d = Phaser.Math.Distance.Between(this.ship.x, this.ship.y, wp.x, wp.y);
    const need = Math.min(cam.width, cam.height) / (d * 2 + 240);
    const target = Phaser.Math.Clamp(need, 0.3, this.flightZoom);
    if (target >= this.flightZoom - 0.01) return; // already on-screen — nothing to reveal
    this.tweenZoom(target, 700, 1800); // hold through the dwell so the speed zoom can't cut the reveal short
    this.revealTimer?.remove();
    this.revealTimer = this.time.delayedCall(1800, () => {
      // Don't fight a survey-mode zoom if the player entered the map meanwhile.
      if (this.waypointStar && !this.mapMode) this.tweenZoom(this.flightZoom, 600);
    });
  }

  private clearWaypoint() {
    this.waypointStar = null;
    this.revealTimer?.remove();
    this.revealTimer = undefined;
    if (this.waypointGfx) this.waypointGfx.clear();
    if (this.waypointEdge) this.waypointEdge.clear();
    if (this.waypointEdgeText) this.waypointEdgeText.setVisible(false);
    if (this.waypointPulse) {
      this.tweens.killTweensOf(this.waypointPulse);
      this.waypointPulse.destroy();
      this.waypointPulse = undefined;
    }
  }

  private updateWaypoint() {
    const g = this.waypointGfx;
    const edge = this.waypointEdge;
    const edgeText = this.waypointEdgeText;
    if (!g) return;
    g.clear();
    if (edge) edge.clear();
    const wp = this.waypointStar;
    if (!wp) {
      if (edgeText) edgeText.setVisible(false);
      return;
    }
    const cam = this.cameras.main;
    const d = Phaser.Math.Distance.Between(this.ship.x, this.ship.y, wp.x, wp.y);
    if (d < CONFIG.DOCK_RADIUS) { this.clearWaypoint(); return; }

    const view = cam.worldView;
    const onScreen = view.contains(wp.x, wp.y);
    const ang = Phaser.Math.Angle.Between(this.ship.x, this.ship.y, wp.x, wp.y);

    // World-space dashed heading from the ship. On-screen it reaches the target;
    // off-screen it's a short stub and the screen-edge arrow carries the rest.
    const start = 26;
    const reach = onScreen ? d - wp.r - 10 : 230;
    const len = Math.max(start, Math.min(reach, 230));
    g.lineStyle(2, BEAM, 0.7);
    const dash = 13, gap = 9;
    for (let t = start; t < len; t += dash + gap) {
      const e = Math.min(t + dash, len);
      g.lineBetween(
        this.ship.x + Math.cos(ang) * t, this.ship.y + Math.sin(ang) * t,
        this.ship.x + Math.cos(ang) * e, this.ship.y + Math.sin(ang) * e,
      );
    }

    if (onScreen) {
      // Mark the destination with a solid ring (the fading pulse alone is too subtle).
      g.lineStyle(2.5, BEAM, 0.9);
      g.strokeCircle(wp.x, wp.y, wp.r + 16);
      if (edgeText) edgeText.setVisible(false);
    } else if (edge && edgeText) {
      // Off-screen: an arrow pinned to the screen edge, pointing the way, labelled.
      const sx = ((wp.x - view.x) / view.width) * cam.width;
      const sy = ((wp.y - view.y) / view.height) * cam.height;
      const cx = cam.width / 2, cy = cam.height / 2;
      const m = 56;
      const dx = sx - cx, dy = sy - cy;
      let t = Infinity;
      if (dx > 0) t = Math.min(t, (cam.width - m - cx) / dx);
      else if (dx < 0) t = Math.min(t, (m - cx) / dx);
      if (dy > 0) t = Math.min(t, (cam.height - m - cy) / dy);
      else if (dy < 0) t = Math.min(t, (m - cy) / dy);
      if (!isFinite(t) || t < 0) t = 0;
      const ix = cx + dx * t, iy = cy + dy * t;
      const ea = Math.atan2(dy, dx);
      edge.fillStyle(BEAM, 0.95);
      edge.fillTriangle(
        ix + Math.cos(ea) * 13, iy + Math.sin(ea) * 13,
        ix + Math.cos(ea + 2.5) * 11, iy + Math.sin(ea + 2.5) * 11,
        ix + Math.cos(ea - 2.5) * 11, iy + Math.sin(ea - 2.5) * 11,
      );
      edge.lineStyle(2, BEAM, 0.5);
      edge.strokeCircle(ix, iy, 17);
      // Label sits inside the arrow (offset enough to clear it in the corners).
      const lx = Phaser.Math.Clamp(ix - Math.cos(ea) * 48, m, cam.width - m);
      const ly = Phaser.Math.Clamp(iy - Math.sin(ea) * 48, m, cam.height - m);
      edgeText.setText(truncate(wp.data.text, 18)).setPosition(lx, ly).setVisible(true);
    }
  }

  // ── Phase 2: ambient awareness. When you drift to a near-stop inside a
  // neighbourhood for a few seconds, the co-pilot drops one throttled toast that
  // speaks to where you're lingering. Reuses the same reflect endpoint (ambient
  // mode) and is heavily rate-limited so it never nags.
  private ambientTick(time: number) {
    if (this.paused || this.encounterActive || this.ambientInFlight) { return; }
    const v = this.ship.body?.velocity;
    const speed = v ? Math.hypot(v.x, v.y) : 0;
    if (speed >= 45) { this.dwellSince = 0; return; }
    if (this.dwellSince === 0) { this.dwellSince = time; return; }
    if (time - this.dwellSince < 4200) return; // must linger ~4s
    if (this.lastAmbientAt && time - this.lastAmbientAt < 75000) return; // ~once / 75s
    const near = this.nearestStar(360);
    const inHood = this.neighborhoods.some(
      (n) => Phaser.Math.Distance.Between(this.ship.x, this.ship.y, n.cx, n.cy) < n.r * 1.1,
    );
    if (!near || !inHood) { this.dwellSince = time; return; }
    this.lastAmbientAt = time;
    this.dwellSince = 0;
    void this.fireAmbient(near);
  }

  private nearestStar(maxD: number): StarEntry | null {
    let best: StarEntry | null = null;
    let bd = maxD;
    for (const s of this.stars) {
      const d = Phaser.Math.Distance.Between(this.ship.x, this.ship.y, s.x, s.y);
      if (d < bd) { bd = d; best = s; }
    }
    return best;
  }

  private async fireAmbient(near: StarEntry) {
    this.ambientInFlight = true;
    try {
      const res = await reflectGalaxy({
        star_id: near.id,
        recent_star_ids: this.recentStarIds.slice(0, 8),
        ship: { x: Math.round(this.ship.x), y: Math.round(this.ship.y) },
        mode: "ambient",
      });
      if (this.paused || this.encounterActive) return; // never pop a toast over a panel/duel
      this.showToast(res.line);
    } catch {
      // Ambient is best-effort — stay silent on failure.
    } finally {
      this.ambientInFlight = false;
    }
  }

  private showToast(text: string) {
    const el = this.toastEl;
    if (!el || !text) return;
    el.textContent = text;
    el.classList.add("show");
    if (this.toastTimer) window.clearTimeout(this.toastTimer);
    this.toastTimer = window.setTimeout(() => el.classList.remove("show"), 7000);
  }

  closePanel() {
    const p = this.q("#cg-panel");
    if (p) p.classList.remove("open");
    // Blur the note textarea FIRST — closing via the backdrop / "Got it" leaves it
    // focused, so its own blur handler never fires. Then re-arm flight keys (the
    // setGameKeyboard call is idempotent if the blur already re-enabled them).
    const input = this.noteInput as HTMLTextAreaElement | null;
    if (input && document.activeElement === input) input.blur();
    this.setGameKeyboard(true);
    this.openStarId = null;
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
    this.clearWaypoint();

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
    this.tweenZoom(targetZoom, 700);

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
    this.tweenZoom(this.encPrevZoom || this.flightZoom, 520);
    this.cameras.main.startFollow(this.ship, true, 0.08, 0.08);
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
    this.fitOverlays();
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
    // Waypoint target (drawn first, under the ship marker): a line from the ship +
    // a beam-coloured dot, so you can navigate to it by the map even when it's
    // off-screen in the main view.
    if (this.waypointStar) {
      const wx = pad + this.waypointStar.x * s;
      const wy = pad + this.waypointStar.y * s;
      this.miniMarker.lineStyle(1, BEAM, 0.45);
      this.miniMarker.lineBetween(mx, my, wx, wy);
      this.miniMarker.fillStyle(BEAM, 1);
      this.miniMarker.fillCircle(wx, wy, 2.2);
      this.miniMarker.lineStyle(1.4, BEAM, 0.95);
      this.miniMarker.strokeCircle(wx, wy, 4.6);
    }
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
    if (this.mapMode) {
      if (Phaser.Input.Keyboard.JustDown(this.keys.M) || Phaser.Input.Keyboard.JustDown(this.keys.ESC)) this.exitMapMode(null);
      else this.updateMapMode();
      return;
    }
    const dt = delta / 1000;
    const ship = this.ship;
    if (this.paused) {
      this.ring.clear();
      this.prompt.setVisible(false);
      if (this.landBtn) this.landBtn.classList.remove("show");
      if (this.touchGfx) this.touchGfx.clear();
      // Let the camera's velocity lead settle back onto the (now parked) ship so
      // a panel never opens with the star pushed off-centre.
      if (!this.reducedMotion()) {
        const lookEase = 1 - Math.exp(-3.5 * dt);
        this.camLook.x = Phaser.Math.Linear(this.camLook.x, 0, lookEase);
        this.camLook.y = Phaser.Math.Linear(this.camLook.y, 0, lookEase);
        this.cameras.main.setFollowOffset(-this.camLook.x, -this.camLook.y);
      }
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
    const braking = this.cursors.down.isDown || this.keys.S.isDown;
    this.touchMag = 1;

    if (left || right || keyThrust) {
      this.autopilot = null;
      if (left) ship.rotation -= CONFIG.SHIP.turn * dt;
      if (right) ship.rotation += CONFIG.SHIP.turn * dt;
      thrusting = keyThrust;
    } else if (this.touch.active && this.touch.moved > 12) {
      // The drag is a thumbstick: direction steers, throw throttles — a short
      // drag noses around a neighbourhood, a full one opens the burn.
      this.steerToward(Phaser.Math.Angle.Between(this.touch.anchorX, this.touch.anchorY, this.touch.curX, this.touch.curY), dt);
      const throw_ = Phaser.Math.Distance.Between(this.touch.anchorX, this.touch.anchorY, this.touch.curX, this.touch.curY);
      this.touchMag = Phaser.Math.Clamp(throw_ / 56, 0.3, 1); // 56 = the stick's visual radius
      thrusting = true;
    } else if (this.autopilot) {
      // Tap/click-to-travel with proper "arrive" steering. The old version thrust
      // whenever dist > 50 regardless of heading, so a fast ship that couldn't
      // turn tightly enough overshot and ORBITED the target forever ("the ship
      // shot around by itself"). Now: ease the speed down near the target so the
      // turn radius shrinks, and only thrust when actually pointed at it — so the
      // ship converges and settles instead of looping.
      const t = this.autopilot;
      const ang = Phaser.Math.Angle.Between(ship.x, ship.y, t.x, t.y);
      const dist = Phaser.Math.Distance.Between(ship.x, ship.y, t.x, t.y);
      const arriveR = t.star ? t.star.r + 34 : 44;
      if (dist <= arriveR) {
        const arrived = t.star;
        this.autopilot = null;
        if (arrived) {
          this.dockAndOpen(arrived);
        } else {
          ship.body.setVelocity(0, 0);
        }
      } else {
        this.steerToward(ang, dt);
        const aim = Math.abs(Phaser.Math.Angle.Wrap(ang - ship.rotation)); // 0 = dead-on
        // Distance-proportional speed cap (floor 60) — brake toward it so the ship
        // can curve onto the target and reach the arrival radius.
        const cap = Phaser.Math.Clamp((dist / 260) * CONFIG.SHIP.maxVel, 60, CONFIG.SHIP.maxVel);
        const speed = ship.body.velocity.length();
        if (speed > cap) ship.body.velocity.scale(cap / speed);
        // Only thrust when roughly pointed at the target (~40°) and under the cap,
        // so we never accelerate sideways into a loop we can't turn out of.
        thrusting = aim < 0.7 && speed < cap;
      }
    }

    // The brake beats thrust: S/↓ eases you to a stop and drops any course.
    if (braking) {
      this.autopilot = null;
      thrusting = false;
      ship.body.velocity.scale(Math.max(0, 1 - CONFIG.FEEL.brake * dt));
    }

    if (thrusting) {
      this.physics.velocityFromRotation(ship.rotation, CONFIG.SHIP.accel * this.touchMag, ship.body.acceleration);
      this.flame.emitParticleAt(ship.x - Math.cos(ship.rotation) * 16, ship.y - Math.sin(ship.rotation) * 16, 2);
    } else {
      ship.body.setAcceleration(0, 0);
    }

    this.flightFeel(time, dt);
    this.drawTouchStick();

    this.updateNeighborhoods();

    let best: StarEntry | null = null, bestD = CONFIG.DOCK_RADIUS;
    for (const s of this.stars) {
      const d = Phaser.Math.Distance.Between(ship.x, ship.y, s.x, s.y);
      if (d < bestD) { bestD = d; best = s; }
    }
    this.candidate = best;
    this.ring.clear();
    if (best) {
      // The dock ring breathes — "you can land here" should beckon, not assert.
      const wob = this.reducedMotion() ? 0 : Math.sin(time / 200);
      this.ring.lineStyle(2, 0xffffff, 0.78 + wob * 0.18);
      this.ring.strokeCircle(best.x, best.y, best.r + 16 + wob * 2.5);
      this.prompt.setPosition(best.x, best.y - best.r - 22).setVisible(true);
      if (this.landBtn) this.landBtn.classList.add("show");
      if (Phaser.Input.Keyboard.JustDown(this.keys.E)) this.dockAndOpen(best);
    } else {
      this.prompt.setVisible(false);
      if (this.landBtn) this.landBtn.classList.remove("show");
    }
    this.updateMinimap();
    this.updateWaypoint();
    this.ambientTick(time);
    if (Phaser.Input.Keyboard.JustDown(this.keys.M)) this.enterMapMode();
    if (Phaser.Input.Keyboard.JustDown(this.keys.ESC)) this.closePanel();
  }
}

export function mountGalaxyGame(canvasParent: HTMLElement, overlayRoot: HTMLElement, galaxy: GalaxyData): Phaser.Game {
  return new Phaser.Game({
    type: Phaser.AUTO,
    parent: canvasParent,
    backgroundColor: "#0b0f13",
    scale: { mode: Phaser.Scale.RESIZE, width: "100%", height: "100%" },
    // Hint the OS to pick the performant GPU path (Phaser default is "default").
    // Phaser renders at CSS-pixel resolution (it never multiplies by
    // devicePixelRatio), so this is the lever, not a DPR cap.
    render: { powerPreference: "high-performance" },
    physics: { default: "arcade", arcade: { debug: false } },
    scene: new GalaxyScene(galaxy, overlayRoot),
  });
}
