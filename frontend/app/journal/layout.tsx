"use client";

import { ReactNode } from "react";

export default function JournalLayout({ children }: { children: ReactNode }) {
  return (
    // data-os-journal: Open Sky cancels the negative margins (they offset the
    // legacy shell's padding) and gives the explorer a viewport-bound height.
    <div data-os-journal className="flex-1 min-h-0 h-full overflow-hidden -mx-4 sm:-mx-6 -mt-4 sm:-mt-6 -mb-4 sm:-mb-6">
      {children}
    </div>
  );
}
