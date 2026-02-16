"use client";

import { ReactNode } from "react";

export default function JournalLayout({ children }: { children: ReactNode }) {
  return (
    <div className="-mx-4 -mt-4 sm:-mx-6 sm:-mt-6 lg:-mx-8 lg:-mt-8">
      <div className="h-[calc(100vh-4rem)]">
        {children}
      </div>
    </div>
  );
}
