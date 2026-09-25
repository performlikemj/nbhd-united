/**
 * Horizons: a sky of faint stars, ONE warm North Star that breathes (8 s), and
 * a mountain ridge at first light (port of the approved mockup HorizonsSkyA /
 * iOS NorthStarSky). Field + ridge are cached per size; per frame ~40 twinkles
 * and the star.
 */
import { ctx2d, hsh, makeCanvas, seeded, starSprite } from "./noise";

function fbm1(x: number, s: number, oct: number): number {
  let v = 0, a = 0.5, f = 1, n = 0;
  for (let i = 0; i < oct; i++) {
    const p = x * f, ix = Math.floor(p), fr = p - ix, u = fr * fr * (3 - 2 * fr);
    const a0 = hsh(ix, 0, s + i * 13), a1 = hsh(ix + 1, 0, s + i * 13);
    v += a * (a0 + (a1 - a0) * u);
    n += a;
    a *= 0.5;
    f *= 2.1;
  }
  return v / n;
}

export class NorthStarRenderer {
  private cache?: { w: number; h: number; dpr: number; field: HTMLCanvasElement; ridge: HTMLCanvasElement };
  private cool = typeof document !== "undefined" ? starSprite("237,243,255") : undefined;
  private warm = typeof document !== "undefined" ? starSprite("255,240,222") : undefined;
  private twinkles: { x: number; y: number; s: number; f: number; p: number }[] = [];

  private build(w: number, h: number, dpr: number) {
    const rnd = seeded(21);
    const field = makeCanvas(w * dpr, h * dpr), fg = ctx2d(field);
    const cool = starSprite("237,243,255"), warm = starSprite("255,236,214"), blue = starSprite("214,226,255");
    const n = Math.round((w * h) / 30);
    for (let i = 0; i < n; i++) {
      const x = rnd() * w * dpr, y = rnd() * h * 0.9 * dpr;
      const a = (0.1 + Math.pow(rnd(), 2) * 0.5) * (1 - (0.35 * y) / (h * dpr));
      const tint = rnd();
      fg.fillStyle = tint > 0.88 ? `rgba(255,232,210,${a})` : tint > 0.65 ? `rgba(210,224,255,${a})` : `rgba(238,242,255,${a})`;
      const side = (rnd() > 0.93 ? 1 : 0.5) * dpr;
      fg.fillRect(x, y, side, side);
    }
    fg.globalCompositeOperation = "lighter";
    const soft = Math.round((w * h) / 1500);
    for (let j = 0; j < soft; j++) {
      const sx = rnd() * w * dpr, sy = rnd() * h * 0.85 * dpr, sz = (2.5 + Math.pow(rnd(), 3) * 6) * dpr, tn = rnd();
      fg.globalAlpha = 0.18 + rnd() * 0.32;
      fg.drawImage(tn > 0.85 ? warm : tn > 0.6 ? blue : cool, sx - sz / 2, sy - sz / 2, sz, sz);
    }
    fg.globalAlpha = 1;

    const ridge = makeCanvas(w * dpr, h * dpr), rg = ctx2d(ridge);
    rg.scale(dpr, dpr);
    const dawn = rg.createLinearGradient(0, h * 0.5, 0, h * 0.95);
    dawn.addColorStop(0, "rgba(160,180,230,0)");
    dawn.addColorStop(0.5, "rgba(180,186,230,0.06)");
    dawn.addColorStop(0.82, "rgba(255,190,165,0.17)");
    dawn.addColorStop(1, "rgba(255,206,180,0.3)");
    rg.fillStyle = dawn;
    rg.fillRect(0, h * 0.5, w, h * 0.5);
    const line = (base: number, amp: number, seed: number, lift: (u: number) => number) => {
      const pts: [number, number][] = [];
      for (let x = 0; x <= w; x += 2) {
        const u = x / w;
        pts.push([x, base - amp * fbm1(u * 4.5 + seed, seed, 5) + lift(u)]);
      }
      return pts;
    };
    const far = line(h * 0.93, h * 0.11, 5, (u) => -h * 0.045 * Math.sin(u * 2.4 + 0.6));
    rg.fillStyle = "rgba(22,24,34,1)";
    rg.beginPath();
    rg.moveTo(0, h);
    for (const [x, y] of far) rg.lineTo(x, y);
    rg.lineTo(w, h);
    rg.closePath();
    rg.fill();
    const near = line(h * 1.0, h * 0.12, 11, (u) => h * 0.06 * Math.pow(Math.abs(u - 0.62) * 1.6, 1.4));
    rg.fillStyle = "#07090c";
    rg.beginPath();
    rg.moveTo(0, h);
    for (const [x, y] of near) rg.lineTo(x, y);
    rg.lineTo(w, h);
    rg.closePath();
    rg.fill();
    rg.strokeStyle = "rgba(255,214,190,0.14)";
    rg.lineWidth = 0.8;
    rg.beginPath();
    near.forEach(([x, y], i) => (i === 0 ? rg.moveTo(x, y) : rg.lineTo(x, y)));
    rg.stroke();

    this.twinkles = Array.from({ length: 40 }, () => ({ x: rnd() * w, y: rnd() * h * 0.75, s: 2 + rnd() * 2.5, f: 0.15 + rnd() * 0.35, p: rnd() * 6.283 }));
    this.cache = { w, h, dpr, field, ridge };
  }

