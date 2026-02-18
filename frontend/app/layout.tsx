import type { Metadata } from "next";

import { AppShell } from "@/components/app-shell";
import { ThemeProvider } from "@/components/theme-provider";
import { Providers } from "@/app/providers";

import "./globals.css";

export const metadata: Metadata = {
  title: "NBHD United Console",
  description: "Subscriber onboarding and operations dashboard for NBHD United.",
};

export default function RootLayout({
  children,
}: Readonly<{ children: React.ReactNode }>) {
  return (
    <html lang="en" className="h-full">
      <body className="overflow-x-hidden bg-bg">
        <ThemeProvider>
          <Providers>
            <AppShell>{children}</AppShell>
          </Providers>
        </ThemeProvider>
      </body>
    </html>
  );
}
