import type { Config } from "tailwindcss";

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
        mist: "var(--mist)",
        card: "var(--card)",
        accent: "var(--accent)",
        signal: "var(--signal)",
      },
      boxShadow: {
        panel: "0 20px 55px rgba(18, 31, 38, 0.14)",
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
  plugins: [],
};

export default config;