  draw(ctx: CanvasRenderingContext2D, w: number, h: number, dpr: number, t: number) {
    if (!this.cache || this.cache.w !== w || this.cache.h !== h || this.cache.dpr !== dpr) this.build(w, h, dpr);
    const { field, ridge } = this.cache!;
    const cool = this.cool!, warm = this.warm!;
    ctx.setTransform(1, 0, 0, 1, 0, 0);
    ctx.globalCompositeOperation = "source-over";
    ctx.globalAlpha = 1;
    ctx.fillStyle = "#07090c";
    ctx.fillRect(0, 0, w * dpr, h * dpr);
    const dx = 4 * Math.sin(t / 70) * dpr, dy = 3 * Math.cos(t / 90) * dpr;
    ctx.globalCompositeOperation = "lighter";
    ctx.drawImage(field, dx, dy);
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    for (const tw of this.twinkles) {
      ctx.globalAlpha = 0.08 + 0.4 * Math.pow(0.5 + 0.5 * Math.sin(t * tw.f * 6.283 + tw.p), 4);
      const s = tw.s * 2;
      ctx.drawImage(cool, tw.x - s / 2, tw.y - s / 2, s, s);
    }
    const PX = w * 0.61, PY = h * 0.36;
    const pulse = 0.5 + 0.5 * Math.sin((t * 6.2832) / 8);
    ctx.globalAlpha = 0.16 + 0.08 * pulse;
    ctx.drawImage(warm, PX - 70, PY - 70, 140, 140);
    ctx.globalAlpha = 0.5;
    ctx.drawImage(warm, PX - 26, PY - 26, 52, 52);
    ctx.globalAlpha = 1;
    ctx.drawImage(warm, PX - 12, PY - 12, 24, 24);
    ctx.drawImage(cool, PX - 6, PY - 6, 12, 12);
    const L = 22, sa = (0.18 + 0.06 * pulse).toFixed(3);
    const gh = ctx.createLinearGradient(PX - L, 0, PX + L, 0);
    gh.addColorStop(0, "rgba(255,248,236,0)");
    gh.addColorStop(0.5, `rgba(255,248,236,${sa})`);
    gh.addColorStop(1, "rgba(255,248,236,0)");
    ctx.fillStyle = gh;
    ctx.fillRect(PX - L, PY - 0.35, L * 2, 0.7);
    const gv = ctx.createLinearGradient(0, PY - L, 0, PY + L);
    gv.addColorStop(0, "rgba(255,248,236,0)");
    gv.addColorStop(0.5, `rgba(255,248,236,${sa})`);
    gv.addColorStop(1, "rgba(255,248,236,0)");
    ctx.fillStyle = gv;
    ctx.fillRect(PX - 0.35, PY - L, 0.7, L * 2);
    ctx.setTransform(1, 0, 0, 1, 0, 0);
    ctx.globalCompositeOperation = "source-over";
    ctx.drawImage(ridge, 0, 0);
    ctx.globalAlpha = 1;
  }
}
