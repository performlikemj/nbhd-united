/**
 * Core: eclipse → sun (port of the approved iOS canvas mockup CoreSessionA).
 * Textures are built once; per frame only transforms/opacity + a few sprites.
 * `progress` (0..1) is the real playback position: 0 = full eclipse, 1 = the sun.
 */
import { clamp255, ctx2d, fbm, hsh, makeCanvas, seeded, starSprite, vnoise, worley } from "./noise";

export interface EclipseState {
  progress: number; // 0..1 real session progress
  beganAt: number | null; // seconds (performance clock) when Begin was pressed, for the soft glint
}

function sunTexture(seed: number): HTMLCanvasElement {
  const S = 440, c = makeCanvas(S, S), g = ctx2d(c), img = g.createImageData(S, S), d = img.data, R = S / 2;
  const spots: [number, number, number][] = [[-0.3, 0.2, 0.022], [-0.26, 0.235, 0.012]];
  for (let y = 0; y < S; y++) {
    for (let x = 0; x < S; x++) {
      const dx = (x + 0.5 - R) / R, dy = (y + 0.5 - R) / R, r2 = dx * dx + dy * dy;
      if (r2 > 1) continue;
      const mu = Math.sqrt(1 - r2);
      const fore = 0.45 + 0.55 * mu;
      const w = worley((dx * 52) / fore + seed * 7.3, dy * 52 + seed * 3.1, seed);
      const lane = Math.min(1, (w[1] - w[0]) / 0.3);
      const fine = fbm(dx * 9, dy * 9, seed + 5, 3);
      const limb = 0.5 + 0.5 * Math.pow(mu, 0.55);
      let I = limb * (0.84 + 0.16 * lane) * (0.92 + 0.12 * fine) * (1.02 + 0.1 * mu);
      for (const [sxo, syo, rad] of spots) {
        const sx = dx - sxo, sy = dy - syo, ds = Math.sqrt(sx * sx + sy * sy);
        if (ds < rad * 2.6) {
          const pen = 1 - ds / (rad * 2.6), umb = Math.max(0, 1 - ds / rad);
          I *= 1 - 0.35 * pen * pen - 0.55 * Math.min(1, umb * 2);
        }
      }
      const t = 1 - mu, o = (y * S + x) * 4;
      d[o] = clamp255(255 * I);
      d[o + 1] = clamp255((250 - 58 * Math.pow(t, 1.3)) * I);
      d[o + 2] = clamp255((240 - 120 * Math.pow(t, 0.9)) * I * 0.97);
      d[o + 3] = clamp255((1 - Math.sqrt(r2)) * R * 1.6 * 255);
    }
  }
  g.putImageData(img, 0, 0);
  return c;
}

function coronaTexture(): HTMLCanvasElement {
  const S = 360, c = makeCanvas(S, S), g = ctx2d(c), img = g.createImageData(S, S), d = img.data;
  const half = S / 2, Rpx = half / 3.2;
  const waves: [number, number, number][] = [];
  const plumes: [number, number][] = [];
  const helmets: [number, number, number][] = [];
  for (let k = 2; k <= 9; k++) waves.push([k, hsh(k, 1, 3) * 6.283, 0.5 / k]);
  for (let p = 20; p <= 70; p += 3) plumes.push([p, hsh(p, 2, 3) * 6.283]);
  for (let h = 0; h < 5; h++) helmets.push([hsh(h, 4, 9) * 6.283, 5 + hsh(h, 5, 9) * 8, 0.6 + 0.4 * hsh(h, 6, 9)]);
  for (let y = 0; y < S; y++) {
    for (let x = 0; x < S; x++) {
      const dx = (x + 0.5 - half) / Rpx, dy = (y + 0.5 - half) / Rpx, r = Math.sqrt(dx * dx + dy * dy);
      if (r < 0.97 || r > 3.2) continue;
      const th = Math.atan2(dy, dx) + 0.05 * (r - 1) * (r - 1);
      let wv = 0;
      for (const [k, ph, amp] of waves) wv += amp * Math.sin(k * th + ph);
      let pl = 0;
      for (const [k, ph] of plumes) pl += Math.sin(k * th + ph);
      pl = 0.5 + ((0.5 * pl) / plumes.length) * 3;
      let helm = 0;
      for (const [at, sharp, str] of helmets) {
        const a = Math.cos(th - at);
        if (a > 0) helm = Math.max(helm, Math.pow(a, sharp * 6) * str);
      }
      const fall = 3.6 - 1.7 * helm - 0.4 * wv;
      let I = Math.pow(1 / r, fall) * (0.7 + 0.08 * pl + 0.32 * helm) + 0.9 * Math.pow(1 / r, 9);
      I *= 0.9 + 0.1 * vnoise(th * 16, r * 1.5, 77);
      I *= 1 - Math.max(0, (r - 2.4) / 0.8);
      const rim = Math.max(0, 1 - Math.abs(r - 1.0) / 0.035);
      const warm = Math.max(0, 1 - (r - 1) / 0.35);
      const o = (y * S + x) * 4;
      d[o] = clamp255(232 + 23 * warm + 10 * rim);
      d[o + 1] = clamp255(236 - 20 * warm - 40 * rim);
      d[o + 2] = clamp255(255 - 55 * warm - 50 * rim);
      d[o + 3] = clamp255((I * 0.5 + rim * 0.14) * 255);
    }
  }
  g.putImageData(img, 0, 0);
  return c;
}

