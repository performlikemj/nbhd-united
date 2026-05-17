"use client";

import { useReportWebVitals } from "next/web-vitals";

/**
 * Logs Core Web Vitals (LCP, FID/INP, CLS, FCP, TTFB) to the console.
 * Open DevTools console and look for `[web-vitals]` lines to see the
 * actual metrics for the current page. Used as the Phase 1 baseline
 * measurement before we ship any caching changes.
 */
export function WebVitals() {
  useReportWebVitals((metric) => {
    if (typeof window === "undefined") return;
    const { name, value, rating, id } = metric;
    const rounded = name === "CLS" ? value.toFixed(3) : Math.round(value);
    console.log(`[web-vitals] ${name}=${rounded} rating=${rating} id=${id}`);
  });
  return null;
}
