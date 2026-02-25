import type { Metadata } from "next";
import { DM_Serif_Display, Plus_Jakarta_Sans } from "next/font/google";

import { AppShell } from "@/components/app-shell";
import { ThemeProvider } from "@/components/theme-provider";
import { Providers } from "@/app/providers";

import "./globals.css";

const plusJakarta = Plus_Jakarta_Sans({
  subsets: ["latin"],
  variable: "--font-body",
  display: "swap",
  weight: ["400", "500", "600", "700"],
});

const dmSerif = DM_Serif_Display({
  subsets: ["latin"],
  variable: "--font-display",
  display: "swap",
  weight: "400",
});

export const metadata: Metadata = {
  title: "Neighborhood United",
  description: "Your AI-powered personal assistant.",
};

export default function RootLayout({
  children,
}: Readonly<{ children: React.ReactNode }>) {
  return (
    <html lang="en" className={`h-full ${plusJakarta.variable} ${dmSerif.variable}`}>
      <head>
        <meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover" />
      </head>
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