export class EclipseRenderer {
  private sunA?: HTMLCanvasElement;
  private sunB?: HTMLCanvasElement;
  private corona?: HTMLCanvasElement;
  private sprite?: HTMLCanvasElement;
  private glow?: HTMLCanvasElement;
  private field?: { w: number; h: number; c: HTMLCanvasElement };
  private twinkles: { x: number; y: number; s: number; f: number; p: number }[] = [];

  private setup() {
    this.sunA = sunTexture(1);
    this.sunB = sunTexture(2);
    this.corona = coronaTexture();
    this.sprite = starSprite("237,243,255", 32, [[0, 1], [0.1, 0.85], [0.28, 0.16], [1, 0]]);
    const glow = makeCanvas(400, 400), gg = ctx2d(glow), gr = gg.createRadialGradient(200, 200, 0, 200, 200, 200);
    gr.addColorStop(0, "rgba(255,238,214,0.5)");
    gr.addColorStop(0.3, "rgba(255,226,200,0.12)");
    gr.addColorStop(1, "rgba(255,226,200,0)");
    gg.fillStyle = gr;
    gg.fillRect(0, 0, 400, 400);
    this.glow = glow;
  }

  private ensureField(w: number, h: number, dpr: number) {
    if (this.field && this.field.w === w && this.field.h === h) return;
    const rnd = seeded(3);
    const c = makeCanvas(w * dpr, h * dpr), fg = ctx2d(c);
    const cool = starSprite("237,243,255", 32, [[0, 1], [0.1, 0.85], [0.28, 0.16], [1, 0]]);
    const warm = starSprite("255,244,222", 32, [[0, 1], [0.1, 0.85], [0.28, 0.16], [1, 0]]);
    fg.globalCompositeOperation = "lighter";
    const n = Math.round((w * h) / 630);
    for (let i = 0; i < n; i++) {
      const s = (0.75 + Math.pow(rnd(), 3) * 3.5) * dpr;
      fg.globalAlpha = 0.15 + rnd() * 0.5;
      fg.drawImage(rnd() > 0.8 ? warm : cool, rnd() * w * dpr - s / 2, rnd() * h * dpr - s / 2, s, s);
    }
    this.field = { w, h, c };
    this.twinkles = Array.from({ length: 26 }, () => ({ x: rnd() * w, y: rnd() * h, s: 5 + rnd() * 6, f: 0.2 + rnd() * 0.5, p: rnd() * 6.283 }));
  }

