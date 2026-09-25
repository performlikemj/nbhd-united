/**
 * Constellation: a living Milky Way with the user's themes as constellations
 * (port of the approved mockup ConstellationA; theme layout mirrors iOS
 * SkyThemeLayout so web and phone draw the same figures). Nebula + two star
 * layers are cached per size; per frame: parallax drift + theme glints/lines.
 */
import type { ConstellationData, ConstellationNode } from "@/lib/types";

import { clamp255, ctx2d, fbm, hsh, makeCanvas, seeded, smooth, starSprite } from "./noise";

export interface SkyTheme {
  id: string;
  name: string;
  lessons: ConstellationNode[];
  count: number;
  stars: { x: number; y: number }[]; // fractions of the sky
  mags: number[];
  links: [number, number][];
  label: { x: number; y: number }; // fraction, top-left of the name
}

const SLOTS = [
  { x: 0.25, y: 0.3 }, { x: 0.8, y: 0.3 }, { x: 0.52, y: 0.55 }, { x: 0.2, y: 0.68 },
  { x: 0.8, y: 0.66 }, { x: 0.55, y: 0.22 }, { x: 0.62, y: 0.4 }, { x: 0.14, y: 0.46 },
];

function seedOf(text: string): number {
  let h = 5381;
  for (const ch of text) h = (Math.imul(h, 33) + (ch.codePointAt(0) ?? 0)) & 0x7fffffff;
  return h;
}

/** Group lessons by cluster → up to 8 constellations at stable slots (iOS parity). */
export function skyThemes(data: ConstellationData): SkyTheme[] {
  const xs = data.nodes.map((n) => n.x).filter((v): v is number => v != null);
  const ys = data.nodes.map((n) => n.y).filter((v): v is number => v != null);
  const bx = xs.length ? [Math.min(...xs), Math.max(...xs)] : null;
  const by = ys.length ? [Math.min(...ys), Math.max(...ys)] : null;
  const norm = (v: number, lo: number, hi: number) => (hi > lo ? 0.06 + ((v - lo) / (hi - lo)) * 0.88 : 0.5);
  const nxy = (n: ConstellationNode, i: number, total: number) =>
    bx && by && n.x != null && n.y != null
      ? { nx: norm(n.x, bx[0], bx[1]), ny: norm(n.y, by[0], by[1]) }
      : { nx: 0.5 + 0.4 * Math.cos((i / Math.max(total, 1)) * 2 * Math.PI), ny: 0.5 + 0.4 * Math.sin((i / Math.max(total, 1)) * 2 * Math.PI) };

  const groups = new Map<string, ConstellationNode[]>();
  for (const n of data.nodes) {
    const key = n.cluster_id == null ? "other" : String(n.cluster_id);
    groups.set(key, [...(groups.get(key) ?? []), n]);
  }
  const keys = [...groups.keys()].sort((a, b) => (a === "other" ? 1 : b === "other" ? -1 : Number(a) - Number(b)));
  return keys.slice(0, SLOTS.length).map((key, index) => {
    const nodes = [...(groups.get(key) ?? [])].sort((a, b) => a.id - b.id);
    const name = nodes.find((n) => n.cluster_label)?.cluster_label || "Other lessons";
    const anchor = SLOTS[index];
    const shown = nodes.slice(0, 5);
    const n = Math.max(shown.length, 1);
    const s = seedOf(key + name);
    const boxW = 0.2, boxH = 0.14;
    const pos = shown.map((node, i) => nxy(node, i, shown.length));
    const sx = pos.map((p) => p.nx), sy = pos.map((p) => p.ny);
    const spreadX = Math.max(...sx) - Math.min(...sx), spreadY = Math.max(...sy) - Math.min(...sy);
    let stars: { x: number; y: number }[];
    if (shown.length >= 2 && spreadX > 0.02 && spreadY > 0.02) {
      const minX = Math.min(...sx), minY = Math.min(...sy);
      stars = pos.map((p) => ({ x: anchor.x + ((p.nx - minX) / spreadX - 0.5) * boxW, y: anchor.y + ((p.ny - minY) / spreadY - 0.5) * boxH }));
    } else {
      stars = Array.from({ length: n }, (_, i) => {
        const angle = (i / n) * 2 * Math.PI + hsh(s, i, 7) * 0.9;
        const radius = 0.45 + 0.4 * hsh(s, i, 11);
        return { x: anchor.x + (Math.cos(angle) * boxW) / 2 * radius, y: anchor.y + (Math.sin(angle) * boxH) / 2 * radius };
      });
    }
    const mags = Array.from({ length: n }, (_, i) => (i === 0 ? 4.6 + 0.6 * hsh(s, 1, 3) : 2.2 + 1.6 * hsh(s, i, 5)));
    const cx = stars.reduce((a, p) => a + p.x, 0) / stars.length, cy = stars.reduce((a, p) => a + p.y, 0) / stars.length;
    const order = stars.map((_, i) => i).sort((a, b) => Math.atan2(stars[a].y - cy, stars[a].x - cx) - Math.atan2(stars[b].y - cy, stars[b].x - cx));
    const links: [number, number][] = [];
    for (let k = 0; k < order.length - 1; k++) links.push([order[k], order[k + 1]]);
    if (order.length >= 5) links.push([order[order.length - 1], order[0]]);
    const top = Math.min(...stars.map((p) => p.y)), left = Math.min(...stars.map((p) => p.x));
    return { id: key, name, lessons: shown.length ? shown : nodes, count: nodes.length, stars, mags, links, label: { x: Math.max(0.03, left - 0.02), y: Math.max(0.06, top - 0.07) } };
  });
}

