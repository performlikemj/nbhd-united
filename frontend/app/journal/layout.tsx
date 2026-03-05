"use client";

import { ReactNode } from "react";

export default function JournalLayout({ children }: { children: ReactNode }) {
  return (
    <div className="-mx-4 -mt-8 -mb-8 sm:-mx-6 flex-1 min-h-0 overflow-hidden">
      {children}
    </div>
  );
}