  draw(ctx: CanvasRenderingContext2D, w: number, h: number, dpr: number, t: number, state: EclipseState) {
    if (!this.sunA) this.setup();
    this.ensureField(w, h, dpr);
    const sunA = this.sunA!, sunB = this.sunB!, corona = this.corona!, sprite = this.sprite!, glow = this.glow!;
    const phase = Math.max(0, Math.min(1, state.progress));
    const reveal = phase * phase * (3 - 2 * phase);
    const since = state.beganAt != null ? t - state.beganAt : 99;
    const pulse = Math.exp(-since * 1.1);
    const cx = w / 2, cy = h * 0.5, R = Math.min(w, h) * 0.2;
    const breath = 0.5 - 0.5 * Math.cos((t * 6.2832) / 10);
    const dirx = 0.78, diry = -0.62;
    const off = reveal * 2.6 * R;
    const mx = cx + dirx * off, my = cy + diry * off;

    ctx.setTransform(1, 0, 0, 1, 0, 0);
    ctx.clearRect(0, 0, w * dpr, h * dpr);
    ctx.globalCompositeOperation = "lighter";
    ctx.globalAlpha = 1 - 0.5 * reveal;
    ctx.drawImage(this.field!.c, 0, 0);
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    for (const tw of this.twinkles) {
      ctx.globalAlpha = (1 - 0.6 * reveal) * (0.15 + 0.55 * Math.pow(0.5 + 0.5 * Math.sin(t * tw.f * 6.283 + tw.p), 3));
      ctx.drawImage(sprite, tw.x - tw.s / 2, tw.y - tw.s / 2, tw.s, tw.s);
    }

    ctx.globalAlpha = 0.28 + 0.12 * breath + 0.45 * reveal;
    const gs = R * (3.4 + 0.35 * breath);
    ctx.drawImage(glow, cx - gs, cy - gs, gs * 2, gs * 2);

    const coronaVis = (1 - 0.7 * Math.min(1, reveal * 2.2)) * (1 + 0.25 * pulse);
    ctx.save();
    ctx.translate(cx, cy);
    ctx.rotate(t * 0.004);
    const cs = R * 3.2 * (1 + 0.05 * breath + 0.03 * pulse);
    ctx.globalAlpha = Math.min(1, (0.9 + 0.1 * breath) * coronaVis);
    ctx.drawImage(corona, -cs, -cs, cs * 2, cs * 2);
    ctx.rotate(-t * 0.007);
    ctx.globalAlpha = Math.min(1, (0.6 + 0.2 * breath) * coronaVis);
    const ci = R * 3.2 * 0.985;
    ctx.drawImage(corona, -ci, -ci, ci * 2, ci * 2);
    ctx.restore();

    ctx.globalCompositeOperation = "source-over";
    if (reveal > 0.001) {
      ctx.save();
      ctx.translate(cx, cy);
      ctx.rotate(t * 0.009);
      ctx.globalAlpha = 1;
      ctx.drawImage(sunA, -R, -R, R * 2, R * 2);
      ctx.rotate(0.02 * Math.sin(t * 0.05));
      ctx.globalAlpha = 0.5 + 0.5 * Math.sin(t * 0.55);
      ctx.drawImage(sunB, -R, -R, R * 2, R * 2);
      ctx.restore();
    }
    let moonA = 1 - Math.max(0, Math.min(1, (reveal - 0.79) / 0.16));
    moonA = moonA * moonA * (3 - 2 * moonA);
    ctx.globalAlpha = moonA;
    ctx.fillStyle = "#07090c";
    ctx.beginPath();
    ctx.arc(mx, my, R * 1.006, 0, Math.PI * 2);
    ctx.fill();
    ctx.globalAlpha = 1;

    ctx.globalCompositeOperation = "lighter";
    const rimA = (0.35 + 0.15 * breath) * Math.max(0, 1 - reveal * 12);
    if (rimA > 0.01) {
      ctx.lineWidth = 1.1;
      ctx.strokeStyle = `rgba(255,150,160,${rimA.toFixed(3)})`;
      ctx.beginPath();
      ctx.arc(cx, cy, R * 1.006, 2.2, 4.4);
      ctx.stroke();
      ctx.strokeStyle = `rgba(255,190,190,${(rimA * 0.45).toFixed(3)})`;
      ctx.beginPath();
      ctx.arc(cx, cy, R * 1.006, 4.4, 8.48);
      ctx.stroke();
    }
    // Soft diamond-ring glint on Begin and at first contact (kept subtle).
    const ringI = Math.max(0.45 * pulse, Math.min(1, reveal / 0.012) * Math.max(0, 1 - reveal / 0.07));
    if (ringI > 0.01) {
      const gx = cx - dirx * R, gy = cy - diry * R;
      const g1 = 9 * ringI;
      ctx.globalAlpha = 0.75 * ringI;
      ctx.drawImage(sprite, gx - g1, gy - g1, g1 * 2, g1 * 2);
      ctx.globalAlpha = 0.3 * ringI;
      ctx.drawImage(sprite, gx - g1 * 3.5, gy - g1 * 3.5, g1 * 7, g1 * 7);
      ctx.globalAlpha = 1;
      const L = 26 * ringI;
      const lg = ctx.createLinearGradient(gx - L, gy, gx + L, gy);
      lg.addColorStop(0, "rgba(255,255,255,0)");
      lg.addColorStop(0.5, `rgba(255,255,255,${(0.28 * ringI).toFixed(3)})`);
      lg.addColorStop(1, "rgba(255,255,255,0)");
      ctx.fillStyle = lg;
      ctx.fillRect(gx - L, gy - 0.4, L * 2, 0.8);
    }
    if (reveal > 0.001) {
      ctx.globalAlpha = 0.22 * reveal;
      ctx.drawImage(glow, cx - R * 1.25, cy - R * 1.25, R * 2.5, R * 2.5);
    }
    ctx.globalAlpha = 1;
    ctx.globalCompositeOperation = "source-over";
  }
}