export class MilkyWayRenderer {
  private cache?: { w: number; h: number; dpr: number; neb: HTMLCanvasElement; dim: HTMLCanvasElement; bright: HTMLCanvasElement };
  private cool?: HTMLCanvasElement;
  private warm?: HTMLCanvasElement;
  themes: SkyTheme[] = [];
  selected = 0;
  private lastSel = -1;
  private drawStart = 0;

  /** Which themes to draw and which one is lit (drawn in over ~0.9 s on change). */
  setScene(themes: SkyTheme[], selected: number) {
    this.themes = themes;
    this.selected = selected;
  }

  /** Band geometry in pixels: a diagonal from lower-left to upper-right. */
  private band(x: number, y: number, w: number, h: number) {
    const x0 = -0.2 * w, y0 = 0.9 * h, x1 = 1.2 * w, y1 = 0.07 * h;
    const dx = x1 - x0, dy = y1 - y0, L = Math.sqrt(dx * dx + dy * dy);
    const ux = dx / L, uy = dy / L, px = x - x0, py = y - y0;
    const along = (px * ux + py * uy) / L;
    const k = Math.min(w, h) / 390;
    let perp = -px * uy + py * ux;
    perp += 26 * k * (fbm(along * 3, 0.5, 41, 3) - 0.5) * 2;
    const bw = (64 + 26 * Math.sin(along * 5.5 + 1.0)) * k;
    return { along, perp, w: bw, k };
  }

