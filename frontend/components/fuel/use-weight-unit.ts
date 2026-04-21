"use client";

import { useCallback, useSyncExternalStore } from "react";

export type WeightUnit = "kg" | "lbs";

const KG_TO_LBS = 2.20462;
const LBS_TO_KG = 1 / KG_TO_LBS;
const STORAGE_KEY = "fuel:weight-unit";

function getSnapshot(): WeightUnit {
  if (typeof window === "undefined") return "kg";
  return (localStorage.getItem(STORAGE_KEY) as WeightUnit) || "kg";
}

function subscribe(callback: () => void) {
  const handler = (e: StorageEvent) => {
    if (e.key === STORAGE_KEY) callback();
  };
  window.addEventListener("storage", handler);
  // Also listen for same-tab changes via custom event
  window.addEventListener("fuel-unit-change", callback);
  return () => {
    window.removeEventListener("storage", handler);
    window.removeEventListener("fuel-unit-change", callback);
  };
}

export function useWeightUnit() {
  const unit = useSyncExternalStore(subscribe, getSnapshot, () => "kg" as WeightUnit);

  const setUnit = useCallback((u: WeightUnit) => {
    localStorage.setItem(STORAGE_KEY, u);
    window.dispatchEvent(new Event("fuel-unit-change"));
  }, []);

  return { unit, setUnit };
}

/** Convert kg value to display value in the current unit. */
export function kgToDisplay(kg: number, unit: WeightUnit): number {
  return unit === "lbs" ? Math.round(kg * KG_TO_LBS * 10) / 10 : Math.round(kg * 10) / 10;
}

/** Convert display value back to kg for storage. */
export function displayToKg(value: number, unit: WeightUnit): number {
  return unit === "lbs" ? Math.round(value * LBS_TO_KG * 100) / 100 : value;
}
