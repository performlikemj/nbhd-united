"use client";

import { ReactNode } from "react";

export default function JournalLayout({ children }: { children: ReactNode }) {
  return (
    <div className="flex-1 min-h-0 h-full overflow-hidden -mx-4 sm:-mx-6 -mt-4 sm:-mt-6 -mb-4 sm:-mb-6">
      {children}
    </div>
  );
}