  private build(w: number, h: number, dpr: number) {
    this.cool = starSprite("237,243,255", 48, [[0, 1], [0.1, 0.92], [0.2, 0.18], [0.5, 0.04], [1, 0]]);
    this.warm = starSprite("255,240,220", 48, [[0, 1], [0.1, 0.92], [0.2, 0.18], [0.5, 0.04], [1, 0]]);
    // Nebula at half resolution (smooth light), scaled up at draw time.
    const NW = Math.ceil(w / 2), NH = Math.ceil(h / 2);
    const neb = makeCanvas(NW, NH), ng = ctx2d(neb), img = ng.createImageData(NW, NH), d = img.data;
    for (let yy = 0; yy < NH; yy++) {
      for (let xx = 0; xx < NW; xx++) {
        const X = xx * 2, Y = yy * 2, b = this.band(X, Y, w, h), s = 1 / b.k;
        const core = Math.exp(-(b.perp * b.perp) / (b.w * b.w));
        const bulge = Math.exp(-Math.pow((b.along - 0.52) / 0.16, 2));
        const nn = fbm((X * s) / 70, (Y * s) / 70, 7, 5);
        const nebv = core * (0.25 + 0.75 * nn) * (0.55 + 0.9 * bulge);
        const wisps = Math.exp(-(b.perp * b.perp) / (b.w * b.w * 4)) * fbm((X * s) / 30, (Y * s) / 30, 19, 4) * 0.35;
        const dn = fbm((X * s) / 38 + 4, (Y * s) / 38, 29, 5);
        const dust = smooth(0.42, 0.78, dn) * Math.exp(-Math.pow((b.perp - 6 * b.k) / (b.w * 0.55), 2));
        const lane = Math.exp(-Math.pow((b.perp + 4 * b.k) / (9 * b.k), 2)) * smooth(0.35, 0.6, fbm((X * s) / 22, (Y * s) / 22, 51, 3)) * (0.4 + bulge);
        const I = (nebv + wisps) * (1 - 0.62 * dust) * (1 - 0.45 * Math.min(1, lane));
        const warm = bulge * 0.9;
        const o = (yy * NW + xx) * 4;
        d[o] = clamp255((150 + 105 * warm) * I * 0.5 + 7);
        d[o + 1] = clamp255((168 + 70 * warm) * I * 0.5 + 9);
        d[o + 2] = clamp255((222 - 30 * warm) * I * 0.5 + 12);
        d[o + 3] = 255;
      }
    }
    ng.putImageData(img, 0, 0);

    const layer = (count: number, seed: number, bright: boolean) => {
      const rnd = seeded(seed);
      const c = makeCanvas(w * dpr, h * dpr), g = ctx2d(c);
      g.globalCompositeOperation = "lighter";
      let n = 0, guard = 0;
      while (n < count && guard < count * 20) {
        guard++;
        const x = rnd() * w, y = rnd() * h;
        const b = this.band(x, y, w, h), s = 1 / b.k;
        let dens = 0.12 + Math.exp(-(b.perp * b.perp) / (b.w * b.w * 1.3));
        const dn = fbm((x * s) / 38 + 4, (y * s) / 38, 29, 3);
        dens *= 1 - 0.8 * smooth(0.46, 0.7, dn) * Math.exp(-Math.pow((b.perp - 6 * b.k) / (b.w * 0.55), 2));
        if (rnd() > dens) continue;
        n++;
        if (!bright) {
          const a = 0.12 + rnd() * 0.55, t = rnd();
          g.fillStyle = t > 0.85 ? `rgba(255,236,214,${a})` : t > 0.6 ? `rgba(214,228,255,${a})` : `rgba(240,244,255,${a})`;
          const sz = (rnd() > 0.9 ? 1 : 0.5) * dpr;
          g.fillRect(x * dpr, y * dpr, sz, sz);
        } else {
          const sp = rnd() > 0.8 ? this.warm! : this.cool!;
          const size = (3 + Math.pow(rnd(), 4) * 11) * dpr;
          g.globalAlpha = 0.35 + rnd() * 0.5;
          g.drawImage(sp, x * dpr - size / 2, y * dpr - size / 2, size, size);
          g.globalAlpha = 1;
        }
      }
      return c;
    };
    const area = (w * h) / (390 * 844);
    this.cache = { w, h, dpr, neb, dim: layer(Math.round(9000 * area), 5, false), bright: layer(Math.round(260 * area), 9, true) };
  }

