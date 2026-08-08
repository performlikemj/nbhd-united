"""AAD coordinates + ladder group for the encrypted fuel columns (free-text set).

Encryption-at-rest Phase 3 (plan §1.3, §3). Same contract as
``apps.journal.enc_columns`` — byte-stable AAD, never hand-typed. Fuel is its OWN
flag pair (``Tenant.encrypt_fuel_writes`` / ``read_encrypted_fuel``, plan §3.1) —
an independent surface (``fuel/envelope.py``, ``fuel/runtime_views.py``) with its
own rollback lever; the completeness predicate enumerates ``FUEL_ENC_COLUMNS``.

Only the FREE-TEXT set is in scope. No value predicate exists on any of these
columns today (grep clean); SQL aggregation touches only ``duration_minutes``, a
non-body-metric integer that stays plaintext (plan §1.3).

EXCLUDED from Phase 3 by design (do NOT add here without a plan/MJ update):
  * Numeric body-metrics — ``RestingHeartRateLog.bpm``, ``SleepLog.duration_hours``
    /``quality``, ``BodyWeightLog.weight_kg``, ``Workout.rpe``/``duration_seconds``:
    need a numeric codec + per-reader aggregation audit; DEFER to Phase 3b
    (MJ decision 2026-07-14).
  * ``WorkoutPlan.schedule_json`` / ``week_overrides`` — still OUT of this
    encryption ladder pending an explicit plan update. Their live normalized
    shape does carry activity/detail strings, so P3 W3b protects them through
    the placeholder-at-rest registry even while the encryption sidecars remain
    deferred.
  * ``FuelProfile.goals`` / ``equipment`` / ``preferred_time`` / ``fitness_level`` —
    structured prefs / enums (tier S), OUT.
  * ``PersonalRecord.exercise_name`` / ``FuelGoal.exercise_name`` — low-PII
    structured names, verdict DEFER (plan §1.3).
"""

from __future__ import annotations

from apps.crypto.enc_columns import EncColumn

# ── AAD 2-tuples (table, logical column) ─────────────────────────────────────
# fuel_workouts
WORKOUT_NOTES: tuple[str, str] = ("fuel_workouts", "notes")
WORKOUT_NOTES_THREAD: tuple[str, str] = ("fuel_workouts", "notes_thread")
WORKOUT_DETAIL_JSON: tuple[str, str] = ("fuel_workouts", "detail_json")
WORKOUT_SKIP_REASON: tuple[str, str] = ("fuel_workouts", "skip_reason")
WORKOUT_ACTIVITY: tuple[str, str] = ("fuel_workouts", "activity")
# fuel_workout_plans
WORKOUT_PLAN_NOTES: tuple[str, str] = ("fuel_workout_plans", "notes")
WORKOUT_PLAN_OBJECTIVE: tuple[str, str] = ("fuel_workout_plans", "objective")
WORKOUT_PLAN_NAME: tuple[str, str] = ("fuel_workout_plans", "name")
# fuel_profiles
FUEL_PROFILE_ADDITIONAL_CONTEXT: tuple[str, str] = ("fuel_profiles", "additional_context")
FUEL_PROFILE_LIMITATIONS: tuple[str, str] = ("fuel_profiles", "limitations")
# fuel_workout_templates
WORKOUT_TEMPLATE_NAME: tuple[str, str] = ("fuel_workout_templates", "name")
WORKOUT_TEMPLATE_DETAIL_JSON: tuple[str, str] = ("fuel_workout_templates", "detail_json")
# fuel_sleep
SLEEP_LOG_NOTES: tuple[str, str] = ("fuel_sleep", "notes")

# ── Ladder group — every model here carries a direct ``tenant`` FK. ──────────
FUEL_ENC_COLUMNS: tuple[EncColumn, ...] = (
    EncColumn("fuel.Workout", "notes", "notes_enc", "fuel_workouts"),
    EncColumn("fuel.Workout", "notes_thread", "notes_thread_enc", "fuel_workouts", is_json=True),
    EncColumn("fuel.Workout", "detail_json", "detail_json_enc", "fuel_workouts", is_json=True),
    EncColumn("fuel.Workout", "skip_reason", "skip_reason_enc", "fuel_workouts"),
    EncColumn("fuel.Workout", "activity", "activity_enc", "fuel_workouts"),
    EncColumn("fuel.WorkoutPlan", "notes", "notes_enc", "fuel_workout_plans"),
    EncColumn("fuel.WorkoutPlan", "objective", "objective_enc", "fuel_workout_plans"),
    EncColumn("fuel.WorkoutPlan", "name", "name_enc", "fuel_workout_plans"),
    EncColumn("fuel.FuelProfile", "additional_context", "additional_context_enc", "fuel_profiles"),
    EncColumn("fuel.FuelProfile", "limitations", "limitations_enc", "fuel_profiles", is_json=True),
    EncColumn("fuel.WorkoutTemplate", "name", "name_enc", "fuel_workout_templates"),
    EncColumn("fuel.WorkoutTemplate", "detail_json", "detail_json_enc", "fuel_workout_templates", is_json=True),
    EncColumn("fuel.SleepLog", "notes", "notes_enc", "fuel_sleep"),
)
