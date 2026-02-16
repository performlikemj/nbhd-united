"use client";

import { ReactNode } from "react";

export default function JournalLayout({ children }: { children: ReactNode }) {
  return (
    <div className="-mx-4 -mt-8 sm:-mx-6 lg:-mx-8">
      <div className="h-[calc(100dvh-4rem)]">
        {children}
      </div>
    </div>
  );
}
