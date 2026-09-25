/** Shared deterministic noise + canvas helpers for the Open Sky art (ported from the iOS/canvas mockups). */

export function hsh(x: number, y: number, s: number): number {
  let h = (Math.imul(x, 374761393) + Math.imul(y, 668265263) + Math.imul(s, 1442695041)) | 0;
  h = Math.imul(h ^ (h >>> 13), 1274126177);
  h = h ^ (h >>> 16);
  return (h >>> 0) / 4294967296;
}

export function vnoise(x: number, y: number, s: number): number {
  const ix = Math.floor(x), iy = Math.floor(y), fx = x - ix, fy = y - iy;
  const ux = fx * fx * (3 - 2 * fx), uy = fy * fy * (3 - 2 * fy);
  const a = hsh(ix, iy, s), b = hsh(ix + 1, iy, s), c = hsh(ix, iy + 1, s), d = hsh(ix + 1, iy + 1, s);
  return a + (b - a) * ux + (c - a) * uy + (a - b - c + d) * ux * uy;
}

export function fbm(x: number, y: number, s: number, oct: number): number {
  let v = 0, amp = 0.5, f = 1, norm = 0;
  for (let i = 0; i < oct; i++) {
    v += amp * vnoise(x * f, y * f, s + i * 17);
    norm += amp;
    amp *= 0.5;
    f *= 2.03;
  }
  return v / norm;
}

export function worley(x: number, y: number, s: number): [number, number] {
  const ix = Math.floor(x), iy = Math.floor(y);
  let f1 = 9, f2 = 9;
  for (let j = -1; j <= 1; j++) {
    for (let i = -1; i <= 1; i++) {
      const cx = ix + i, cy = iy + j;
      const px = cx + hsh(cx, cy, s), py = cy + hsh(cx, cy, s + 91);
      const dx = px - x, dy = py - y, d = Math.sqrt(dx * dx + dy * dy);
      if (d < f1) {
        f2 = f1;
        f1 = d;
      } else if (d < f2) {
        f2 = d;
      }
    }
  }
  return [f1, f2];
}

export function smooth(a: number, b: number, x: number): number {
  const t = Math.max(0, Math.min(1, (x - a) / (b - a)));
  return t * t * (3 - 2 * t);
}

export function clamp255(v: number): number {
  return v < 0 ? 0 : v > 255 ? 255 : v;
}

export function makeCanvas(w: number, h: number): HTMLCanvasElement {
  const c = document.createElement("canvas");
  c.width = Math.max(1, Math.round(w));
  c.height = Math.max(1, Math.round(h));
  return c;
}

export function ctx2d(c: HTMLCanvasElement): CanvasRenderingContext2D {
  const g = c.getContext("2d");
  if (!g) throw new Error("2D canvas unavailable");
  return g;
}

/** Seeded Park–Miller PRNG. */
export function seeded(seed: number): () => number {
  let s = seed;
  return () => {
    s = (s * 16807) % 2147483647;
    return (s - 1) / 2147483646;
  };
}

/** Soft point-of-light sprite (a PSF: bright core, faint halo). */
export function starSprite(rgb: string, size = 48, stops: [number, number][] = [[0, 1], [0.1, 0.9], [0.22, 0.16], [0.5, 0.035], [1, 0]]): HTMLCanvasElement {
  const c = makeCanvas(size, size);
  const g = ctx2d(c);
  const r = size / 2;
  const gr = g.createRadialGradient(r, r, 0, r, r, r);
  for (const [at, a] of stops) gr.addColorStop(at, `rgba(${rgb},${a})`);
  g.fillStyle = gr;
  g.fillRect(0, 0, size, size);
  return c;
}
