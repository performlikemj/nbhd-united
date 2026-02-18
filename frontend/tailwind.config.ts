import type { Config } from "tailwindcss";
import typography from "@tailwindcss/typography";

const config: Config = {
  content: [
    "./app/**/*.{js,ts,jsx,tsx,mdx}",
    "./components/**/*.{js,ts,jsx,tsx,mdx}",
    "./lib/**/*.{js,ts,jsx,tsx,mdx}",
  ],
  theme: {
    extend: {
      colors: {
        ink: "var(--ink)",
        "ink-muted": "var(--ink-muted)",
        "ink-faint": "var(--ink-faint)",
        mist: "var(--mist)",
        bg: "var(--bg)",
        surface: "var(--surface)",
        "surface-elevated": "var(--surface-elevated)",
        "surface-hover": "var(--surface-hover)",
        card: "var(--card)",
        accent: "var(--accent)",
        "accent-hover": "var(--accent-hover)",
        signal: "var(--signal)",
        "signal-faint": "var(--signal-faint)",
        border: "var(--border)",
        "border-strong": "var(--border-strong)",
        "rose-bg": "var(--rose-bg)",
        "rose-border": "var(--rose-border)",
        "rose-text": "var(--rose-text)",
        "amber-bg": "var(--amber-bg)",
        "amber-border": "var(--amber-border)",
        "amber-text": "var(--amber-text)",
        "emerald-bg": "var(--emerald-bg)",
        "emerald-text": "var(--emerald-text)",
        overlay: "var(--overlay)",

        "status-emerald": "var(--status-emerald-bg)",
        "status-emerald-text": "var(--status-emerald-text)",
        "status-rose": "var(--status-rose-bg)",
        "status-rose-text": "var(--status-rose-text)",
        "status-amber": "var(--status-amber-bg)",
        "status-amber-text": "var(--status-amber-text)",
        "status-sky": "var(--status-sky-bg)",
        "status-sky-text": "var(--status-sky-text)",
        "status-slate": "var(--status-slate-bg)",
        "status-slate-text": "var(--status-slate-text)",
        "status-indigo": "var(--status-indigo-bg)",
        "status-indigo-text": "var(--status-indigo-text)",
        "status-violet": "var(--status-violet-bg)",
        "status-violet-text": "var(--status-violet-text)",
        "status-orange": "var(--status-orange-bg)",
        "status-orange-text": "var(--status-orange-text)",
      },
      boxShadow: {
        panel: "var(--shadow-panel)",
      },
      borderRadius: {
        panel: "1.25rem",
      },
      keyframes: {
        reveal: {
          "0%": { opacity: "0", transform: "translateY(14px)" },
          "100%": { opacity: "1", transform: "translateY(0)" },
        },
        pulseGrid: {
          "0%, 100%": { opacity: "0.55" },
          "50%": { opacity: "0.8" },
        },
      },
      animation: {
        reveal: "reveal 420ms ease-out both",
        pulseGrid: "pulseGrid 7s ease-in-out infinite",
      },
    },
  },
  plugins: [typography],
};

export default config;
