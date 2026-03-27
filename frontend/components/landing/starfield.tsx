"use client";

import { useEffect, useRef } from "react";

type Star = {
  x: number;
  y: number;
  r: number;
  opacity: number;
  speed: number;
  color: string;
};

const COLORS = [
  "255,255,255",
  "124,107,240",
  "78,205,196",
  "232,180,184",
];

export function Starfield({ className = "" }: { className?: string }) {
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const starsRef = useRef<Star[]>([]);
  const rafRef = useRef<number>(0);

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;

    const prefersReduced = window.matchMedia(
      "(prefers-reduced-motion: reduce)"
    ).matches;

    const ctx = canvas.getContext("2d");
    if (!ctx) return;

    function resize() {
      if (!canvas) return;
      canvas.width = canvas.offsetWidth * window.devicePixelRatio;
      canvas.height = canvas.offsetHeight * window.devicePixelRatio;
      ctx!.scale(window.devicePixelRatio, window.devicePixelRatio);
    }

    function initStars() {
      if (!canvas) return;
      const count = Math.floor(
        (canvas.offsetWidth * canvas.offsetHeight) / 4000
      );
      starsRef.current = Array.from({ length: count }, () => ({
        x: Math.random() * canvas.offsetWidth,
        y: Math.random() * canvas.offsetHeight,
        r: Math.random() * 1.5 + 0.3,
        opacity: Math.random() * 0.6 + 0.1,
        speed: Math.random() * 0.002 + 0.001,
        color: COLORS[Math.floor(Math.random() * COLORS.length)],
      }));
    }

    resize();
    initStars();

    let time = 0;
    function draw() {
      if (!canvas || !ctx) return;
      ctx.clearRect(0, 0, canvas.offsetWidth, canvas.offsetHeight);
      time += 0.016;

      for (const star of starsRef.current) {
        const flicker = prefersReduced
          ? star.opacity
          : star.opacity +
            Math.sin(time * star.speed * 600) * 0.3 * star.opacity;
        ctx.beginPath();
        ctx.arc(star.x, star.y, star.r, 0, Math.PI * 2);
        ctx.fillStyle = `rgba(${star.color},${Math.max(0, Math.min(1, flicker))})`;
        ctx.fill();
      }

      rafRef.current = requestAnimationFrame(draw);
    }

    if (prefersReduced) {
      draw();
    } else {
      rafRef.current = requestAnimationFrame(draw);
    }

    const onResize = () => {
      resize();
      initStars();
    };
    window.addEventListener("resize", onResize);

    return () => {
      cancelAnimationFrame(rafRef.current);
      window.removeEventListener("resize", onResize);
    };
  }, []);

  return (
    <canvas
      ref={canvasRef}
      className={`pointer-events-none absolute inset-0 h-full w-full ${className}`}
      aria-hidden="true"
    />
  );
}
