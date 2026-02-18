"use client";

import { createContext, useContext, useEffect, useMemo, useState, ReactNode } from "react";

type Theme = "light" | "dark";

const ThemeContext = createContext<{
  theme: Theme;
  setTheme: (t: Theme) => void;
  toggleTheme: () => void;
}>({
  theme: "light",
  setTheme: () => {},
  toggleTheme: () => {},
});

function getSystemTheme(): Theme {
  if (typeof window === "undefined") return "light";
  return window.matchMedia?.("(prefers-color-scheme: dark)").matches ? "dark" : "light";
}

function getInitialTheme(): Theme {
  if (typeof window === "undefined") return "light";
  const stored = localStorage.getItem("theme");
  if (stored === "light" || stored === "dark") return stored;
  return getSystemTheme();
}

export function ThemeProvider({ children }: { children: ReactNode }) {
  const [theme, setThemeState] = useState<Theme>(getInitialTheme);

  useEffect(() => {
    document.documentElement.setAttribute("data-theme", theme);
    document.documentElement.style.colorScheme = theme;
    localStorage.setItem("theme", theme);
    const meta = document.querySelector("meta[name=\"theme-color\"]");
    if (meta) meta.setAttribute("content", theme === "dark" ? "#0b0f13" : "#f8f6ef");
  }, [theme]);

  useEffect(() => {
    const stored = localStorage.getItem("theme");
    if (stored === "light" || stored === "dark") return;

    const mql = window.matchMedia("(prefers-color-scheme: dark)");
    const handler = () => setThemeState(getSystemTheme());
    mql.addEventListener("change", handler);
    return () => mql.removeEventListener("change", handler);
  }, []);

  const api = useMemo(() => ({
    theme,
    setTheme: (t: Theme) => setThemeState(t),
    toggleTheme: () => setThemeState((prev) => (prev === "dark" ? "light" : "dark")),
  }), [theme]);

  return <ThemeContext.Provider value={api}>{children}</ThemeContext.Provider>;
}

export function useTheme() {
  return useContext(ThemeContext);
}
