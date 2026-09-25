"use client";

import { useState } from "react";

import { SkyCanvas } from "@/components/open-sky/sky-canvas";
import { NorthStarRenderer } from "@/lib/sky-art/north-star";

/** Horizons header: the North Star sky with the page title over it. */
export function HorizonsSkyHeader({ title, subtitle }: { title: string; subtitle?: string }) {
  const [painter] = useState(() => new NorthStarRenderer());
  return (
    <header className="relative -mx-4 mb-8 overflow-hidden sm:-mx-6 md:mx-0">
      <SkyCanvas
        painter={painter}
        label="A sky of faint stars with one bright north star, above a mountain ridge at first light"
        className="os-band-fade block h-[260px] w-full sm:h-[300px]"
      />
      <div className="pointer-events-none absolute inset-x-0 top-0 px-4 pt-5 sm:px-6 md:px-8 md:pt-7">
        <h1 className="os-page-title">{title}</h1>
        {subtitle ? <p className="mt-2 text-[0.9375rem] text-os-muted">{subtitle}</p> : null}
      </div>
    </header>
  );
}
