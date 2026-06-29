"use client";

import { useCallback } from "react";

import { useFuelProfileQuery, useUpdateFuelProfileMutation } from "@/lib/queries";
import type { DistanceUnit } from "@/lib/types";

const KM_TO_MI = 0.621371;
const MI_TO_KM = 1 / KM_TO_MI;
const M_TO_FT = 3.28084;
const FT_TO_M = 1 / M_TO_FT;

/**
 * Reads the user's preferred distance unit from `FuelProfile.distance_unit`.
 *
 * Storage is always canonical (km for distance, m for elevation) — the
 * conversion helpers below run only at the display boundary. Defaults to
 * "km" while the profile is loading or for users without Fuel enabled.
 */
export function useDistanceUnit() {
  const { data: profile } = useFuelProfileQuery();
  const updateMutation = useUpdateFuelProfileMutation();

  const unit: DistanceUnit = profile?.distance_unit ?? "km";

  const setUnit = useCallback(
    (next: DistanceUnit) => {
      updateMutation.mutate({ distance_unit: next });
    },
    [updateMutation],
  );

  return { unit, setUnit, isPending: updateMutation.isPending };
}

/** Convert a stored km value into the user's preferred display unit. */
export function kmToDisplay(km: number, unit: DistanceUnit): number {
  return unit === "mi" ? Math.round(km * KM_TO_MI * 100) / 100 : Math.round(km * 100) / 100;
}

/** Convert a display-unit value back into km for storage. */
export function displayToKm(value: number, unit: DistanceUnit): number {
  return unit === "mi" ? Math.round(value * MI_TO_KM * 1000) / 1000 : value;
}

/** Convert a stored elevation (meters) into the display unit (feet for "mi"). */
export function metersToDisplay(m: number, unit: DistanceUnit): number {
  return unit === "mi" ? Math.round(m * M_TO_FT) : Math.round(m);
}

/** Convert a display-unit elevation value back into meters for storage. */
export function displayToMeters(value: number, unit: DistanceUnit): number {
  return unit === "mi" ? Math.round(value * FT_TO_M) : value;
}

/** Display label for the elevation unit, paired with the distance unit. */
export function elevationLabel(unit: DistanceUnit): string {
  return unit === "mi" ? "ft" : "m";
}

/** Parse an "M:SS" pace string into total seconds, or null if not MM:SS shape. */
export function parsePaceSeconds(mmss: string | null | undefined): number | null {
  if (!mmss) return null;
  const m = mmss.trim().match(/^(\d{1,2}):(\d{2})$/);
  if (!m) return null;
  const secs = parseInt(m[2], 10);
  if (secs >= 60) return null;
  return parseInt(m[1], 10) * 60 + secs;
}

function formatPaceSeconds(total: number): string {
  const s = Math.max(0, Math.round(total));
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
}

/**
 * Convert a canonical per-KILOMETER "M:SS" pace into the display unit's "M:SS".
 *
 * Pace is time-per-distance, so km→mi MULTIPLIES by km-per-mile (covering a mile
 * takes ~1.609× the time of a km) — the INVERSE of the distance conversion. The
 * bug this guards against was relabeling "/km"→"/mi" without scaling the value
 * (a 7:08/km run displayed as "7:08 /mi"). Anything that isn't MM:SS (descriptive
 * "tempo", in-progress input) passes through untouched. Returns null when absent.
 */
export function paceToDisplay(canonicalPerKm: string | null | undefined, unit: DistanceUnit): string | null {
  if (canonicalPerKm == null || canonicalPerKm === "") return null;
  const secs = parsePaceSeconds(canonicalPerKm);
  if (secs == null) return canonicalPerKm;
  return unit === "mi" ? formatPaceSeconds(secs * MI_TO_KM) : formatPaceSeconds(secs);
}

/** Convert a display-unit "M:SS" pace back into canonical per-km "M:SS" for storage. */
export function displayToPace(shown: string, unit: DistanceUnit): string {
  const secs = parsePaceSeconds(shown);
  if (secs == null) return shown.trim();
  return unit === "mi" ? formatPaceSeconds(secs / MI_TO_KM) : formatPaceSeconds(secs);
}