  private glint(ctx: CanvasRenderingContext2D, x: number, y: number, size: number, a: number, spikes: boolean) {
    const cool = this.cool!;
    ctx.globalAlpha = a;
    const s = size * 5;
    ctx.drawImage(cool, x - s / 2, y - s / 2, s, s);
    if (spikes) {
      const L = size * 3, sa = (0.38 * a).toFixed(3);
      const gh = ctx.createLinearGradient(x - L, y, x + L, y);
      gh.addColorStop(0, "rgba(237,243,255,0)");
      gh.addColorStop(0.5, `rgba(255,255,255,${sa})`);
      gh.addColorStop(1, "rgba(237,243,255,0)");
      ctx.globalAlpha = 1;
      ctx.fillStyle = gh;
      ctx.fillRect(x - L, y - 0.35, L * 2, 0.7);
      const gv = ctx.createLinearGradient(x, y - L, x, y + L);
      gv.addColorStop(0, "rgba(237,243,255,0)");
      gv.addColorStop(0.5, `rgba(255,255,255,${sa})`);
      gv.addColorStop(1, "rgba(237,243,255,0)");
      ctx.fillStyle = gv;
      ctx.fillRect(x - 0.35, y - L, 0.7, L * 2);
    }
    ctx.globalAlpha = 1;
  }

  draw(ctx: CanvasRenderingContext2D, w: number, h: number, dpr: number, t: number) {
    if (!this.cache || this.cache.w !== w || this.cache.h !== h || this.cache.dpr !== dpr) this.build(w, h, dpr);
    const { neb, dim, bright } = this.cache!;
    if (this.lastSel !== this.selected) {
      this.lastSel = this.selected;
      this.drawStart = t;
    }
    const still = typeof window !== "undefined" && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    let prog = still ? 1 : Math.min(1, (t - this.drawStart) / 0.9);
    prog = 1 - Math.pow(1 - prog, 3);
    const ox = 6 * Math.sin(t / 47), oy = 4 * Math.cos(t / 61);

    ctx.setTransform(1, 0, 0, 1, 0, 0);
    ctx.globalCompositeOperation = "source-over";
    ctx.globalAlpha = 1;
    ctx.imageSmoothingEnabled = true;
    ctx.drawImage(neb, (ox - 12) * dpr, (oy - 12) * dpr, (w + 24) * dpr, (h + 24) * dpr);
    ctx.globalCompositeOperation = "lighter";
    ctx.drawImage(dim, ox * 1.3 * dpr, oy * 1.3 * dpr);
    ctx.globalAlpha = 0.85 + 0.15 * Math.sin(t * 0.7);
    ctx.drawImage(bright, ox * 2 * dpr, oy * 2 * dpr);
    ctx.globalAlpha = 1;

    ctx.setTransform(dpr, 0, 0, dpr, ox * 1.25 * dpr, oy * 1.25 * dpr);
    const scale = Math.min(1.25, Math.max(0.85, Math.min(w, h) / 480));
    this.themes.forEach((th, k) => {
      const on = k === this.selected;
      const p = on ? prog : 1;
      const pts = th.stars.map((s) => ({ x: s.x * w, y: s.y * h }));
      ctx.lineWidth = on ? 0.8 : 0.55;
      ctx.strokeStyle = on ? "rgba(236,240,255,0.55)" : "rgba(226,232,240,0.18)";
      th.links.forEach(([ai, bi], e) => {
        const seg = Math.max(0, Math.min(1, p * th.links.length - e));
        if (seg <= 0) return;
        const a = pts[ai], b = pts[bi];
        const vx = b.x - a.x, vy = b.y - a.y, L = Math.sqrt(vx * vx + vy * vy), gap = 9 * scale;
        if (L < gap * 2 + 2) return;
        const sx = a.x + (vx / L) * gap, sy = a.y + (vy / L) * gap;
        ctx.beginPath();
        ctx.moveTo(sx, sy);
        ctx.lineTo(sx + (vx / L) * (L - gap * 2) * seg, sy + (vy / L) * (L - gap * 2) * seg);
        ctx.stroke();
      });
      pts.forEach((pt, s) => {
        const tw = 0.85 + 0.15 * Math.sin(t * 1.1 + k * 1.7 + s * 2.3);
        this.glint(ctx, pt.x, pt.y, th.mags[s] * scale * (on ? 1.5 : 1.15), (on ? 1 : 0.72) * tw, on && s === 0);
      });
    });
    ctx.globalAlpha = 1;
    ctx.globalCompositeOperation = "source-over";
  }
}
