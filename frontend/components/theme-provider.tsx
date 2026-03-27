"use client";

import { createContext, useContext, useEffect, useMemo, ReactNode } from "react";

type Theme = "dark";

const ThemeContext = createContext<{
  theme: Theme;
  setTheme: (t: Theme) => void;
  toggleTheme: () => void;
}>({
  theme: "dark",
  setTheme: () => {},
  toggleTheme: () => {},
});

export function ThemeProvider({ children }: { children: ReactNode }) {
  useEffect(() => {
    document.documentElement.setAttribute("data-theme", "dark");
    document.documentElement.style.colorScheme = "dark";
    const meta = document.querySelector("meta[name=\"theme-color\"]");
    if (meta) meta.setAttribute("content", "#0b0f13");
  }, []);

  const api = useMemo(() => ({
    theme: "dark" as Theme,
    setTheme: () => {},
    toggleTheme: () => {},
  }), []);

  return <ThemeContext.Provider value={api}>{children}</ThemeContext.Provider>;
}

export function useTheme() {
  return useContext(ThemeContext);
}
