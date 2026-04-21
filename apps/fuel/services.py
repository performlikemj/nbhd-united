"""Fuel business logic — est1RM calculation and progress aggregation."""

from __future__ import annotations


def est_1rm(weight: float, reps: int) -> float:
    """Epley formula: estimated one-rep max from weight and reps."""
    if not weight or reps < 1:
        return 0.0
    if reps == 1:
        return float(weight)
    return round(weight * (1 + reps / 30), 1)


def enrich_strength_detail(detail: dict) -> dict:
    """Add est_1rm to each set in a strength detail_json."""
    for exercise in detail.get("exercises", []):
        for s in exercise.get("sets", []):
            s["est_1rm"] = est_1rm(s.get("weight", 0), s.get("reps", 0))
    return detail


def aggregate_strength_progress(workouts) -> dict:
    """Build per-exercise est1RM trend data from strength workouts.

    Returns: {exercise_name: [{date, value}]} sorted oldest-first.
    """
    by_lift: dict[str, list[dict]] = {}
    for w in sorted(workouts, key=lambda w: w.date):
        for ex in (w.detail_json or {}).get("exercises", []):
            name = ex.get("name", "").strip()
            if not name:
                continue
            top = max(
                (est_1rm(s.get("weight", 0), s.get("reps", 0)) for s in ex.get("sets", [])),
                default=0,
            )
            by_lift.setdefault(name, []).append({"date": str(w.date), "value": top})
    return by_lift


def aggregate_cardio_progress(workouts) -> dict:
    """Build pace and distance trends from cardio workouts."""
    pace_points = []
    dist_points = []
    total_km = 0.0

    for w in sorted(workouts, key=lambda w: w.date):
        d = w.detail_json or {}
        if d.get("pace"):
            parts = str(d["pace"]).split(":")
            try:
                secs = int(parts[0]) * 60 + int(parts[1]) if len(parts) == 2 else int(parts[0]) * 60
                pace_points.append({"date": str(w.date), "value": secs})
            except (ValueError, IndexError):
                pass
        if d.get("distance_km"):
            km = float(d["distance_km"])
            total_km += km
            dist_points.append({"date": str(w.date), "value": km})

    return {"pace": pace_points, "distance": dist_points, "total_km": round(total_km, 1)}


def aggregate_hiit_progress(workouts) -> dict:
    """Build peak HR trend and totals from HIIT workouts."""
    hr_points = []
    total_minutes = 0

    for w in sorted(workouts, key=lambda w: w.date):
        d = w.detail_json or {}
        if d.get("peak_hr"):
            hr_points.append({"date": str(w.date), "value": d["peak_hr"]})
        total_minutes += w.duration_minutes or 0

    return {"peak_hr": hr_points, "session_count": len(workouts), "total_minutes": total_minutes}


def aggregate_calisthenics_progress(workouts) -> dict:
    """Build per-skill trend data from calisthenics workouts.

    Returns: {skill_name: {points: [{date, value}], is_hold: bool}}
    """
    by_skill: dict[str, dict] = {}

    for w in sorted(workouts, key=lambda w: w.date):
        for sk in (w.detail_json or {}).get("skills", []):
            name = sk.get("name", "").strip()
            if not name:
                continue
            sets = sk.get("sets", [])
            is_hold = bool(sets and sets[0].get("hold_s") is not None)
            top = max(
                (s.get("hold_s", 0) if is_hold else s.get("reps", 0) for s in sets),
                default=0,
            )
            entry = by_skill.setdefault(name, {"points": [], "is_hold": is_hold})
            entry["points"].append({"date": str(w.date), "value": top})

    return by_skill
