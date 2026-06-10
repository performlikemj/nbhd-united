"""Internal runtime views for the OpenClaw fuel plugin."""

from __future__ import annotations

import logging
from datetime import UTC, date
from decimal import Decimal, InvalidOperation
from uuid import UUID

from django.db import models as db_models
from rest_framework import status
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.common.llm_contracts import resolve_relative_date, today_in_tenant_tz
from apps.integrations.internal_auth import InternalAuthError, validate_internal_runtime_request
from apps.tenants.middleware import set_rls_context
from apps.tenants.models import Tenant

from .models import (
    BodyWeightLog,
    FuelProfile,
    OnboardingStatus,
    PlanStatus,
    Workout,
    WorkoutCategory,
    WorkoutPlan,
    WorkoutStatus,
)

logger = logging.getLogger(__name__)

_PROFILE_FIELDS = (
    "onboarding_status",
    "fitness_level",
    "goals",
    "limitations",
    "equipment",
    "days_per_week",
    "preferred_days",
    "preferred_time",
    "additional_context",
)


def _serialize_profile(profile: FuelProfile) -> dict:
    return {f: getattr(profile, f) for f in _PROFILE_FIELDS}


def _edit_locked_response(workout: Workout) -> Response | None:
    """Return a 409 response if the workout is user-edit-locked, else None.

    OpenClaw's runtime documents 429 + Retry-After as retry-able; 409 is
    undocumented, so we include Retry-After on the 409 too plus a
    structured body so any reasonable assistant runtime can interpret
    the conflict instead of treating it as terminal.
    """
    from django.utils import timezone

    if workout.edit_lock_until is None:
        return None
    now = timezone.now()
    if workout.edit_lock_until <= now:
        return None
    retry_after_s = max(1, int((workout.edit_lock_until - now).total_seconds()) + 1)
    resp = Response(
        {
            "error": "edit_locked",
            "lock_owner": workout.edit_lock_owner or "user",
            "retry_after_s": retry_after_s,
            "edit_lock_until": workout.edit_lock_until.isoformat(),
            "workout_id": str(workout.id),
        },
        status=status.HTTP_409_CONFLICT,
    )
    resp["Retry-After"] = str(retry_after_s)
    return resp


def _internal_auth_or_401(request, tenant_id: UUID) -> Response | None:
    try:
        validate_internal_runtime_request(
            provided_key=request.headers.get("X-NBHD-Internal-Key", ""),
            provided_tenant_id=request.headers.get("X-NBHD-Tenant-Id", ""),
            expected_tenant_id=str(tenant_id),
        )
    except InternalAuthError as exc:
        return Response(
            {"error": "internal_auth_failed", "detail": str(exc)},
            status=status.HTTP_401_UNAUTHORIZED,
        )
    set_rls_context(tenant_id=tenant_id, service_role=True)
    return None


def _get_tenant_or_404(tenant_id: UUID) -> Tenant | Response:
    try:
        return Tenant.objects.get(id=tenant_id)
    except Tenant.DoesNotExist:
        return Response(
            {"error": "tenant_not_found"},
            status=status.HTTP_404_NOT_FOUND,
        )


