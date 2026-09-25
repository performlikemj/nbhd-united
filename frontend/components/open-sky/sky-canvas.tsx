"use client";

import { useEffect, useRef } from "react";

export interface SkyPainter {
  draw(ctx: CanvasRenderingContext2D, w: number, h: number, dpr: number, t: number): void;
}

/**
 * Runs an Open Sky painter on a canvas sized to its box. requestAnimationFrame
 * while visible; one static frame under prefers-reduced-motion; paused while the
 * tab is hidden. The art is decorative — pass `label` for a single img role.
 */
export function SkyCanvas({
  painter,
  label,
  className,
  staticTime = 4,
  redrawKey,
}: {
  painter: SkyPainter;
  label: string;
  className?: string;
  staticTime?: number;
  /** Change this when the scene changes, so a reduced-motion static frame repaints. */
  redrawKey?: string | number;
}) {
  const ref = useRef<HTMLCanvasElement>(null);
  const repaintStatic = useRef<() => void>(() => {});
  const painterRef = useRef(painter);
  useEffect(() => {
    painterRef.current = painter;
  }, [painter]);

  useEffect(() => {
    const canvas = ref.current;
    if (!canvas) return;
    const ctx = canvas.getContext("2d");
    if (!ctx) return;
    const reduce = window.matchMedia("(prefers-reduced-motion: reduce)");
    let raf = 0;
    let size = { w: 0, h: 0, dpr: 1 };

    const resize = () => {
      const rect = canvas.getBoundingClientRect();
      const dpr = Math.min(2, window.devicePixelRatio || 1);
      const w = Math.max(1, Math.round(rect.width));
      const h = Math.max(1, Math.round(rect.height));
      if (w !== size.w || h !== size.h || dpr !== size.dpr) {
        size = { w, h, dpr };
        canvas.width = w * dpr;
        canvas.height = h * dpr;
      }
    };
    const paint = (t: number) => {
      resize();
      painterRef.current.draw(ctx, size.w, size.h, size.dpr, t);
    };
    const loop = (ms: number) => {
      paint(ms / 1000);
      raf = requestAnimationFrame(loop);
    };
    const start = () => {
      cancelAnimationFrame(raf);
      if (reduce.matches) {
        paint(staticTime);
      } else if (document.visibilityState === "visible") {
        raf = requestAnimationFrame(loop);
      }
    };
    repaintStatic.current = () => {
      if (reduce.matches) paint(staticTime);
    };
    const ro = new ResizeObserver(() => {
      if (reduce.matches) paint(staticTime);
    });
    ro.observe(canvas);
    document.addEventListener("visibilitychange", start);
    reduce.addEventListener("change", start);
    start();
    return () => {
      cancelAnimationFrame(raf);
      ro.disconnect();
      document.removeEventListener("visibilitychange", start);
      reduce.removeEventListener("change", start);
    };
  }, [staticTime]);

  useEffect(() => {
    repaintStatic.current();
  }, [redrawKey]);

  return <canvas ref={ref} role="img" aria-label={label} className={className} />;
}
