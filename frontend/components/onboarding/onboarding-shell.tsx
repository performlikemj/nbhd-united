"use client";

import { ReactNode } from "react";
import { Starfield } from "@/components/landing/starfield";

export function OnboardingShell({ children }: { children: ReactNode }) {
  return (
    <div className="nebula-bg relative flex min-h-[100dvh] flex-col items-center overflow-hidden">
      <Starfield className="opacity-60" />
      <div className="relative z-10 flex w-full flex-1 flex-col items-center justify-center px-4 py-16 sm:px-8">
        {children}
      </div>
    </div>
  );
}