class RuntimeLogWorkoutView(APIView):
    """POST: log a workout from the AI assistant."""

    permission_classes = [AllowAny]

    def post(self, request, tenant_id):
        err = _internal_auth_or_401(request, tenant_id)
        if err:
            return err
        tenant_or_resp = _get_tenant_or_404(tenant_id)
        if isinstance(tenant_or_resp, Response):
            return tenant_or_resp
        tenant = tenant_or_resp

        data = request.data
        category = data.get("category", "other")
        if category not in WorkoutCategory.values:
            category = "other"

        workout_status = data.get("status", "done")
        if workout_status not in WorkoutStatus.values:
            workout_status = "done"

        # Coerce duration_minutes and rpe to int, tolerating non-numeric input
        duration = data.get("duration_minutes")
        if duration is not None:
            try:
                duration = int(duration)
            except (TypeError, ValueError):
                duration = None

        rpe = data.get("rpe")
        if rpe is not None:
            try:
                rpe = max(1, min(10, int(rpe)))
            except (TypeError, ValueError):
                rpe = None

        # Resolve date in the tenant's timezone (handles "today" / "yesterday"
        # / ISO; falls back to today-in-tenant-tz when uninterpretable).
        resolved = resolve_relative_date(tenant, data.get("date"))
        if resolved is None:
            resolved = today_in_tenant_tz(tenant)
        workout_date = str(resolved)

        # Validate activity is a non-empty string
        activity = str(data.get("activity") or WorkoutCategory(category).label).strip()
        if not activity:
            activity = WorkoutCategory(category).label

        # Phase 1 (#593) — deterministic registry correction of each set's
        # `type` and (strength↔calisthenics) category before persistence,
        # so a mis-classified set ("plank" as reps+weight) can't be stored.
        # Local import: matches this module's idiom (detect_prs) and keeps
        # the lint-autofix from reaping it between edits.
        from .set_contract import normalize_detail, validate_detail

        detail_json, category = normalize_detail(data.get("detail_json", {}) or {}, category, activity=activity)[:2]
        detail_json, verr = validate_detail(detail_json, category)
        if verr is not None:
            return Response(verr.as_tool_result(), status=status.HTTP_400_BAD_REQUEST)

        try:
            workout = Workout.objects.create(
                tenant=tenant,
                date=workout_date,
                status=workout_status,
                category=category,
                activity=activity,
                duration_minutes=duration,
                rpe=rpe,
                notes=data.get("notes", ""),
                detail_json=detail_json,
            )
        except Exception as exc:
            logger.exception("RuntimeLogWorkoutView failed for tenant %s", tenant_id)
            return Response(
                {"error": "create_failed", "detail": str(exc)},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # PR detection is best-effort — don't let it break workout logging
        try:
            from .services import detect_prs

            detect_prs(tenant, workout)
        except Exception:
            logger.exception("PR detection failed for workout %s", workout.id)

        return Response(
            {
                "id": str(workout.id),
                "date": str(workout.date),
                "category": workout.category,
                "activity": workout.activity,
                "status": workout.status,
            },
            status=status.HTTP_201_CREATED,
        )


class RuntimeWorkoutDetailView(APIView):
    """PATCH/DELETE a single workout from the AI assistant."""

    permission_classes = [AllowAny]

    def _get_workout(self, request, tenant_id, workout_id):
        err = _internal_auth_or_401(request, tenant_id)
        if err:
            return None, None, err
        tenant_or_resp = _get_tenant_or_404(tenant_id)
        if isinstance(tenant_or_resp, Response):
            return None, None, tenant_or_resp
        tenant = tenant_or_resp
        try:
            workout = Workout.objects.get(id=workout_id, tenant=tenant)
        except Workout.DoesNotExist:
            return None, None, Response({"error": "workout_not_found"}, status=status.HTTP_404_NOT_FOUND)
        return tenant, workout, None

    def patch(self, request, tenant_id, workout_id):
        tenant, workout, err = self._get_workout(request, tenant_id, workout_id)
        if err:
            return err
        lock_resp = _edit_locked_response(workout)
        if lock_resp is not None:
            logger.info("runtime.patch.edit_locked workout=%s", workout_id)
            return lock_resp

        data = request.data
        updated_fields = []

        if "activity" in data:
            workout.activity = str(data["activity"]).strip()
            updated_fields.append("activity")

        if "category" in data:
            val = data["category"]
            if val in WorkoutCategory.values:
                workout.category = val
                updated_fields.append("category")

        if "status" in data:
            val = data["status"]
            if val in WorkoutStatus.values:
                workout.status = val
                updated_fields.append("status")

        if "date" in data:
            try:
                date.fromisoformat(str(data["date"]))
                workout.date = str(data["date"])
                updated_fields.append("date")
            except (ValueError, TypeError):
                pass

        if "duration_minutes" in data:
            val = data["duration_minutes"]
            if val is None:
                workout.duration_minutes = None
            else:
                try:
                    workout.duration_minutes = int(val)
                except (TypeError, ValueError):
                    pass
            updated_fields.append("duration_minutes")

        if "rpe" in data:
            val = data["rpe"]
            if val is None:
                workout.rpe = None
            else:
                try:
                    workout.rpe = max(1, min(10, int(val)))
                except (TypeError, ValueError):
                    pass
            updated_fields.append("rpe")

        if "notes" in data:
            workout.notes = str(data["notes"]).strip()
            updated_fields.append("notes")

        if "detail_json" in data and isinstance(data["detail_json"], dict):
            from .set_contract import normalize_detail, validate_detail

            nd, ncat = normalize_detail(data["detail_json"], workout.category, activity=workout.activity)[:2]
            nd, verr = validate_detail(nd, ncat)
            if verr is not None:
                return Response(verr.as_tool_result(), status=status.HTTP_400_BAD_REQUEST)
            workout.detail_json = nd
            updated_fields.append("detail_json")
            if ncat != workout.category:
                workout.category = ncat
                if "category" not in updated_fields:
                    updated_fields.append("category")

        if updated_fields:
            updated_fields.append("updated_at")
            try:
                workout.save(update_fields=updated_fields)
            except Exception as exc:
                logger.exception("RuntimeWorkoutDetailView PATCH failed for %s", workout_id)
                return Response(
                    {"error": "update_failed", "detail": str(exc)},
                    status=status.HTTP_400_BAD_REQUEST,
                )

            # Re-run PR detection if exercise data changed
            if "detail_json" in updated_fields:
                try:
                    from .services import detect_prs

                    detect_prs(tenant, workout)
                except Exception:
                    logger.exception("PR detection failed for workout %s", workout.id)

        return Response(
            {
                "id": str(workout.id),
                "date": str(workout.date),
                "category": workout.category,
                "activity": workout.activity,
                "status": workout.status,
                "duration_minutes": workout.duration_minutes,
                "rpe": workout.rpe,
            }
        )

    def delete(self, request, tenant_id, workout_id):
        _tenant, workout, err = self._get_workout(request, tenant_id, workout_id)
        if err:
            return err
        lock_resp = _edit_locked_response(workout)
        if lock_resp is not None:
            logger.info("runtime.delete.edit_locked workout=%s", workout_id)
            return lock_resp
        workout_info = {"id": str(workout.id), "activity": workout.activity, "date": str(workout.date)}
        workout.delete()
        return Response({"deleted": True, **workout_info})


class RuntimeWorkoutSkipView(APIView):
    """POST: assistant marks a planned workout as skipped, with reason.

    Soft-state — preserves the row for adherence; distinct from DELETE.
    Mirrors the consumer-facing WorkoutSkipView.
    """

    permission_classes = [AllowAny]

    def post(self, request, tenant_id, workout_id):
        err = _internal_auth_or_401(request, tenant_id)
        if err:
            return err
        tenant_or_resp = _get_tenant_or_404(tenant_id)
        if isinstance(tenant_or_resp, Response):
            return tenant_or_resp
        try:
            workout = Workout.objects.get(id=workout_id, tenant=tenant_or_resp)
        except Workout.DoesNotExist:
            return Response({"error": "workout_not_found"}, status=status.HTTP_404_NOT_FOUND)
        lock_resp = _edit_locked_response(workout)
        if lock_resp is not None:
            logger.info("runtime.skip.edit_locked workout=%s", workout_id)
            return lock_resp
        reason = str(request.data.get("reason") or "")[:128]
        workout.status = WorkoutStatus.SKIPPED
        workout.skip_reason = reason
        workout.save(update_fields=["status", "skip_reason", "updated_at"])
        return Response(
            {
                "id": str(workout.id),
                "status": workout.status,
                "skip_reason": workout.skip_reason,
                "date": str(workout.date),
            }
        )


class RuntimeWorkoutCompleteView(APIView):
    """POST: assistant marks a workout as completed.

    Optional: notes, rpe, duration_minutes. Mirrors WorkoutCompleteView.
    """

    permission_classes = [AllowAny]

    def post(self, request, tenant_id, workout_id):
        err = _internal_auth_or_401(request, tenant_id)
        if err:
            return err
        tenant_or_resp = _get_tenant_or_404(tenant_id)
        if isinstance(tenant_or_resp, Response):
            return tenant_or_resp
        try:
            workout = Workout.objects.get(id=workout_id, tenant=tenant_or_resp)
        except Workout.DoesNotExist:
            return Response({"error": "workout_not_found"}, status=status.HTTP_404_NOT_FOUND)
        lock_resp = _edit_locked_response(workout)
        if lock_resp is not None:
            logger.info("runtime.complete.edit_locked workout=%s", workout_id)
            return lock_resp
        workout.status = WorkoutStatus.DONE
        if "notes" in request.data:
            workout.notes = str(request.data.get("notes") or "")
        if request.data.get("rpe") is not None:
            try:
                rpe = int(request.data["rpe"])
                if 1 <= rpe <= 10:
                    workout.rpe = rpe
            except (TypeError, ValueError):
                pass
        if request.data.get("duration_minutes") is not None:
            try:
                workout.duration_minutes = int(request.data["duration_minutes"])
            except (TypeError, ValueError):
                pass
        # Scoped save — a full-column save from a stale in-memory copy
        # would blind-revert fields a concurrent HealthKit sync just wrote
        # (external_id, merged detail_json).
        workout.save(update_fields=["status", "notes", "rpe", "duration_minutes", "updated_at"])
        try:
            from .services import detect_prs

            detect_prs(tenant_or_resp, workout)
        except Exception:
            logger.exception("PR detection failed for workout %s", workout.id)
        return Response(
            {
                "id": str(workout.id),
                "status": workout.status,
                "rpe": workout.rpe,
                "duration_minutes": workout.duration_minutes,
                "date": str(workout.date),
            }
        )


class RuntimeWorkoutSwapView(APIView):
    """POST: assistant swaps scheduled_at + date of two workouts atomically.

    Body: {"a": <uuid>, "b": <uuid>}. Mirrors WorkoutSwapView.
    """

    permission_classes = [AllowAny]

    def post(self, request, tenant_id):
        from django.db import transaction

        err = _internal_auth_or_401(request, tenant_id)
        if err:
            return err
        tenant_or_resp = _get_tenant_or_404(tenant_id)
        if isinstance(tenant_or_resp, Response):
            return tenant_or_resp
        a_id = request.data.get("a")
        b_id = request.data.get("b")
        if not a_id or not b_id or a_id == b_id:
            return Response(
                {"error": "must provide distinct 'a' and 'b' workout ids"},
                status=status.HTTP_400_BAD_REQUEST,
            )
        try:
            a = Workout.objects.get(id=a_id, tenant=tenant_or_resp)
            b = Workout.objects.get(id=b_id, tenant=tenant_or_resp)
        except Workout.DoesNotExist:
            return Response({"error": "workout_not_found"}, status=status.HTTP_404_NOT_FOUND)
        with transaction.atomic():
            a.scheduled_at, b.scheduled_at = b.scheduled_at, a.scheduled_at
            a.window_start_at, b.window_start_at = b.window_start_at, a.window_start_at
            a.window_end_at, b.window_end_at = b.window_end_at, a.window_end_at
            a.date, b.date = b.date, a.date
            a.save(update_fields=["scheduled_at", "window_start_at", "window_end_at", "date", "updated_at"])
            b.save(update_fields=["scheduled_at", "window_start_at", "window_end_at", "date", "updated_at"])
        return Response(
            {
                "a": {
                    "id": str(a.id),
                    "scheduled_at": a.scheduled_at.isoformat() if a.scheduled_at else None,
                    "date": str(a.date),
                },
                "b": {
                    "id": str(b.id),
                    "scheduled_at": b.scheduled_at.isoformat() if b.scheduled_at else None,
                    "date": str(b.date),
                },
            }
        )


class RuntimeFuelSummaryView(APIView):
    """GET: recent workouts + weekly stats for AI context."""

    permission_classes = [AllowAny]

    def get(self, request, tenant_id):
        err = _internal_auth_or_401(request, tenant_id)
        if err:
            return err
        tenant_or_resp = _get_tenant_or_404(tenant_id)
        if isinstance(tenant_or_resp, Response):
            return tenant_or_resp
        tenant = tenant_or_resp

        recent = Workout.objects.filter(tenant=tenant, status="done").order_by("-date", "-created_at")[:20]
        recent_data = []
        for w in recent:
            entry = {
                "id": str(w.id),
                "date": str(w.date),
                "category": w.category,
                "activity": w.activity,
                "duration_minutes": w.duration_minutes,
                "rpe": w.rpe,
                "source": w.source,
            }
            # Measured metrics (HealthKit imports and any logged actuals)
            # so the assistant can coach off real data, not just labels.
            detail = w.detail_json if isinstance(w.detail_json, dict) else {}
            for key in ("distance_km", "avg_hr", "peak_hr", "calories"):
                if isinstance(detail.get(key), int | float):
                    entry[key] = detail[key]
            recent_data.append(entry)

        planned = Workout.objects.filter(tenant=tenant, status="planned").order_by("date")[:10]
        planned_data = [
            {
                "id": str(w.id),
                "date": str(w.date),
                "category": w.category,
                "activity": w.activity,
                "duration_minutes": w.duration_minutes,
            }
            for w in planned
        ]

        # Latest body weight
        latest_weight = BodyWeightLog.objects.filter(tenant=tenant).first()
        weight_data = None
        if latest_weight:
            weight_data = {"date": str(latest_weight.date), "weight_kg": str(latest_weight.weight_kg)}

        # Fitness profile
        try:
            profile = FuelProfile.objects.get(tenant=tenant)
            profile_data = _serialize_profile(profile)
        except FuelProfile.DoesNotExist:
            profile_data = None

        # Latest sleep
        from .models import SleepLog

        latest_sleep = SleepLog.objects.filter(tenant=tenant).first()
        sleep_data = None
        if latest_sleep:
            sleep_data = {
                "date": str(latest_sleep.date),
                "duration_hours": str(latest_sleep.duration_hours),
                "quality": latest_sleep.quality,
            }

        # Latest resting HR (HealthKit daily sync or manual log)
        from .models import RestingHeartRateLog

        latest_rhr = RestingHeartRateLog.objects.filter(tenant=tenant).order_by("-date").first()
        rhr_data = None
        if latest_rhr:
            rhr_data = {"date": str(latest_rhr.date), "bpm": latest_rhr.bpm}

        # Active workout plans
        active_plans = WorkoutPlan.objects.filter(tenant=tenant, status=PlanStatus.ACTIVE)[:3]
        plans_data = []
        for p in active_plans:
            total = Workout.objects.filter(plan=p).count()
            done = Workout.objects.filter(plan=p, status=WorkoutStatus.DONE).count()
            plans_data.append(
                {
                    "id": str(p.id),
                    "name": p.name,
                    "start_date": str(p.start_date),
                    "weeks": p.weeks,
                    "days_per_week": p.days_per_week,
                    "workout_count": total,
                    "completed_count": done,
                }
            )

        return Response(
            {
                "recent_workouts": recent_data,
                "planned_workouts": planned_data,
                "active_plans": plans_data,
                "latest_body_weight": weight_data,
                "latest_sleep": sleep_data,
                "latest_resting_hr": rhr_data,
                "profile": profile_data,
            }
        )


class RuntimeFuelProfileView(APIView):
    """GET/PATCH: fitness profile for the AI assistant."""

    permission_classes = [AllowAny]

    def get(self, request, tenant_id):
        err = _internal_auth_or_401(request, tenant_id)
        if err:
            return err
        tenant_or_resp = _get_tenant_or_404(tenant_id)
        if isinstance(tenant_or_resp, Response):
            return tenant_or_resp
        tenant = tenant_or_resp

        try:
            profile = FuelProfile.objects.get(tenant=tenant)
        except FuelProfile.DoesNotExist:
            return Response({"error": "no_profile"}, status=status.HTTP_404_NOT_FOUND)
        return Response(_serialize_profile(profile))

    def patch(self, request, tenant_id):
        err = _internal_auth_or_401(request, tenant_id)
        if err:
            return err
        tenant_or_resp = _get_tenant_or_404(tenant_id)
        if isinstance(tenant_or_resp, Response):
            return tenant_or_resp
        tenant = tenant_or_resp

        profile, _created = FuelProfile.objects.get_or_create(tenant=tenant)
        data = request.data
        updated_fields = []

        if "onboarding_status" in data:
            val = data["onboarding_status"]
            if val in OnboardingStatus.values:
                profile.onboarding_status = val
                updated_fields.append("onboarding_status")

        _VALID_FITNESS_LEVELS = {"beginner", "intermediate", "advanced", ""}
        if "fitness_level" in data:
            val = str(data["fitness_level"]).strip()
            if val in _VALID_FITNESS_LEVELS:
                profile.fitness_level = val
                updated_fields.append("fitness_level")

        if "additional_context" in data:
            profile.additional_context = str(data["additional_context"]).strip()
            updated_fields.append("additional_context")

        for field in ("goals", "limitations", "equipment"):
            if field in data and isinstance(data[field], list):
                # Ensure all items are strings
                cleaned = [str(item).strip() for item in data[field] if item is not None]
                setattr(profile, field, cleaned)
                updated_fields.append(field)

        if "days_per_week" in data:
            val = data["days_per_week"]
            # Coerce string to int
            if isinstance(val, str):
                try:
                    val = int(val)
                except (TypeError, ValueError):
                    val = None
            if val is None or (isinstance(val, int) and 1 <= val <= 7):
                profile.days_per_week = val
                updated_fields.append("days_per_week")

        if "preferred_days" in data and isinstance(data["preferred_days"], list):
            cleaned = [int(d) for d in data["preferred_days"] if isinstance(d, int) and 0 <= d <= 6]
            profile.preferred_days = cleaned
            updated_fields.append("preferred_days")

        if "preferred_time" in data:
            val = str(data["preferred_time"]).strip().lower()
            if val in {"morning", "afternoon", "evening", ""}:
                profile.preferred_time = val
                updated_fields.append("preferred_time")

        if updated_fields:
            updated_fields.append("updated_at")
            try:
                profile.save(update_fields=updated_fields)
            except Exception as exc:
                logger.exception("Profile save failed for tenant %s", tenant_id)
                return Response(
                    {"error": "save_failed", "detail": str(exc)},
                    status=status.HTTP_400_BAD_REQUEST,
                )

        # If preferred_time changed and there's an active plan, update the fuel cron
        if "preferred_time" in updated_fields:
            active_plan = WorkoutPlan.objects.filter(tenant=tenant, status="active").order_by("-created_at").first()
            if active_plan:
                _manage_fuel_cron(tenant, active_plan, action="update")

        return Response(_serialize_profile(profile))


class RuntimeBodyWeightView(APIView):
    """POST: log body weight. DELETE: remove an entry by date."""

    permission_classes = [AllowAny]

    def post(self, request, tenant_id):
        err = _internal_auth_or_401(request, tenant_id)
        if err:
            return err
        tenant_or_resp = _get_tenant_or_404(tenant_id)
        if isinstance(tenant_or_resp, Response):
            return tenant_or_resp
        tenant = tenant_or_resp

        data = request.data
        # Resolve date in the tenant's timezone so a morning entry doesn't
        # land on yesterday when the server's UTC clock has already rolled
        # over (Bug #3 from the 2026-05-16 video session).
        resolved = resolve_relative_date(tenant, data.get("date"))
        if resolved is None:
            resolved = today_in_tenant_tz(tenant)
        weight_date = str(resolved)
        weight_val = data.get("weight_kg")
        if weight_val is None:
            return Response({"error": "weight_kg is required"}, status=status.HTTP_400_BAD_REQUEST)

        try:
            weight_kg = Decimal(str(weight_val))
        except (InvalidOperation, ValueError):
            return Response({"error": "weight_kg must be a valid number"}, status=status.HTTP_400_BAD_REQUEST)

        if weight_kg <= 0 or weight_kg > 500:
            return Response({"error": "weight_kg must be between 0 and 500"}, status=status.HTTP_400_BAD_REQUEST)

        entry, created = BodyWeightLog.objects.update_or_create(
            tenant=tenant,
            date=weight_date,
            defaults={"weight_kg": weight_kg},
        )
        return Response(
            {"date": str(entry.date), "weight_kg": str(entry.weight_kg), "created": created},
            status=status.HTTP_201_CREATED if created else status.HTTP_200_OK,
        )

    def delete(self, request, tenant_id):
        err = _internal_auth_or_401(request, tenant_id)
        if err:
            return err
        tenant_or_resp = _get_tenant_or_404(tenant_id)
        if isinstance(tenant_or_resp, Response):
            return tenant_or_resp
        tenant = tenant_or_resp

        weight_date = request.query_params.get("date") or request.data.get("date")
        if not weight_date:
            return Response({"error": "date is required"}, status=status.HTTP_400_BAD_REQUEST)
        try:
            date.fromisoformat(str(weight_date))
        except (ValueError, TypeError):
            return Response({"error": "date must be YYYY-MM-DD"}, status=status.HTTP_400_BAD_REQUEST)

        try:
            entry = BodyWeightLog.objects.get(tenant=tenant, date=weight_date)
        except BodyWeightLog.DoesNotExist:
            return Response(
                {"error": "no_entry_for_date", "date": str(weight_date)},
                status=status.HTTP_404_NOT_FOUND,
            )
        entry.delete()
        return Response({"deleted": True, "date": str(weight_date)}, status=status.HTTP_200_OK)


class RuntimeSleepView(APIView):
    """POST: log sleep from the AI assistant."""

    permission_classes = [AllowAny]

    def post(self, request, tenant_id):
        err = _internal_auth_or_401(request, tenant_id)
        if err:
            return err
        tenant_or_resp = _get_tenant_or_404(tenant_id)
        if isinstance(tenant_or_resp, Response):
            return tenant_or_resp
        tenant = tenant_or_resp

        from .models import SleepLog

        data = request.data
        # Same tz-aware resolution as body weight and workout endpoints.
        resolved = resolve_relative_date(tenant, data.get("date"))
        if resolved is None:
            resolved = today_in_tenant_tz(tenant)
        sleep_date = str(resolved)
        duration_val = data.get("duration_hours")
        if duration_val is None:
            return Response({"error": "duration_hours is required"}, status=status.HTTP_400_BAD_REQUEST)

        try:
            duration = Decimal(str(duration_val))
        except (InvalidOperation, ValueError):
            return Response({"error": "duration_hours must be a number"}, status=status.HTTP_400_BAD_REQUEST)

        if duration < 0 or duration > 24:
            return Response({"error": "duration_hours must be between 0 and 24"}, status=status.HTTP_400_BAD_REQUEST)

        quality = None
        quality_raw = data.get("quality")
        if quality_raw is not None:
            try:
                quality = max(1, min(5, int(quality_raw)))
            except (TypeError, ValueError):
                quality = None

        entry, created = SleepLog.objects.update_or_create(
            tenant=tenant,
            date=sleep_date,
            defaults={
                "duration_hours": duration,
                "quality": quality,
                "notes": str(data.get("notes", "")).strip(),
            },
        )
        return Response(
            {
                "date": str(entry.date),
                "duration_hours": str(entry.duration_hours),
                "quality": entry.quality,
                "created": created,
            },
            status=status.HTTP_201_CREATED if created else status.HTTP_200_OK,
        )


# ── Workout Plan CRUD ────────────────────────────────────────────────


def _serialize_plan(plan, include_workouts=False):
    """Serialize a WorkoutPlan with optional workout list."""
    total = Workout.objects.filter(plan=plan).count()
    done = Workout.objects.filter(plan=plan, status=WorkoutStatus.DONE).count()
    data = {
        "id": str(plan.id),
        "name": plan.name,
        "status": plan.status,
        "start_date": str(plan.start_date),
        "weeks": plan.weeks,
        "days_per_week": plan.days_per_week,
        "schedule_json": plan.schedule_json,
        "objective": plan.objective,
        "week_overrides": plan.week_overrides,
        "notes": plan.notes,
        "workout_count": total,
        "completed_count": done,
    }
    if include_workouts:
        workouts = Workout.objects.filter(plan=plan).order_by("date", "created_at")
        data["workouts"] = [
            {
                "id": str(w.id),
                "date": str(w.date),
                "status": w.status,
                "category": w.category,
                "activity": w.activity,
                "duration_minutes": w.duration_minutes,
                "rpe": w.rpe,
            }
            for w in workouts
        ]
    return data


def _validate_normalize_schedule(schedule_json):
    """Validate weekday keys + normalize/validate each day's prescription.

    Returns ``(normalized_schedule, error_response)``. On any problem
    ``normalized_schedule`` is None and ``error_response`` is a 400 — carrying
    the ``LLMValidationError`` envelope when a strength/calisthenics
    ``detail_json`` is the culprit, so the agent self-corrects in-loop (the same
    chokepoint the log-workout path uses). Atomic by design: the caller persists
    nothing unless the whole schedule validates.
    """
    from .set_contract import normalize_detail, validate_detail

    normalized: dict = {}
    for day_str, workout_def in schedule_json.items():
        try:
            day_int = int(day_str)
        except (TypeError, ValueError):
            return None, Response(
                {"error": "invalid_schedule", "detail": f"weekday key '{day_str}' must be an integer 0-6"},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if day_int < 0 or day_int > 6:
            return None, Response(
                {"error": "invalid_schedule", "detail": f"weekday key '{day_str}' out of range (0=Mon..6=Sun)"},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if not isinstance(workout_def, dict):
            return None, Response(
                {"error": "invalid_schedule", "detail": f"day {day_str} value must be a workout object"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        category = workout_def.get("category", "other")
        if category not in WorkoutCategory.values:
            category = "other"
        activity = str(workout_def.get("activity") or WorkoutCategory(category).label).strip()

        detail = workout_def.get("detail_json", {}) or {}
        detail, category = normalize_detail(detail, category, activity=activity)[:2]
        detail, verr = validate_detail(detail, category)
        if verr is not None:
            payload = dict(verr.as_tool_result())
            payload["weekday"] = day_int
            return None, Response(payload, status=status.HTTP_400_BAD_REQUEST)

        target_rpe = workout_def.get("target_rpe", workout_def.get("rpe"))
        if target_rpe is not None:
            try:
                target_rpe = max(1, min(10, int(target_rpe)))
            except (TypeError, ValueError):
                target_rpe = None

        duration = workout_def.get("duration_minutes")
        if duration is not None:
            try:
                duration = int(duration)
            except (TypeError, ValueError):
                duration = None

        norm: dict = {"category": category, "activity": activity, "detail_json": detail}
        if duration is not None:
            norm["duration_minutes"] = duration
        if target_rpe is not None:
            norm["target_rpe"] = target_rpe
        normalized[str(day_int)] = norm

    return normalized, None


def _validate_normalize_week_overrides(week_overrides):
    """Validate the per-week progression/deload map.

    Keys are 0-indexed week offsets; values are partial schedule overrides merged
    over the base template for that week. A day mapped to ``null`` means "rest
    this week" (drop the base day). Returns ``(normalized, error_response)``;
    on error ``normalized`` is None.
    """
    if not week_overrides:
        return {}, None
    if not isinstance(week_overrides, dict):
        return None, Response(
            {"error": "invalid_week_overrides", "detail": "must be an object keyed by week offset"},
            status=status.HTTP_400_BAD_REQUEST,
        )

    normalized: dict = {}
    for wk_str, override in week_overrides.items():
        try:
            wk = int(wk_str)
        except (TypeError, ValueError):
            return None, Response(
                {"error": "invalid_week_overrides", "detail": f"week key '{wk_str}' must be an integer >= 0"},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if wk < 0:
            return None, Response(
                {"error": "invalid_week_overrides", "detail": f"week key '{wk_str}' must be >= 0"},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if not isinstance(override, dict):
            return None, Response(
                {"error": "invalid_week_overrides", "detail": f"week {wk} value must be an object"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        day_defs: dict = {}
        rest_days: dict = {}
        for day_str, val in override.items():
            try:
                day_int = int(day_str)
            except (TypeError, ValueError):
                return None, Response(
                    {"error": "invalid_week_overrides", "detail": f"weekday key '{day_str}' must be an integer 0-6"},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            if val is None:
                rest_days[str(day_int)] = None
            else:
                day_defs[str(day_int)] = val

        norm_days, err = _validate_normalize_schedule(day_defs) if day_defs else ({}, None)
        if err is not None:
            return None, err
        normalized[str(wk)] = {**norm_days, **rest_days}

    return normalized, None


def _expand_plan_workouts(plan, tenant, schedule_json, start_date, weeks, week_overrides=None):
    """Create planned Workout rows + matching PlanSlot rows from a schedule.

    Each workout gets its ``slot`` FK set so the reconciler (Phase 5) can
    later mutate slots in place without tombstoning workout uuids.

    ``week_overrides`` (0-indexed week offset -> partial schedule) applies
    per-week progression/deload: each override is merged over the base template
    for that week, with a day mapped to ``None`` dropped (rest). Inputs are
    assumed already normalized by ``_validate_normalize_*``.

    Switched from ``bulk_create`` to per-row create so each row can carry
    the slot FK we create alongside it. Typical plan size is bounded
    (max ~52 weeks × 7 weekdays = 364 slots), so the per-row cost is
    negligible vs. the safety it buys.
    """
    from datetime import timedelta

    from .models import PlanSlot

    week_overrides = week_overrides or {}
    plan_monday = start_date - timedelta(days=start_date.weekday())
    elapsed_weeks = max(0, (start_date - plan.start_date).days // 7)
    workouts_created = 0

    for week_offset in range(weeks):
        week_idx = elapsed_weeks + week_offset

        override = week_overrides.get(str(week_offset))
        if isinstance(override, dict):
            effective = dict(schedule_json)
            for day_key, day_val in override.items():
                if day_val is None:
                    effective.pop(str(day_key), None)
                else:
                    effective[str(day_key)] = day_val
        else:
            effective = schedule_json

        for day_str, workout_def in effective.items():
            try:
                day_int = int(day_str)
            except (TypeError, ValueError):
                continue
            if day_int < 0 or day_int > 6:
                continue

            workout_date = plan_monday + timedelta(weeks=week_offset, days=day_int)
            if workout_date < start_date:
                continue

            category = workout_def.get("category", "other")
            if category not in WorkoutCategory.values:
                category = "other"

            # get_or_create can't take an ``archived_at__isnull`` lookup as a
            # field-set; query active rows first, fall back to create.
            slot = PlanSlot.objects.filter(
                plan=plan,
                week_index=week_idx,
                weekday=day_int,
                archived_at__isnull=True,
            ).first()
            if slot is None:
                slot = PlanSlot.objects.create(
                    tenant=tenant,
                    plan=plan,
                    week_index=week_idx,
                    weekday=day_int,
                )

            Workout.objects.create(
                tenant=tenant,
                plan=plan,
                slot=slot,
                date=workout_date,
                status=WorkoutStatus.PLANNED,
                category=category,
                activity=str(workout_def.get("activity", WorkoutCategory(category).label)).strip(),
                duration_minutes=workout_def.get("duration_minutes"),
                rpe=workout_def.get("target_rpe"),
                detail_json=workout_def.get("detail_json", {}),
            )
            workouts_created += 1

    return workouts_created


def _manage_fuel_cron(tenant, plan, action="create"):
    """Best-effort cron lifecycle management for a workout plan.

    Actions: "create" (add cron), "remove" (delete cron), "update" (remove + recreate).
    Failures are logged but never block plan operations.
    """
    try:
        from apps.cron.gateway_client import GatewayError, invoke_gateway_tool
        from apps.orchestrator.config_generator import build_fuel_workout_cron
    except ImportError:
        logger.warning("Could not import gateway_client or config_generator for fuel cron")
        return

    cron_name = f"_fuel:{plan.name}"

    try:
        if action in ("remove", "update"):
            # Find and remove existing fuel cron(s) for this tenant
            try:
                result = invoke_gateway_tool(tenant, "cron.list", {"includeDisabled": True})
                existing = (
                    result.get("jobs", []) if isinstance(result, dict) else result if isinstance(result, list) else []
                )
                for job in existing:
                    if isinstance(job, dict) and str(job.get("name", "")).startswith("_fuel:"):
                        job_id = job.get("id") or job.get("jobId") or job.get("name")
                        if job_id:
                            invoke_gateway_tool(tenant, "cron.remove", {"jobId": job_id})
            except GatewayError:
                logger.warning("Failed to remove fuel cron for tenant %s", tenant.id)

        if action in ("create", "update"):
            if plan.status != "active":
                return  # Only create crons for active plans
            # Get preferred_time from profile for cron scheduling
            pref_time = ""
            try:
                pref_time = FuelProfile.objects.get(tenant=tenant).preferred_time
            except FuelProfile.DoesNotExist:
                pass
            job_dict = build_fuel_workout_cron(tenant, plan, preferred_time=pref_time)
            if job_dict:
                invoke_gateway_tool(tenant, "cron.add", {"job": job_dict})
                logger.info("Created fuel cron '%s' for tenant %s", cron_name, tenant.id)

    except GatewayError:
        logger.warning("Fuel cron %s failed for tenant %s (best-effort)", action, tenant.id)
    except Exception:
        logger.exception("Unexpected error managing fuel cron for tenant %s", tenant.id)


class RuntimeWorkoutPlanListCreateView(APIView):
    """GET: list plans. POST: create plan + expand into planned workouts."""

    permission_classes = [AllowAny]

    def get(self, request, tenant_id):
        err = _internal_auth_or_401(request, tenant_id)
        if err:
            return err
        tenant_or_resp = _get_tenant_or_404(tenant_id)
        if isinstance(tenant_or_resp, Response):
            return tenant_or_resp
        tenant = tenant_or_resp

        status_filter = request.query_params.get("status")
        qs = WorkoutPlan.objects.filter(tenant=tenant)
        if status_filter and status_filter in PlanStatus.values:
            qs = qs.filter(status=status_filter)
        # Active plans first, then by created_at desc
        plans = qs.order_by(
            db_models.Case(
                db_models.When(status=PlanStatus.ACTIVE, then=0),
                default=1,
                output_field=db_models.IntegerField(),
            ),
            "-created_at",
        )[:10]

        return Response({"plans": [_serialize_plan(p) for p in plans]})

    def post(self, request, tenant_id):
        err = _internal_auth_or_401(request, tenant_id)
        if err:
            return err
        tenant_or_resp = _get_tenant_or_404(tenant_id)
        if isinstance(tenant_or_resp, Response):
            return tenant_or_resp
        tenant = tenant_or_resp

        data = request.data
        name = str(data.get("name", "")).strip()
        if not name:
            return Response({"error": "name is required"}, status=status.HTTP_400_BAD_REQUEST)

        schedule_json = data.get("schedule_json", {})
        if not isinstance(schedule_json, dict) or not schedule_json:
            return Response(
                {"error": "schedule_json must be a non-empty object"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        weeks_val = data.get("weeks")
        try:
            weeks = max(1, min(52, int(weeks_val)))
        except (TypeError, ValueError):
            return Response({"error": "weeks must be an integer 1-52"}, status=status.HTTP_400_BAD_REQUEST)

        days_per_week_val = data.get("days_per_week")
        try:
            days_per_week = max(1, min(7, int(days_per_week_val)))
        except (TypeError, ValueError):
            return Response(
                {"error": "days_per_week must be an integer 1-7"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Validate + normalize every prescription BEFORE persisting anything, so a
        # malformed strength set is rejected with a self-correcting envelope rather
        # than silently stored (same chokepoint as log_workout). Atomic: nothing is
        # created unless the whole schedule (and any week overrides) validates.
        normalized_schedule, sched_err = _validate_normalize_schedule(schedule_json)
        if sched_err is not None:
            return sched_err

        normalized_overrides, ov_err = _validate_normalize_week_overrides(data.get("week_overrides"))
        if ov_err is not None:
            return ov_err

        # Resolve start_date in the TENANT's timezone — handles ISO + relative
        # phrases ("next monday", "today") — defaulting to the next Monday in
        # tenant-local time. Never bare ``date.today()``: that is computed in UTC
        # and drifts a day in the evening for tenants offset from UTC, which then
        # propagates into every materialized workout date.
        from datetime import timedelta

        plan_start = resolve_relative_date(tenant, data.get("start_date")) if data.get("start_date") else None
        if plan_start is None:
            today = today_in_tenant_tz(tenant)
            days_ahead = (7 - today.weekday()) % 7 or 7
            plan_start = today + timedelta(days=days_ahead)

        # Idempotency: a retried / double-fired create with the same name +
        # start_date returns the existing active plan instead of duplicating it
        # (and its whole calendar of planned workouts). Mirrors the task/goal
        # runtime dedup contract (return 200, not a second 201).
        existing = WorkoutPlan.objects.filter(
            tenant=tenant, name=name, start_date=plan_start, status=PlanStatus.ACTIVE
        ).first()
        if existing is not None:
            result = _serialize_plan(existing)
            result["deduped"] = True
            return Response(result, status=status.HTTP_200_OK)

        try:
            plan = WorkoutPlan.objects.create(
                tenant=tenant,
                name=name,
                start_date=plan_start,
                weeks=weeks,
                days_per_week=days_per_week,
                schedule_json=normalized_schedule,
                week_overrides=normalized_overrides,
                objective=str(data.get("objective", "")).strip()[:200],
                notes=str(data.get("notes", "")).strip(),
            )
        except Exception as exc:
            logger.exception("WorkoutPlan creation failed for tenant %s", tenant_id)
            return Response(
                {"error": "create_failed", "detail": str(exc)},
                status=status.HTTP_400_BAD_REQUEST,
            )

        workouts_created = _expand_plan_workouts(
            plan, tenant, normalized_schedule, plan_start, weeks, week_overrides=normalized_overrides
        )

        # Create background fuel cron (best-effort)
        _manage_fuel_cron(tenant, plan, action="create")

        result = _serialize_plan(plan)
        result["workouts_created"] = workouts_created
        return Response(result, status=status.HTTP_201_CREATED)


class RuntimeWorkoutPlanDetailView(APIView):
    """GET/PATCH/DELETE a single workout plan."""

    permission_classes = [AllowAny]

    def _get_plan(self, tenant, plan_id):
        try:
            return WorkoutPlan.objects.get(id=plan_id, tenant=tenant)
        except WorkoutPlan.DoesNotExist:
            return None

    def get(self, request, tenant_id, plan_id):
        err = _internal_auth_or_401(request, tenant_id)
        if err:
            return err
        tenant_or_resp = _get_tenant_or_404(tenant_id)
        if isinstance(tenant_or_resp, Response):
            return tenant_or_resp
        tenant = tenant_or_resp

        plan = self._get_plan(tenant, plan_id)
        if not plan:
            return Response({"error": "plan_not_found"}, status=status.HTTP_404_NOT_FOUND)

        return Response(_serialize_plan(plan, include_workouts=True))

    def patch(self, request, tenant_id, plan_id):
        err = _internal_auth_or_401(request, tenant_id)
        if err:
            return err
        tenant_or_resp = _get_tenant_or_404(tenant_id)
        if isinstance(tenant_or_resp, Response):
            return tenant_or_resp
        tenant = tenant_or_resp

        plan = self._get_plan(tenant, plan_id)
        if not plan:
            return Response({"error": "plan_not_found"}, status=status.HTTP_404_NOT_FOUND)

        data = request.data
        updated_fields = []
        needs_regeneration = False

        if "name" in data:
            plan.name = str(data["name"]).strip()
            updated_fields.append("name")

        if "status" in data and data["status"] in PlanStatus.values:
            plan.status = data["status"]
            updated_fields.append("status")

        if "notes" in data:
            plan.notes = str(data["notes"]).strip()
            updated_fields.append("notes")

        if "weeks" in data:
            try:
                plan.weeks = max(1, min(52, int(data["weeks"])))
                updated_fields.append("weeks")
                needs_regeneration = True
            except (TypeError, ValueError):
                pass

        if "schedule_json" in data and isinstance(data["schedule_json"], dict):
            plan.schedule_json = data["schedule_json"]
            updated_fields.append("schedule_json")
            needs_regeneration = True

        if "days_per_week" in data:
            try:
                plan.days_per_week = max(1, min(7, int(data["days_per_week"])))
                updated_fields.append("days_per_week")
            except (TypeError, ValueError):
                pass

        if updated_fields:
            updated_fields.append("updated_at")
            try:
                plan.save(update_fields=updated_fields)
            except Exception as exc:
                logger.exception("WorkoutPlan update failed for plan %s", plan_id)
                return Response(
                    {"error": "update_failed", "detail": str(exc)},
                    status=status.HTTP_400_BAD_REQUEST,
                )

        # Reconcile slot/workout state with the desired schedule. Replaces
        # the old DELETE+INSERT regen — the slot model now provides stable
        # identity so a workout uuid the user's browser is holding stays
        # valid across regens. User-actively-edited workouts (with an
        # active edit_lock) are skipped from deletion; see
        # apps.fuel.services.apply_reconciliation.
        if needs_regeneration:
            from django.utils import timezone

            from .services import apply_reconciliation, reconcile_plan_state

            def _is_edit_locked(workout) -> bool:
                if workout.edit_lock_until is None:
                    return False
                return workout.edit_lock_until > timezone.now()

            rec = reconcile_plan_state(plan, plan.schedule_json, plan.weeks)
            try:
                counts = apply_reconciliation(
                    rec,
                    plan=plan,
                    tenant=tenant,
                    edit_lock_check=_is_edit_locked,
                )
                logger.info("fuel.plan_reconciled plan=%s counts=%s", plan.id, counts)
            except Exception:
                logger.exception("fuel.plan_reconcile_failed plan=%s", plan.id)
                return Response(
                    {"error": "regen_failed"},
                    status=status.HTTP_500_INTERNAL_SERVER_ERROR,
                )

        # Manage fuel cron based on status/schedule changes (best-effort)
        if "status" in updated_fields or needs_regeneration:
            if plan.status == "active":
                _manage_fuel_cron(tenant, plan, action="update")
            else:
                # Paused, completed, archived → remove cron
                _manage_fuel_cron(tenant, plan, action="remove")

        return Response(_serialize_plan(plan, include_workouts=True))

    def delete(self, request, tenant_id, plan_id):
        err = _internal_auth_or_401(request, tenant_id)
        if err:
            return err
        tenant_or_resp = _get_tenant_or_404(tenant_id)
        if isinstance(tenant_or_resp, Response):
            return tenant_or_resp
        tenant = tenant_or_resp

        plan = self._get_plan(tenant, plan_id)
        if not plan:
            return Response({"error": "plan_not_found"}, status=status.HTTP_404_NOT_FOUND)

        # Remove fuel cron before deleting plan (best-effort)
        _manage_fuel_cron(tenant, plan, action="remove")

        # Delete planned workouts, preserve completed ones
        Workout.objects.filter(plan=plan, status=WorkoutStatus.PLANNED).delete()
        Workout.objects.filter(plan=plan).exclude(status=WorkoutStatus.PLANNED).update(plan=None)
        plan.delete()

        return Response(status=status.HTTP_204_NO_CONTENT)


# ═════════════════════════════════════════════════════════════════════
# Audit — single source-of-truth view for the assistant before
# creating/proposing/delivering any workout-related schedule. Cross-
# references three places where workout state can hide:
#   1. Today's daily-note Fuel section (the "today_plan" the user is
#      already locked into — written by the morning prep cron).
#   2. Workout rows in Postgres (next 14d).
#   3. The OpenClaw container's cron registry (active _fuel:* and any
#      other workout-named user-created cron).
# ═════════════════════════════════════════════════════════════════════


def _parse_fuel_section(markdown: str) -> str | None:
    """Return the contents of the `## Fuel` section from a daily-note doc, or None."""
    if not markdown:
        return None
    marker = "## Fuel"
    idx = markdown.find(marker)
    if idx == -1:
        # Try lowercase / variant
        lower = markdown.lower().find("## fuel")
        if lower == -1:
            return None
        idx = lower
    after_heading = markdown.find("\n", idx)
    if after_heading == -1:
        return None
    next_heading = markdown.find("\n## ", after_heading + 1)
    if next_heading == -1:
        section_body = markdown[after_heading + 1 :]
    else:
        section_body = markdown[after_heading + 1 : next_heading]
    section_body = section_body.strip()
    return section_body or None


class RuntimeFuelAuditView(APIView):
    """GET: cross-reference today's daily note + Workout rows + container crons.

    Designed to be the single tool the assistant calls before suggesting,
    delivering, or scheduling any workout. Returns conflicts so the agent
    can stop short of creating duplicates or contradicting the locked plan.
    """

    permission_classes = [AllowAny]

    def get(self, request, tenant_id):
        from datetime import datetime, timedelta

        from apps.cron.gateway_client import GatewayError, invoke_gateway_tool
        from apps.journal.models import Document
        from apps.orchestrator.fuel_cron import _FUEL_SESSION_PREFIX
        from apps.orchestrator.services import _extract_cron_jobs

        err = _internal_auth_or_401(request, tenant_id)
        if err:
            return err
        tenant_or_resp = _get_tenant_or_404(tenant_id)
        if isinstance(tenant_or_resp, Response):
            return tenant_or_resp
        tenant = tenant_or_resp

        now = datetime.now(tz=UTC)
        today = date.today()
        horizon_14d_end = today + timedelta(days=14)
        horizon_48h_end = now + timedelta(hours=48)

        # 1. today_plan — parse from today's daily-note Fuel section
        today_doc = Document.objects.filter(tenant=tenant, kind="daily", slug=str(today)).first()
        today_plan_body = _parse_fuel_section(today_doc.markdown) if today_doc else None
        today_plan = {
            "exists": bool(today_plan_body),
            "iso_date": str(today),
            "raw_section": today_plan_body,
        }

        # 2. next_14d_workouts — Postgres truth
        next_14d_qs = Workout.objects.filter(
            tenant=tenant,
            date__gte=today,
            date__lte=horizon_14d_end,
        ).order_by("date", "scheduled_at", "created_at")
        next_14d = [
            {
                "id": str(w.id),
                "date": str(w.date),
                "scheduled_at": w.scheduled_at.isoformat() if w.scheduled_at else None,
                "category": w.category,
                "activity": w.activity,
                "status": w.status,
                "duration_minutes": w.duration_minutes,
            }
            for w in next_14d_qs
        ]

        # 3. fuel-related crons — gateway cron.list filtered to _fuel:* and
        # any user-named cron whose name hints at workout activity.
        fuel_crons: list[dict] = []
        cron_list_error: str | None = None
        try:
            list_result = invoke_gateway_tool(tenant, "cron.list", {"includeDisabled": True})
            all_jobs = _extract_cron_jobs(list_result) or []
            workout_hints = (
                "fuel",
                "workout",
                "lift",
                "run",
                "yoga",
                "gym",
                "train",
                "push",
                "pull",
                "leg",
                "session",
                "exercise",
                "cardio",
                "hiit",
                "bouldering",
                "climb",
                "cycle",
                "swim",
            )
            for j in all_jobs:
                if not isinstance(j, dict):
                    continue
                name = (j.get("name") or "").strip()
                lname = name.lower()
                is_fuel_session = name.startswith(_FUEL_SESSION_PREFIX)
                is_workout_hint = any(h in lname for h in workout_hints) and not lname.startswith("_sync:")
                if is_fuel_session or is_workout_hint:
                    # ``nextRunAtMs`` lives under ``state`` in the gateway's
                    # cron.list response — reading it from the top level
                    # always returned None and hid the cron's actual fire
                    # time from the audit response.
                    job_state = j.get("state") or {}
                    fuel_crons.append(
                        {
                            "name": name,
                            "id": j.get("id") or j.get("jobId"),
                            "schedule": j.get("schedule"),
                            "next_fire_at_ms": job_state.get("nextRunAtMs"),
                            "kind": "fuel_session" if is_fuel_session else "user_named",
                            "enabled": j.get("enabled", True),
                        }
                    )
        except GatewayError as exc:
            cron_list_error = str(exc)
            logger.warning(
                "RuntimeFuelAuditView: cron.list failed for tenant %s: %s",
                tenant_id,
                exc,
            )

        # 4. conflicts
        # duplicate_fires: more than one cron fires at the same minute
        by_fire: dict[str, list[str]] = {}
        for c in fuel_crons:
            sched = c.get("schedule") or {}
            expr = sched.get("expr") or sched.get("cronExpr") or ""
            tz = sched.get("tz") or ""
            key = f"{expr}@{tz}"
            if expr:
                by_fire.setdefault(key, []).append(c["name"])
        duplicate_fires = [{"fires_at": k, "crons": names} for k, names in by_fire.items() if len(names) > 1]

        # orphan_crons: _fuel:{8-hex} cron whose Workout (by short id) isn't in next_14d
        next_14d_short_ids = {w["id"].split("-")[0] for w in next_14d}
        orphan_crons = [
            {"name": c["name"], "kind": c["kind"]}
            for c in fuel_crons
            if c["kind"] == "fuel_session" and c["name"].removeprefix(_FUEL_SESSION_PREFIX) not in next_14d_short_ids
        ]

        # orphan_workouts: planned Workout in next 48h with no matching _fuel: cron
        fuel_session_short_ids = {
            c["name"].removeprefix(_FUEL_SESSION_PREFIX) for c in fuel_crons if c["kind"] == "fuel_session"
        }
        orphan_workouts = []
        for w in next_14d_qs:
            if w.status != WorkoutStatus.PLANNED:
                continue
            if not w.scheduled_at or w.scheduled_at > horizon_48h_end:
                continue
            short = str(w.id).split("-")[0]
            if short not in fuel_session_short_ids:
                orphan_workouts.append({"id": str(w.id), "date": str(w.date), "activity": w.activity})

        return Response(
            {
                "today_plan": today_plan,
                "next_14d_workouts": next_14d,
                "fuel_crons": fuel_crons,
                "cron_list_error": cron_list_error,
                "conflicts": {
                    "duplicate_fires": duplicate_fires,
                    "orphan_crons": orphan_crons,
                    "orphan_workouts": orphan_workouts,
                },
                "guidance": _audit_guidance(today_plan, fuel_crons, duplicate_fires),
            }
        )


def _audit_guidance(today_plan: dict, fuel_crons: list, duplicate_fires: list) -> str:
    """Single-line instruction for the agent based on the audit state."""
    if duplicate_fires:
        return (
            "STOP — duplicate cron firings detected. Surface the duplicates to the user "
            "and offer to remove them. Do NOT add more crons until they are resolved."
        )
    if today_plan.get("exists"):
        return (
            "today_plan.raw_section is the locked plan description for today. Deliver "
            "THAT plan to the user verbatim — do not invent a different one. To UPDATE "
            "or DELETE today's workout (e.g. swap an exercise, change weights), find "
            "the matching workout_id in next_14d_workouts[i].id (match by date) and "
            "call nbhd_fuel_update_workout or nbhd_fuel_delete_workout directly. "
            "Workout IDs are already in this response — do NOT call nbhd_fuel_summary "
            "just to retrieve them."
        )
    return (
        "No locked plan for today. Safe to propose one. Before scheduling, check "
        "next_14d_workouts so your proposal fits the existing program. Workout IDs "
        "for any update or delete are in next_14d_workouts[i].id."
    )
