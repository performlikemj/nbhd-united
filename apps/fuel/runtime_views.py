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

from apps.common.llm_contracts import WEEKDAY_INDEX, WEEKDAY_NAMES, resolve_relative_date, today_in_tenant_tz
from apps.integrations.internal_auth import InternalAuthError, validate_internal_runtime_request
from apps.pii.egress import KnownValueResponseGuardMixin
from apps.router.document_write_guard import assert_write_allowed_for_document_turn, record_runtime_write_activity
from apps.tenants.middleware import set_rls_context
from apps.tenants.models import Tenant

from .models import (
    BodyWeightLog,
    FuelProfile,
    OnboardingStatus,
    PersonalRecord,
    PlanStatus,
    Workout,
    WorkoutCategory,
    WorkoutPlan,
    WorkoutSource,
    WorkoutStatus,
)

logger = logging.getLogger(__name__)


class _FuelResponseGuard(KnownValueResponseGuardMixin):
    pii_egress_seam = "fuel_runtime_response"
    pii_egress_text_fields = frozenset(
        {
            "name",
            "objective",
            "notes",
            "goals",
            "limitations",
            "equipment",
            "additional_context",
            "detail",
            "detail_json",
            "activity",
            "exercise",
            "skip_reason",
            "reason",
            "summary",
        }
    )


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


def _serialize_workout_summary_card(workout: Workout) -> dict:
    entry = {
        "id": str(workout.id),
        "date": str(workout.date),
        "category": workout.category,
        "activity": workout.activity,
        "duration_minutes": workout.duration_minutes,
        "rpe": workout.rpe,
        "source": workout.source,
    }
    # Measured metrics (HealthKit imports and any logged actuals) so the
    # assistant can coach off real data, not just labels.
    detail = workout.detail_json if isinstance(workout.detail_json, dict) else {}
    for key in ("distance_km", "avg_hr", "peak_hr", "calories"):
        if isinstance(detail.get(key), int | float):
            entry[key] = detail[key]
    return entry


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


def _emit_fuel_event(tenant, *, tool_name, outcome, reason_code="", detail=None):
    """Call-site telemetry for the fuel tool contract (namespace ``fuel``).

    The generic runtime middleware already records accept/reject/latency per
    endpoint; what it cannot know is WHY. These events carry the reason a fuel
    call was rejected — or was silently normalized — so a contract drift is
    found because a number moved, not because someone tripped over it in a
    conversation months later. Shape-only values; never caller text (see
    ``docs/agents/telemetry.md``).
    """
    from apps.platform_logs.telemetry import emit_tool_event

    emit_tool_event(
        tool_name=tool_name,
        outcome=outcome,
        namespace="fuel",
        tenant_id=getattr(tenant, "id", None),
        reason_code=reason_code,
        detail=detail or {},
    )


_STATUS_HINT = ", ".join(WorkoutStatus.values)

# Statuses describing a session that did not happen. Nothing was performed and
# nothing is prescribed, so the empty-prescription guard does not apply to them.
_NO_PRESCRIPTION_STATUSES = frozenset({WorkoutStatus.SKIPPED, WorkoutStatus.RESCHEDULED, WorkoutStatus.REST})


def _reject_unknown_status(tenant, value, *, tool_name):
    """400 for a workout status outside :class:`WorkoutStatus`.

    This used to be a silent coercion to "done" on the create path, which is
    the worst possible default: a user telling the assistant "I missed leg
    day" had their missed session recorded as a completed workout, inflating
    adherence with training that never happened. Reject instead, and name the
    legal values so the model picks the right one in-loop.
    """
    from apps.common.llm_contracts import LLMValidationError

    _emit_fuel_event(tenant, tool_name=tool_name, outcome="rejected", reason_code="unknown_status_rejected")
    shown = str(value)[:40]
    err = LLMValidationError(
        message=(
            f"'{shown}' is not a workout status. Use one of: {_STATUS_HINT}. "
            'A session the user did not do is "skipped" (keep it for adherence) or '
            '"rescheduled" — never "done".'
        ),
        details=[
            {
                "loc": ["status"],
                "msg": f"status must be one of: {_STATUS_HINT}",
                "type": "unknown_status",
                "allowed": list(WorkoutStatus.values),
            }
        ],
    )
    return Response(err.as_tool_result(), status=status.HTTP_400_BAD_REQUEST)


def _weekday_key_style(keys) -> str:
    """Which spelling a caller used for weekday keys: name / int / mixed / none.

    Both spellings are accepted, but they are not equally safe — an integer
    index is silently corruptible across the three numbering conventions in
    this product, which is how a Wednesday session once landed on Thursday.
    Measuring the split is how we know when the legacy integer surface has
    actually stopped being used.
    """
    saw_int = False
    saw_name = False
    for key in keys:
        token = str(key).strip().lower()
        if token in WEEKDAY_INDEX:
            saw_name = True
            continue
        try:
            int(token)
        except (TypeError, ValueError):
            continue
        saw_int = True
    if saw_int and saw_name:
        return "mixed"
    if saw_name:
        return "name"
    if saw_int:
        return "int"
    return "none"


def _emit_weekday_key_style(tenant, keys, *, tool_name):
    style = _weekday_key_style(keys)
    _emit_fuel_event(
        tenant,
        tool_name=tool_name,
        outcome="accepted" if style == "name" else "normalized",
        reason_code="weekday_key_style",
        detail={"weekday_key_style": style},
    )


class RuntimeLogWorkoutView(_FuelResponseGuard, APIView):
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

        blocked = assert_write_allowed_for_document_turn(tenant)
        if blocked is not None:
            return blocked

        data = request.data
        category = data.get("category", "other")
        if category not in WorkoutCategory.values:
            category = "other"

        workout_status = data.get("status", "done")
        if workout_status not in WorkoutStatus.values:
            return _reject_unknown_status(tenant, workout_status, tool_name="runtime-fuel-log")

        # Coerce duration_minutes and rpe to int, tolerating non-numeric input
        duration = data.get("duration_minutes")
        if duration is not None:
            try:
                duration = int(duration)
            except (TypeError, ValueError):
                duration = None

        # The 1-10 clamp stays — an out-of-range rpe is a scale confusion, not a
        # reason to drop the whole log — but it is no longer invisible: the
        # stored value is echoed back and the clamp is counted, so "rpe 99"
        # doesn't quietly become a 10 the model still believes is a 99.
        rpe = data.get("rpe")
        rpe_clamped = False
        if rpe is not None:
            try:
                raw_rpe = int(rpe)
            except (TypeError, ValueError):
                rpe = None
            else:
                rpe = max(1, min(10, raw_rpe))
                rpe_clamped = rpe != raw_rpe

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
        from .set_contract import normalize_detail, validate_detail, validate_flat_detail

        detail_json, category = normalize_detail(data.get("detail_json", {}) or {}, category, activity=activity)[:2]
        detail_json, verr = validate_detail(detail_json, category)
        if verr is not None:
            return Response(verr.as_tool_result(), status=status.HTTP_400_BAD_REQUEST)

        # Cardio/HIIT/mobility numbers must actually be numbers. Unvalidated,
        # a distance of "5 miles" persists fine and then raises on every load
        # of that user's cardio Progress view for good.
        detail_json, flat_err = validate_flat_detail(detail_json, category)
        if flat_err is not None:
            _emit_fuel_event(
                tenant,
                tool_name="runtime-fuel-log",
                outcome="rejected",
                reason_code="cardio_detail_invalid",
                detail={"category": category, "field": str(flat_err.details[0]["loc"][-1])},
            )
            return Response(flat_err.as_tool_result(), status=status.HTTP_400_BAD_REQUEST)

        # A strength/calisthenics log with no exercises is an invisible
        # workout: the row exists, the Fuel tab shows the activity name, and
        # opening it reveals nothing to do or to review. The PLAN path has
        # rejected this since #1481 and its comment predicted this exact hole
        # on the log path — on 2026-08-19 the canary fell into it (three set
        # rejections, then a 201 carrying skills=[]). Same envelope, so the
        # model adds a real prescription and retries in-loop.
        #
        # Exempt the statuses where there is nothing to prescribe: a session the
        # user SKIPPED (or moved, or a rest day) has no sets by definition, and
        # requiring them would leave "I missed leg day" with no expressible
        # payload at all — the very gap the status enum above just closed.
        if (
            category in ("strength", "calisthenics")
            and workout_status not in _NO_PRESCRIPTION_STATUSES
            and not _has_prescription(detail_json)
        ):
            from apps.common.llm_contracts import LLMValidationError

            _emit_fuel_event(
                tenant,
                tool_name="runtime-fuel-log",
                outcome="rejected",
                reason_code="empty_prescription",
                detail={"category": category},
            )
            pres_err = LLMValidationError(
                message=(
                    "Strength and calisthenics workouts require an exercise "
                    "prescription. Add at least one exercise with sets under "
                    "detail_json.exercises before retrying — record the work that "
                    "was actually done, don't drop the category to dodge this and "
                    "don't send an empty exercises list."
                ),
                details=[
                    {
                        "loc": ["detail_json", "exercises"],
                        "msg": "strength/calisthenics workouts require a non-empty exercises list",
                        "type": "missing_prescription",
                        "example": _EMPTY_PRESCRIPTION_EXAMPLE,
                    }
                ],
            )
            return Response(pres_err.as_tool_result(), status=status.HTTP_400_BAD_REQUEST)

        if rpe_clamped:
            _emit_fuel_event(
                tenant,
                tool_name="runtime-fuel-log",
                outcome="normalized",
                reason_code="rpe_clamped",
                detail={"rpe_clamped": True},
            )

        from apps.pii.store_authoring import author_store_fields

        authored, receipts = author_store_fields(
            tenant,
            {
                "activity": activity,
                "notes": data.get("notes", ""),
                "detail_json": detail_json,
            },
            model_label="fuel.Workout",
            seam="fuel.runtime.workout.create",
            writer="runtime",
            defer_detection=True,
        )

        try:
            workout = Workout.objects.create(
                tenant=tenant,
                date=workout_date,
                status=workout_status,
                # Provenance: this path is the assistant logging on the user's
                # behalf from a chat message (any channel). Distinct from a
                # user tapping "log workout" in the app/web (consumer endpoint
                # → source=user) and from Apple Health sync (source=healthkit),
                # so the model can reason about where a session came from.
                source=WorkoutSource.ASSISTANT,
                category=category,
                activity=authored["activity"],
                duration_minutes=duration,
                rpe=rpe,
                notes=authored["notes"],
                detail_json=authored["detail_json"],
                pii_receipts=receipts,
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

        # Echo the STORED rpe (and say so when it was clamped): the assistant
        # otherwise reports back the number it sent, not the number on the row.
        payload = {
            "id": str(workout.id),
            "date": str(workout.date),
            "category": workout.category,
            "activity": workout.activity,
            "status": workout.status,
            "rpe": workout.rpe,
        }
        if rpe_clamped:
            payload["rpe_clamped"] = True
        return Response(payload, status=status.HTTP_201_CREATED)


class RuntimeWorkoutDetailView(_FuelResponseGuard, APIView):
    """GET/PATCH/DELETE a single workout from the AI assistant."""

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

    def get(self, request, tenant_id, workout_id):
        _tenant, workout, err = self._get_workout(request, tenant_id, workout_id)
        if err:
            return err

        payload = {
            **_serialize_workout_summary_card(workout),
            "detail_json": workout.detail_json,
            "status": workout.status,
            "scheduled_at": workout.scheduled_at.isoformat() if workout.scheduled_at else None,
        }
        if workout.plan_id:
            payload["plan_id"] = str(workout.plan_id)
        if workout.slot_id:
            payload["slot_id"] = str(workout.slot_id)
        return Response(payload)

    def patch(self, request, tenant_id, workout_id):
        from django.db import transaction

        tenant, workout, err = self._get_workout(request, tenant_id, workout_id)
        if err:
            return err
        record_runtime_write_activity(tenant)
        lock_resp = _edit_locked_response(workout)
        if lock_resp is not None:
            logger.info("runtime.patch.edit_locked workout=%s", workout_id)
            return lock_resp

        data = request.data
        original_date = workout.date
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
            # Same reject as the create path. Silently ignoring the field here
            # is the mirror-image failure: the assistant reports "marked as
            # missed" off a 200 while the row never moved off "planned".
            if val not in WorkoutStatus.values:
                return _reject_unknown_status(tenant, val, tool_name="runtime-fuel-workout-detail")
            workout.status = val
            updated_fields.append("status")

        if "date" in data:
            try:
                workout.date = date.fromisoformat(str(data["date"]))
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
            from .set_contract import normalize_detail, validate_detail, validate_flat_detail

            nd, ncat = normalize_detail(data["detail_json"], workout.category, activity=workout.activity)[:2]
            nd, verr = validate_detail(nd, ncat)
            if verr is not None:
                return Response(verr.as_tool_result(), status=status.HTTP_400_BAD_REQUEST)
            nd, flat_err = validate_flat_detail(nd, ncat)
            if flat_err is not None:
                _emit_fuel_event(
                    tenant,
                    tool_name="runtime-fuel-workout-detail",
                    outcome="rejected",
                    reason_code="cardio_detail_invalid",
                    detail={"category": ncat, "field": str(flat_err.details[0]["loc"][-1])},
                )
                return Response(flat_err.as_tool_result(), status=status.HTTP_400_BAD_REQUEST)
            workout.detail_json = nd
            updated_fields.append("detail_json")
            if ncat != workout.category:
                workout.category = ncat
                if "category" not in updated_fields:
                    updated_fields.append("category")

        if updated_fields:
            from apps.pii.store_authoring import author_store_fields

            pii_values = {
                field: getattr(workout, field)
                for field in ("activity", "notes", "detail_json")
                if field in updated_fields
            }
            authored, receipts = author_store_fields(
                tenant,
                pii_values,
                model_label="fuel.Workout",
                seam="fuel.runtime.workout.update",
                writer="runtime",
                receipts=workout.pii_receipts,
                defer_detection=True,
            )
            for field, value in authored.items():
                setattr(workout, field, value)
            if pii_values:
                workout.pii_receipts = receipts
                updated_fields.append("pii_receipts")
            updated_fields.append("updated_at")
            try:
                with transaction.atomic():
                    workout.save(update_fields=updated_fields)
                    if workout.date != original_date:
                        PersonalRecord.objects.filter(workout_id=workout.id).update(date=workout.date)
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
        tenant, workout, err = self._get_workout(request, tenant_id, workout_id)
        if err:
            return err
        record_runtime_write_activity(tenant)
        lock_resp = _edit_locked_response(workout)
        if lock_resp is not None:
            logger.info("runtime.delete.edit_locked workout=%s", workout_id)
            return lock_resp
        workout_info = {"id": str(workout.id), "activity": workout.activity, "date": str(workout.date)}
        workout.delete()
        return Response({"deleted": True, **workout_info})


class RuntimeWorkoutSkipView(_FuelResponseGuard, APIView):
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
        record_runtime_write_activity(tenant_or_resp)
        try:
            workout = Workout.objects.get(id=workout_id, tenant=tenant_or_resp)
        except Workout.DoesNotExist:
            return Response({"error": "workout_not_found"}, status=status.HTTP_404_NOT_FOUND)
        lock_resp = _edit_locked_response(workout)
        if lock_resp is not None:
            logger.info("runtime.skip.edit_locked workout=%s", workout_id)
            return lock_resp
        reason = str(request.data.get("reason") or "")[:128]
        from apps.pii.store_authoring import author_store_fields

        authored, receipts = author_store_fields(
            tenant_or_resp,
            {"skip_reason": reason},
            model_label="fuel.Workout",
            seam="fuel.runtime.workout.skip",
            writer="runtime",
            receipts=workout.pii_receipts,
            defer_detection=True,
        )
        workout.status = WorkoutStatus.SKIPPED
        workout.skip_reason = authored["skip_reason"]
        workout.pii_receipts = receipts
        workout.save(update_fields=["status", "skip_reason", "pii_receipts", "updated_at"])
        return Response(
            {
                "id": str(workout.id),
                "status": workout.status,
                "skip_reason": workout.skip_reason,
                "date": str(workout.date),
            }
        )


class RuntimeWorkoutCompleteView(_FuelResponseGuard, APIView):
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
        record_runtime_write_activity(tenant_or_resp)
        try:
            workout = Workout.objects.get(id=workout_id, tenant=tenant_or_resp)
        except Workout.DoesNotExist:
            return Response({"error": "workout_not_found"}, status=status.HTTP_404_NOT_FOUND)
        lock_resp = _edit_locked_response(workout)
        if lock_resp is not None:
            logger.info("runtime.complete.edit_locked workout=%s", workout_id)
            return lock_resp
        workout.status = WorkoutStatus.DONE
        update_fields = ["status", "rpe", "duration_minutes", "updated_at"]
        if "notes" in request.data:
            from apps.pii.store_authoring import author_store_fields

            authored, receipts = author_store_fields(
                tenant_or_resp,
                {"notes": str(request.data.get("notes") or "")},
                model_label="fuel.Workout",
                seam="fuel.runtime.workout.complete",
                writer="runtime",
                receipts=workout.pii_receipts,
                defer_detection=True,
            )
            workout.notes = authored["notes"]
            workout.pii_receipts = receipts
            update_fields.extend(["notes", "pii_receipts"])
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
        workout.save(update_fields=update_fields)
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


class RuntimeWorkoutSwapView(_FuelResponseGuard, APIView):
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
        record_runtime_write_activity(tenant_or_resp)
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
            PersonalRecord.objects.filter(workout_id=a.id).update(date=a.date)
            PersonalRecord.objects.filter(workout_id=b.id).update(date=b.date)
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


class RuntimeFuelSummaryView(_FuelResponseGuard, APIView):
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
        recent_data = [_serialize_workout_summary_card(workout) for workout in recent]

        planned = Workout.objects.filter(tenant=tenant, status="planned").order_by("date")[:10]
        planned_data = [
            {
                "id": str(w.id),
                "date": str(w.date),
                "category": w.category,
                "activity": w.activity,
                "duration_minutes": w.duration_minutes,
                # Prescribed intensity (the plan's target_rpe, stored on the row).
                # Echo it back so a later session can see the intensity it set —
                # without this the assistant is blind to its own prescription.
                # null = no target prescribed (NOT an easy day).
                "rpe": w.rpe,
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
        from .services import plan_progress_fields, rest_dates_for_window

        today = today_in_tenant_tz(tenant)
        active_plans = list(WorkoutPlan.objects.filter(tenant=tenant, status=PlanStatus.ACTIVE)[:3])
        plans_data = []
        for p in active_plans:
            total = Workout.objects.filter(plan=p).count()
            done = Workout.objects.filter(plan=p, status=WorkoutStatus.DONE).count()
            plans_data.append(
                {
                    "id": str(p.id),
                    "name": p.name,
                    # The plan's through-line (desired outcome), e.g. "Run a
                    # sub-25 5K". Echoed back so the assistant can program toward
                    # the objective it set, not just the workout labels.
                    "objective": p.objective,
                    "start_date": str(p.start_date),
                    "weeks": p.weeks,
                    "days_per_week": p.days_per_week,
                    "workout_count": total,
                    "completed_count": done,
                    # Additive program-progress: end_date / days_remaining / current_week.
                    **plan_progress_fields(p, today),
                }
            )

        # rest_today — is today a PROGRAMMED rest day (an active plan covers it
        # but prescribes no session on this weekday) with no real row logged? A
        # programmed rest day is on-plan adherence, not a gap — surfaced here
        # alongside planned_workouts so a "what's today?" turn frames it right.
        rest_today = bool(rest_dates_for_window(tenant, today, today, plans=active_plans)) and not (
            Workout.objects.filter(tenant=tenant, date=today).exists()
        )

        # Computed 4-week aggregates (volume, frequency-by-activity, recency,
        # recent PRs) so a deep-dive answers off trends, not just the raw row
        # list above — the same digest the always-on USER.md fuel section shows.
        from .services import all_time_prs, monthly_volume_12mo, open_goals, weekly_trends

        trends = weekly_trends(tenant)

        # Deeper history + typed goals so the assistant's view matches the app's
        # (which shows a full year), not just the 28-day ``trends`` window:
        #   • all_time_prs        — lifetime PR list (same as the human PR feed)
        #   • monthly_volume_12mo — 12 months of session/volume datapoints
        #   • open_goals          — the user's typed FuelGoal targets
        # All three are kept compact because this payload enters model context.
        return Response(
            {
                "recent_workouts": recent_data,
                "planned_workouts": planned_data,
                "rest_today": rest_today,
                "active_plans": plans_data,
                "latest_body_weight": weight_data,
                "latest_sleep": sleep_data,
                "latest_resting_hr": rhr_data,
                "trends": trends,
                "all_time_prs": all_time_prs(tenant),
                "monthly_volume_12mo": monthly_volume_12mo(tenant),
                "open_goals": open_goals(tenant),
                "profile": profile_data,
            }
        )


_PREFERRED_DAYS_HINT = (
    'weekday names ("monday".."sunday", or "mon".."sun"), or the legacy integer '
    "index 0-6 (0=Mon..6=Sun) as a number or a numeric string"
)


def _normalize_preferred_days(tenant, raw):
    """Resolve ``preferred_days`` to a sorted list of weekday indices.

    Returns ``(days, error_response)``. Accepts names, abbreviations, ints and
    int-strings through the SAME :data:`WEEKDAY_INDEX` map the schedule keys
    use — one map, so the two surfaces can never disagree about what "wed"
    means.

    The old filter was ``isinstance(d, int)``, which silently DROPPED every
    value it did not recognise: ``["1", "3"]`` — a perfectly ordinary thing for
    a model to send — stored ``[]`` and wiped the user's stated training days
    with a 200 and no complaint. Anything unrecognised is now a 400 naming the
    accepted forms; a reduced or emptied list is never stored on the quiet.
    An explicit ``[]`` still clears the preference, because that is the caller
    saying so rather than the parser giving up.

    ``isinstance(True, int)`` is True in Python, so a bool would have sailed
    through the old filter as day 1; ``_normalize_weekday_key`` stringifies
    first, so "true" fails both lookups and is rejected here.
    """
    from apps.common.llm_contracts import LLMValidationError

    def _reject(message, details):
        _emit_fuel_event(
            tenant,
            tool_name="runtime-fuel-profile",
            outcome="rejected",
            reason_code="preferred_days_invalid",
        )
        return None, Response(
            LLMValidationError(message=message, details=details).as_tool_result(),
            status=status.HTTP_400_BAD_REQUEST,
        )

    if not isinstance(raw, list):
        return _reject(
            f"preferred_days must be a list of {_PREFERRED_DAYS_HINT}.",
            [{"loc": ["preferred_days"], "msg": "must be a list", "type": "list_type"}],
        )

    days: list[int] = []
    rejected: list[str] = []
    for idx, value in enumerate(raw):
        day_int, key_err = _normalize_weekday_key(value)
        if key_err is not None or day_int is None:
            rejected.append(str(value)[:24])
            continue
        if day_int not in days:
            days.append(day_int)

    if rejected:
        return _reject(
            f"preferred_days contains values this contract does not accept: "
            f"{', '.join(repr(v) for v in rejected)}. Use {_PREFERRED_DAYS_HINT}. "
            "Send the complete list you want stored — it replaces the previous one.",
            [
                {
                    "loc": ["preferred_days", idx],
                    "msg": f"'{value}' is not a weekday — use {_PREFERRED_DAYS_HINT}",
                    "type": "invalid_weekday",
                }
                for idx, value in enumerate(rejected)
            ],
        )

    if days:
        # Names are the contract; ints are legacy-but-legal. Recording the split
        # is how we learn when the integer surface has actually gone quiet.
        style = _weekday_key_style(raw)
        _emit_fuel_event(
            tenant,
            tool_name="runtime-fuel-profile",
            outcome="accepted" if style == "name" else "normalized",
            reason_code="" if style == "name" else "preferred_days_coerced",
            detail={"preferred_days_style": style},
        )
    return sorted(days), None


class RuntimeFuelProfileView(_FuelResponseGuard, APIView):
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

        blocked = assert_write_allowed_for_document_turn(tenant)
        if blocked is not None:
            return blocked

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

        if "preferred_days" in data:
            cleaned, pd_err = _normalize_preferred_days(tenant, data["preferred_days"])
            if pd_err is not None:
                return pd_err
            profile.preferred_days = cleaned
            updated_fields.append("preferred_days")

        if "preferred_time" in data:
            val = str(data["preferred_time"]).strip().lower()
            if val in {"morning", "afternoon", "evening", ""}:
                profile.preferred_time = val
                updated_fields.append("preferred_time")

        if updated_fields:
            from apps.pii.store_authoring import author_store_fields

            pii_values = {
                field: getattr(profile, field)
                for field in ("additional_context", "limitations")
                if field in updated_fields
            }
            authored, receipts = author_store_fields(
                tenant,
                pii_values,
                model_label="fuel.FuelProfile",
                seam="fuel.runtime.profile.update",
                writer="runtime",
                receipts=profile.pii_receipts,
                defer_detection=True,
            )
            for field, value in authored.items():
                setattr(profile, field, value)
            if pii_values:
                profile.pii_receipts = receipts
                updated_fields.append("pii_receipts")
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

        blocked = assert_write_allowed_for_document_turn(tenant)
        if blocked is not None:
            return blocked

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
        record_runtime_write_activity(tenant)

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

        blocked = assert_write_allowed_for_document_turn(tenant)
        if blocked is not None:
            return blocked

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

        from apps.pii.store_authoring import author_store_fields

        authored, receipts = author_store_fields(
            tenant,
            {"notes": str(data.get("notes", "")).strip()},
            model_label="fuel.SleepLog",
            seam="fuel.runtime.sleep.upsert",
            writer="runtime",
            defer_detection=True,
        )
        entry, created = SleepLog.objects.update_or_create(
            tenant=tenant,
            date=sleep_date,
            defaults={
                "duration_hours": duration,
                "quality": quality,
                "notes": authored["notes"],
                "pii_receipts": receipts,
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


def _serialize_plan(plan, include_workouts=False, *, today=None):
    """Serialize a WorkoutPlan with optional workout list.

    ``today`` is the tenant-local date backing the derived program-progress
    fields (``end_date`` / ``days_remaining`` / ``current_week``). Callers
    serializing several plans compute it once and pass it in; when omitted it is
    resolved from the plan's tenant (one extra query — fine for a single plan).
    """
    from .services import plan_progress_fields

    if today is None:
        today = today_in_tenant_tz(plan.tenant)
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
        # Additive program-progress: end_date (inclusive last day), days_remaining
        # (0 once over), current_week (1-based). All off the tenant-local today.
        **plan_progress_fields(plan, today),
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


_EMPTY_PRESCRIPTION_EXAMPLE = {
    "exercises": [{"name": "Bench Press", "sets": [{"type": "weighted_reps", "reps": 5, "weight": 60}]}]
}


def _has_prescription(detail) -> bool:
    """True when ``detail`` carries at least one exercise (or calisthenics
    ``skills``) entry.

    Used to reject strength/calisthenics plan days whose normalized
    ``detail_json`` would expand into a planned Workout with no exercises at
    all — the empty-plan bug the iOS Fuel tab surfaces (activity name shown, but
    zero exercises to do).
    """
    if not isinstance(detail, dict):
        return False
    return any(isinstance(detail.get(key), list) and detail.get(key) for key in ("exercises", "skills"))


_WEEKDAY_KEY_HINT = "a weekday name (monday..sunday, or mon..sun) or a legacy integer 0-6 (0=Mon..6=Sun)"


def _normalize_weekday_key(day_key) -> tuple[int | None, str | None]:
    """Resolve one weekday key to a canonical index (Monday=0..Sunday=6).

    Accepts weekday NAMES ("wednesday", "wed" — case- and whitespace-
    insensitive) alongside the legacy integer strings "0".."6". Names are what
    the tool contract now leads with, because an integer index is silently
    corruptible: three weekday-numbering conventions coexist in this product
    (Python Mon=0, ISO Mon=1, cron Sun=0), and on 2026-08-19 the model passed
    "3" for a Wednesday — a perfectly legal key that landed the user's workout
    on Thursday. A name has no competing convention to slip into.

    Returns ``(weekday_int, None)`` on success, ``(None, error_detail)`` on a
    key this contract does not recognise.
    """
    token = str(day_key).strip().lower()
    if token in WEEKDAY_INDEX:
        return WEEKDAY_INDEX[token], None
    try:
        day_int = int(token)
    except (TypeError, ValueError):
        return None, f"weekday key '{day_key}' must be {_WEEKDAY_KEY_HINT}"
    if day_int < 0 or day_int > 6:
        return None, f"weekday key '{day_key}' out of range — use {_WEEKDAY_KEY_HINT}"
    return day_int, None


def _validate_normalize_remove_days(remove_days):
    """Normalize explicit plan-day removals to canonical string keys."""
    from apps.common.llm_contracts import LLMValidationError

    if not isinstance(remove_days, list):
        err = LLMValidationError(
            message=f"remove_days must be a list of {_WEEKDAY_KEY_HINT}.",
            details=[
                {
                    "loc": ["remove_days"],
                    "msg": "must be a list",
                    "type": "list_type",
                }
            ],
        )
        return None, Response(err.as_tool_result(), status=status.HTTP_400_BAD_REQUEST)

    normalized: list[str] = []
    seen: dict[int, object] = {}
    for index, raw_day in enumerate(remove_days):
        day_int, key_err = _normalize_weekday_key(raw_day)
        if key_err is not None or day_int is None:
            err = LLMValidationError(
                message=f"remove_days contains an invalid weekday. Use {_WEEKDAY_KEY_HINT}.",
                details=[
                    {
                        "loc": ["remove_days", index],
                        "msg": key_err or "invalid weekday",
                        "type": "invalid_weekday",
                    }
                ],
            )
            return None, Response(err.as_tool_result(), status=status.HTTP_400_BAD_REQUEST)
        if day_int in seen:
            err = LLMValidationError(
                message=(
                    f"remove_days values '{seen[day_int]}' and '{raw_day}' both mean "
                    f"{WEEKDAY_NAMES[day_int]}. Send each day once."
                ),
                details=[
                    {
                        "loc": ["remove_days", index],
                        "msg": "duplicate weekday after normalization",
                        "type": "duplicate_weekday",
                    }
                ],
            )
            return None, Response(err.as_tool_result(), status=status.HTTP_400_BAD_REQUEST)
        seen[day_int] = raw_day
        normalized.append(str(day_int))
    return normalized, None


def _implicit_schedule_removal_error(day_keys):
    """Self-correcting response for schedule_json removal attempts."""
    from apps.common.llm_contracts import LLMValidationError

    day_names = [WEEKDAY_NAMES[int(day)] for day in sorted(day_keys, key=int)]
    err = LLMValidationError(
        message=(
            "schedule_json merges; to drop days pass remove_days or replace_schedule. "
            f"Do not set merged schedule days to null; remove explicitly instead: {', '.join(day_names)}."
        ),
        details=[
            {
                "loc": ["schedule_json", day_name],
                "msg": "explicit removal requires remove_days or replace_schedule",
                "type": "explicit_removal_required",
            }
            for day_name in day_names
        ],
    )
    payload = dict(err.as_tool_result())
    payload["remove_days"] = day_names
    return Response(payload, status=status.HTTP_400_BAD_REQUEST)


def _schedule_remove_collision_error(day_keys):
    """Reject a request that both defines and explicitly removes a day."""
    from apps.common.llm_contracts import LLMValidationError

    day_names = [WEEKDAY_NAMES[int(day)] for day in sorted(day_keys, key=int)]
    err = LLMValidationError(
        message=(
            "schedule_json and remove_days cannot target the same day. "
            f"Choose either update or removal for: {', '.join(day_names)}."
        ),
        details=[
            {
                "loc": ["remove_days", day_name],
                "msg": "day is also defined in schedule_json",
                "type": "schedule_remove_conflict",
            }
            for day_name in day_names
        ],
    )
    return Response(err.as_tool_result(), status=status.HTTP_400_BAD_REQUEST)


def _normalize_stored_schedule_keys(schedule_json):
    """Canonicalize legacy stored weekday names before merge/reconciliation."""
    normalized = {}
    for raw_key, day_def in (schedule_json or {}).items():
        day_int, key_err = _normalize_weekday_key(raw_key)
        if key_err is None and day_int is not None:
            normalized[str(day_int)] = day_def
    return normalized


def _validate_normalize_schedule(schedule_json, *, require_detail=True):
    """Validate weekday keys + normalize/validate each day's prescription.

    Returns ``(normalized_schedule, error_response)``. On any problem
    ``normalized_schedule`` is None and ``error_response`` is a 400 — carrying
    the ``LLMValidationError`` envelope when a strength/calisthenics
    ``detail_json`` is the culprit, so the agent self-corrects in-loop (the same
    chokepoint the log-workout path uses). Atomic by design: the caller persists
    nothing unless the whole schedule validates.

    ``require_detail`` (default True, the create path) additionally rejects any
    strength/calisthenics day whose prescription is empty — even when the caller
    supplied no ``detail_json`` at all — because a fresh plan expands every day
    into a brand-new Workout, and an empty strength day means the user opens it
    to no exercises. On the update path pass ``require_detail=False``: there a
    day that OMITS ``detail_json`` is a "leave the existing prescription alone"
    signal (the caller strips the injected empty key), so only a day that
    explicitly supplied an empty ``detail_json`` is rejected.
    """
    from .set_contract import normalize_detail, validate_detail

    normalized: dict = {}
    seen_keys: dict[int, str] = {}
    for day_str, workout_def in schedule_json.items():
        day_int, key_err = _normalize_weekday_key(day_str)
        if key_err is not None:
            return None, Response(
                {"error": "invalid_schedule", "detail": key_err},
                status=status.HTTP_400_BAD_REQUEST,
            )
        # Names and integers both normalize onto one canonical key, so the same
        # weekday can arrive twice under two spellings ("2" and "wednesday").
        # Silently letting the last one win would drop a whole training day.
        if day_int in seen_keys:
            return None, Response(
                {
                    "error": "invalid_schedule",
                    "detail": (
                        f"weekday keys '{seen_keys[day_int]}' and '{day_str}' both mean "
                        f"{WEEKDAY_NAMES[day_int]} — send each training day exactly once"
                    ),
                },
                status=status.HTTP_400_BAD_REQUEST,
            )
        seen_keys[day_int] = day_str
        if not isinstance(workout_def, dict):
            return None, Response(
                {"error": "invalid_schedule", "detail": f"day {day_str} value must be a workout object"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        category = workout_def.get("category", "other")
        if category not in WorkoutCategory.values:
            category = "other"
        activity = str(workout_def.get("activity") or WorkoutCategory(category).label).strip()

        detail_supplied = "detail_json" in workout_def
        detail = workout_def.get("detail_json", {}) or {}
        detail, category = normalize_detail(detail, category, activity=activity)[:2]
        detail, verr = validate_detail(detail, category)
        if verr is not None:
            payload = dict(verr.as_tool_result())
            payload["weekday"] = day_int
            payload["weekday_name"] = WEEKDAY_NAMES[day_int]
            return None, Response(payload, status=status.HTTP_400_BAD_REQUEST)

        # A strength/calisthenics day with no exercises passes validate_detail
        # (it only checks sets that ARE present) but expands into a planned
        # Workout with nothing to do — the empty-plan the iOS Fuel tab surfaces.
        # Reject it in the same self-correction envelope the malformed-set path
        # uses so the agent adds a real prescription and retries in-loop. Skip
        # days that merely omitted detail_json on the update path
        # (require_detail=False): those mean "leave the existing plan alone" and
        # the caller strips the injected empty key — enforcing here would wedge
        # a status/duration-only edit of a legacy plan.
        if (
            category in ("strength", "calisthenics")
            and (require_detail or detail_supplied)
            and not _has_prescription(detail)
        ):
            from apps.common.llm_contracts import LLMValidationError

            pres_err = LLMValidationError(
                message=(
                    "Strength and calisthenics training days require an exercise "
                    "prescription. Add at least one exercise with sets under "
                    "detail_json.exercises before retrying — design the real "
                    "programming for the day, don't drop the category to dodge this."
                ),
                details=[
                    {
                        "loc": ["schedule_json", WEEKDAY_NAMES[day_int], "detail_json", "exercises"],
                        "msg": "strength/calisthenics days require a non-empty exercises list",
                        "type": "missing_prescription",
                        "example": _EMPTY_PRESCRIPTION_EXAMPLE,
                    }
                ],
            )
            payload = dict(pres_err.as_tool_result())
            payload["weekday"] = day_int
            payload["weekday_name"] = WEEKDAY_NAMES[day_int]
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


def _validate_normalize_week_overrides(week_overrides, *, weeks=None, tenant=None, tool_name=""):
    """Validate the per-week progression/deload map.

    Keys are 0-indexed week offsets ABSOLUTE to the plan ("0" is always the
    plan's first week, never "the first week left"); values are partial
    schedule overrides merged over the base template for that week. A day
    mapped to ``null`` means "rest this week" (drop the base day). Returns
    ``(normalized, error_response)``; on error ``normalized`` is None.

    ``weeks`` bound-checks the keys against the plan's real length. Without it
    a deload written for "week 9" of a 4-week plan was accepted, stored, and
    echoed back in the response — so the model saw its deload confirmed while
    the calendar never contained one, and nothing in the plan ever mentioned
    the discrepancy. Left None (the default) the bound check is skipped, for
    callers that do not know the week count.
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
        if weeks is not None and wk >= weeks:
            _emit_fuel_event(
                tenant,
                tool_name=tool_name or "runtime-fuel-plans",
                outcome="rejected",
                reason_code="week_override_out_of_range",
                detail={"weeks": weeks, "week_key": wk},
            )
            return None, Response(
                {
                    "error": "invalid_week_overrides",
                    "detail": (
                        f"week key '{wk_str}' is outside this plan: valid keys are 0-{weeks - 1} "
                        f"({weeks} week{'' if weeks == 1 else 's'} total, 0 = the plan's FIRST week). "
                        "Either renumber the override or extend the plan's weeks."
                    ),
                },
                status=status.HTTP_400_BAD_REQUEST,
            )
        if not isinstance(override, dict):
            return None, Response(
                {"error": "invalid_week_overrides", "detail": f"week {wk} value must be an object"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        day_defs: dict = {}
        rest_days: dict = {}
        seen_keys: dict[int, str] = {}
        for day_str, val in override.items():
            day_int, key_err = _normalize_weekday_key(day_str)
            if key_err is not None:
                return None, Response(
                    {"error": "invalid_week_overrides", "detail": key_err},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            # Same collision as the base template, and worse here: a duplicate
            # split across the rest-day and day-def buckets would merge with the
            # rest day winning, silently deleting a session the caller asked for.
            if day_int in seen_keys:
                return None, Response(
                    {
                        "error": "invalid_week_overrides",
                        "detail": (
                            f"week {wk}: weekday keys '{seen_keys[day_int]}' and '{day_str}' both mean "
                            f"{WEEKDAY_NAMES[day_int]} — send each weekday exactly once"
                        ),
                    },
                    status=status.HTTP_400_BAD_REQUEST,
                )
            seen_keys[day_int] = day_str
            if val is None:
                rest_days[str(day_int)] = None
            else:
                day_defs[str(day_int)] = val

        norm_days, err = _validate_normalize_schedule(day_defs) if day_defs else ({}, None)
        if err is not None:
            return None, err
        normalized[str(wk)] = {**norm_days, **rest_days}

    return normalized, None


def _author_plan_expansion_inputs(
    tenant, schedule_json, weeks, week_overrides=None, *, writer: str, week_index_base: int = 0
):
    """Author every registered child-Workout value before the DB transaction.

    ``week_index_base`` is the ABSOLUTE plan-week index that this expansion's
    first iteration corresponds to, and must match the base
    :func:`_expand_plan_workouts` derives from its ``start_date``. It is what
    keeps ``week_overrides`` (which are keyed by absolute plan week) resolving
    to the same weeks in both functions, and it keys the returned dict, so a
    mid-plan regen cannot look up an entry this function never authored.
    """
    from apps.pii.store_authoring import author_store_fields

    week_overrides = week_overrides or {}
    authored_workouts = {}
    for week_offset in range(weeks):
        week_idx = week_index_base + week_offset
        override = week_overrides.get(str(week_idx))
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
            if not 0 <= day_int <= 6 or not isinstance(workout_def, dict):
                continue
            category = workout_def.get("category", "other")
            if category not in WorkoutCategory.values:
                category = "other"
            authored_workouts[(week_idx, day_int)] = author_store_fields(
                tenant,
                {
                    "activity": str(workout_def.get("activity", WorkoutCategory(category).label)).strip(),
                    "detail_json": workout_def.get("detail_json", {}),
                },
                model_label="fuel.Workout",
                seam=f"fuel.{writer}.plan.expand",
                writer=writer,
                defer_detection=writer == "runtime",
            )
    return authored_workouts


def _expand_plan_workouts(
    plan,
    tenant,
    schedule_json,
    start_date,
    weeks,
    week_overrides=None,
    *,
    authored_workouts,
):
    """Create planned Workout rows + matching PlanSlot rows from a schedule.

    Each workout gets its ``slot`` FK set so the reconciler (Phase 5) can
    later mutate slots in place without tombstoning workout uuids.

    ``week_overrides`` (0-indexed ABSOLUTE plan-week -> partial schedule)
    applies per-week progression/deload: each override is merged over the base
    template for that week, with a day mapped to ``None`` dropped (rest).
    Inputs are assumed already normalized by ``_validate_normalize_*``.

    Overrides are resolved by ``week_idx`` — the absolute plan week — NOT by
    the loop offset. The two are equal only when ``start_date`` is the plan's
    own start; on a mid-plan regen (``start_date`` = today, ``weeks`` = the
    weeks that remain) the offset restarts at 0 while the plan is in, say,
    week 3, so keying overrides by the offset would silently re-anchor a
    week-1 deload onto the first REMAINING week. ``_author_plan_expansion_inputs``
    must be called with a matching ``week_index_base`` so its keys line up.

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

        override = week_overrides.get(str(week_idx))
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

            authored, receipts = authored_workouts[(week_idx, day_int)]
            Workout.objects.create(
                tenant=tenant,
                plan=plan,
                slot=slot,
                date=workout_date,
                status=WorkoutStatus.PLANNED,
                category=category,
                activity=authored["activity"],
                duration_minutes=workout_def.get("duration_minutes"),
                rpe=workout_def.get("target_rpe"),
                detail_json=authored["detail_json"],
                pii_receipts=receipts,
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
        from apps.orchestrator.fuel_cron import _FUEL_RESERVED_NAMES, _desired_fuel_crons
        from apps.orchestrator.services import _extract_cron_jobs
    except ImportError:
        logger.warning("Could not import gateway_client or fuel_cron for fuel cron")
        return

    # Session-scheduling tenants own the entire ``_fuel:*`` namespace via the
    # per-session reconciler (apps/orchestrator/fuel_cron.py), driven by
    # Workout post_save/post_delete signals (this PATCH already mutates Workout
    # rows through apply_reconciliation, which fires them). The legacy
    # plan-name path must NOT run for them: its additive ``cron.add`` mints a
    # ``_fuel:{plan.name}`` the reconciler treats as legacy, and its
    # "remove every _fuel:*" sweep deletes the live session crons on every
    # edit — the dual-writer collision that accumulated duplicate fuel crons.
    # (Seed-path emission is already suppressed in build_cron_seed_jobs; this
    # closes the runtime-CRUD gap. Legacy orphans on session tenants are reaped
    # by the reconciler's namespace ownership.)
    try:
        if FuelProfile.objects.get(tenant=tenant).use_session_scheduling:
            return
    except FuelProfile.DoesNotExist:
        pass

    try:
        if action in ("create", "remove", "update"):
            # Sweep existing _fuel:* cron(s) before (re)creating. "create" is
            # swept too so it is IDEMPOTENT — the pre-fix additive create left
            # a _fuel:{plan.name} behind on every call (and renames stranded
            # old-named crons), which is how the duplicate pile accumulated.
            # After the sweep, the "create"/"update" block below re-adds the
            # single canonical cron. The hourly fuel reconciler is the
            # fleet-wide backstop for anything that slips.
            try:
                result = invoke_gateway_tool(tenant, "cron.list", {"includeDisabled": True})
                # Use the canonical extractor: the gateway wraps jobs in
                # ``{"details": {"jobs": [...]}}``, which the old ad-hoc
                # ``result.get("jobs")`` missed — so the sweep silently removed
                # nothing on the real gateway, another reason orphans persisted.
                existing = _extract_cron_jobs(result) or []
                for job in existing:
                    name = str(job.get("name", "")) if isinstance(job, dict) else ""
                    # Sweep workout-plan crons only; leave reserved names
                    # (e.g. _fuel:welcome, owned by the welcome scheduler)
                    # alone — symmetric with the reconciler's exclusion.
                    if name.startswith("_fuel:") and name not in _FUEL_RESERVED_NAMES:
                        job_id = job.get("id") or job.get("jobId")
                        if not job_id:
                            logger.warning(
                                "Cannot remove fuel cron %s for tenant %s: missing gateway job ID",
                                name,
                                tenant.id,
                            )
                            continue
                        invoke_gateway_tool(tenant, "cron.remove", {"jobId": job_id})
            except GatewayError:
                logger.warning("Failed to remove fuel cron for tenant %s", tenant.id)

        if action in ("create", "update"):
            # Re-add the CANONICAL desired set — the most-recent active plan's
            # cron, the exact same selection the hourly reconciler and the seed
            # path use (apps/orchestrator/fuel_cron._desired_fuel_crons), NOT
            # the request's `plan`. If a tenant ever holds >1 active plan,
            # editing an older one must not re-add its cron only to have the
            # reconciler reap it an hour later: both writers now resolve the
            # same single owner, so they can never disagree. Returns [] (no add)
            # when there is no active plan.
            for job_dict in _desired_fuel_crons(tenant):
                invoke_gateway_tool(tenant, "cron.add", {"job": job_dict})
                logger.info("Created fuel cron '%s' for tenant %s", job_dict.get("name"), tenant.id)

    except GatewayError:
        logger.warning("Fuel cron %s failed for tenant %s (best-effort)", action, tenant.id)
    except Exception:
        logger.exception("Unexpected error managing fuel cron for tenant %s", tenant.id)


def _supersede_other_active_plans(tenant, keep_plan) -> list[str]:
    """Single-active-plan invariant: archive every OTHER active plan for the
    tenant and drop its PLANNED workouts (completed sessions are kept as history,
    still linked to the archived plan). Same teardown contract as deleting a
    plan (``delete()`` on planned rows, no date filter — a superseded program's
    remaining sessions, missed or upcoming, no longer apply). Returns the
    archived plan names so the response can tell the assistant what it replaced.

    This is the backend safety net behind the "one active plan" model: even if
    the assistant misreads "change my plan" as "make a new one", the prior plan
    can't linger active and strand a duplicate prep cron (the duplicate-fuel-cron
    class). A user who genuinely wants two concurrent programs opts in explicitly
    (``concurrent=true``), which skips this.
    """
    from django.db import transaction

    archived: list[str] = []
    with transaction.atomic():
        others = WorkoutPlan.objects.filter(tenant=tenant, status=PlanStatus.ACTIVE).exclude(id=keep_plan.id)
        for p in others:
            Workout.objects.filter(plan=p, status=WorkoutStatus.PLANNED).delete()
            p.status = PlanStatus.ARCHIVED
            p.save(update_fields=["status", "updated_at"])
            archived.append(p.name)
    if archived:
        logger.info(
            "fuel.superseded_active_plans tenant=%s kept=%s archived=%s",
            tenant.id,
            keep_plan.name,
            archived,
        )
    return archived


def _plan_start_metadata(plan: WorkoutPlan) -> dict[str, str | None]:
    """Describe where a plan's materialized calendar actually starts."""
    workouts = Workout.objects.filter(plan=plan)
    first_workout_date = workouts.order_by("date").values_list("date", flat=True).first()
    start_date_note = None
    if not workouts.filter(date=plan.start_date).exists():
        if first_workout_date is None:
            start_date_note = (
                f"start_date {plan.start_date.isoformat()} is not a training day in the cadence; "
                "no sessions were materialized"
            )
        else:
            start_date_note = (
                f"start_date {plan.start_date.isoformat()} is not a training day in the cadence; "
                f"first session is {first_workout_date.isoformat()}"
            )
    return {
        "first_workout_date": first_workout_date.isoformat() if first_workout_date else None,
        "start_date_note": start_date_note,
    }


def _reject_start_today_without_session(tenant, plan_start, normalized_schedule, normalized_overrides):
    """400 when a plan starting TODAY has no session on today's weekday.

    A hard reject, not advice. On 2026-08-19 the model asked for a workout
    "today" (Wednesday), sent the correct ``start_date`` with weekday key "3"
    (Thursday under ISO/cron numbering), and the session landed a day late. The
    advisory ``start_date_note`` from :func:`_plan_start_metadata` was returned
    and never read; hard 400s out of the schedule validator, in that same
    transcript, DID drive the model to self-correct. So the start-anchor rule
    that ``rules/fuel.md`` has always declared is enforced here instead of
    asked for.

    Scope is deliberately narrow — only ``start_date == today`` in the tenant's
    timezone. "Start next Monday" with any cadence stays legal, and an
    off-cadence FUTURE start keeps the advisory note (the caller may genuinely
    want the program to begin at the next matching weekday).
    """
    if plan_start != today_in_tenant_tz(tenant):
        return None

    wanted = plan_start.weekday()
    name = WEEKDAY_NAMES[wanted]
    # Mirror the week-0 merge in _expand_plan_workouts: an override can add the
    # day the base template lacks, or rest a day the base template has.
    override_0 = (normalized_overrides or {}).get("0")
    if isinstance(override_0, dict) and str(wanted) in override_0:
        has_session = override_0[str(wanted)] is not None
    else:
        has_session = str(wanted) in (normalized_schedule or {})
    if has_session:
        return None

    from apps.common.llm_contracts import LLMValidationError

    _emit_fuel_event(
        tenant,
        tool_name="runtime-fuel-plans",
        outcome="rejected",
        reason_code="start_today_reject",
        detail={"start_today_reject": True},
    )
    start_err = LLMValidationError(
        message=(
            f"This plan starts TODAY ({plan_start.isoformat()}, a {name.capitalize()}), but "
            f"schedule_json has no {name} session — the first workout would land on a later day, "
            f'which is not what the user asked for. Add a "{name}" day to schedule_json and '
            "rotate the split so today is day 1, then retry. Weekday keys are NAMES "
            "(monday..sunday); do not translate the day into an index."
        ),
        details=[
            {
                "loc": ["schedule_json", name],
                "msg": f"start_date {plan_start.isoformat()} is a {name}, but schedule_json has no {name} day",
                "type": "missing_start_weekday",
                "example": {
                    name: {
                        "category": "strength",
                        "activity": "Full Body",
                        "detail_json": _EMPTY_PRESCRIPTION_EXAMPLE,
                    }
                },
            }
        ],
    )
    payload = dict(start_err.as_tool_result())
    payload["weekday"] = wanted
    payload["weekday_name"] = name
    payload["start_date"] = plan_start.isoformat()
    return Response(payload, status=status.HTTP_400_BAD_REQUEST)


class RuntimeWorkoutPlanListCreateView(_FuelResponseGuard, APIView):
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

        today = today_in_tenant_tz(tenant)
        return Response({"plans": [_serialize_plan(p, today=today) for p in plans]})

    def post(self, request, tenant_id):
        err = _internal_auth_or_401(request, tenant_id)
        if err:
            return err
        tenant_or_resp = _get_tenant_or_404(tenant_id)
        if isinstance(tenant_or_resp, Response):
            return tenant_or_resp
        tenant = tenant_or_resp

        blocked = assert_write_allowed_for_document_turn(tenant)
        if blocked is not None:
            return blocked

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
        _emit_weekday_key_style(tenant, schedule_json.keys(), tool_name="runtime-fuel-plans")

        normalized_overrides, ov_err = _validate_normalize_week_overrides(
            data.get("week_overrides"),
            weeks=weeks,
            tenant=tenant,
            tool_name="runtime-fuel-plans",
        )
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

        # Start-anchor enforcement: a plan the caller anchored on TODAY must
        # train today. Checked before the idempotency short-circuit so a retry
        # of a bad payload can never be waved through as a 200 dedup.
        start_err = _reject_start_today_without_session(tenant, plan_start, normalized_schedule, normalized_overrides)
        if start_err is not None:
            return start_err

        # Single-active-plan invariant: a new plan REPLACES any current active
        # plan unless the caller explicitly asks for concurrent programs. This
        # is the backend half of the model — the assistant confirms intent with
        # the user, but even a misread can't leave two active plans (and a
        # stranded prep cron) behind. ``concurrent=true`` keeps the others.
        concurrent = bool(data.get("concurrent", False))

        # Idempotency: a retried / double-fired create with the same name +
        # start_date returns the existing active plan instead of duplicating it
        # (and its whole calendar of planned workouts). Mirrors the task/goal
        # runtime dedup contract (return 200, not a second 201). Even on this
        # path enforce single-active: the re-asserted plan still supersedes any
        # OTHER active plans — otherwise the multi-active legacy tenants this
        # exists to clean up keep their stragglers when a plan is re-created.
        from apps.journal.lifecycle_views import _search_variants

        name_query = db_models.Q()
        for variant in _search_variants(tenant, name):
            name_query |= db_models.Q(name=variant)  # guard: encrypted-predicate
        existing = WorkoutPlan.objects.filter(
            name_query,
            tenant=tenant,
            start_date=plan_start,
            status=PlanStatus.ACTIVE,
        ).first()
        if existing is not None:
            superseded = [] if concurrent else _supersede_other_active_plans(tenant, existing)
            if superseded:
                _manage_fuel_cron(tenant, existing, action="update")
            result = _serialize_plan(existing, today=today_in_tenant_tz(tenant))
            result.update(_plan_start_metadata(existing))
            result["deduped"] = True
            if superseded:
                result["superseded_plans"] = superseded
            return Response(result, status=status.HTTP_200_OK)

        from apps.pii.store_authoring import author_store_fields

        authored_plan, plan_receipts = author_store_fields(
            tenant,
            {
                "name": name,
                "objective": str(data.get("objective", "")).strip()[:200],
                "notes": str(data.get("notes", "")).strip(),
                "schedule_json": normalized_schedule,
                "week_overrides": normalized_overrides,
            },
            model_label="fuel.WorkoutPlan",
            seam="fuel.runtime.plan.create",
            writer="runtime",
            defer_detection=True,
        )
        authored_workouts = _author_plan_expansion_inputs(
            tenant,
            authored_plan["schedule_json"],
            weeks,
            week_overrides=authored_plan["week_overrides"],
            writer="runtime",
        )

        # Persist the plan row and expand its full calendar of planned workouts
        # in a SINGLE transaction. A mid-loop failure in _expand_plan_workouts
        # must roll back the plan row too — otherwise the retry hits the
        # idempotency short-circuit above (return 200 deduped) before any
        # re-expansion, silently locking in a structurally-incomplete plan.
        from django.db import transaction

        try:
            with transaction.atomic():
                plan = WorkoutPlan.objects.create(
                    tenant=tenant,
                    name=authored_plan["name"],
                    start_date=plan_start,
                    weeks=weeks,
                    days_per_week=days_per_week,
                    schedule_json=authored_plan["schedule_json"],
                    week_overrides=authored_plan["week_overrides"],
                    objective=authored_plan["objective"],
                    notes=authored_plan["notes"],
                    pii_receipts=plan_receipts,
                )
                workouts_created = _expand_plan_workouts(
                    plan,
                    tenant,
                    authored_plan["schedule_json"],
                    plan_start,
                    weeks,
                    week_overrides=authored_plan["week_overrides"],
                    authored_workouts=authored_workouts,
                )
                superseded = [] if concurrent else _supersede_other_active_plans(tenant, plan)
        except Exception as exc:
            logger.exception("WorkoutPlan creation failed for tenant %s", tenant_id)
            return Response(
                {"error": "create_failed", "detail": str(exc)},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Reconcile the fuel cron set to the now-canonical active plans
        # (best-effort). After a supersede this drops the archived plans' crons
        # and keeps only the new plan's.
        _manage_fuel_cron(tenant, plan, action="create")

        result = _serialize_plan(plan, today=today_in_tenant_tz(tenant))
        result.update(_plan_start_metadata(plan))
        result["workouts_created"] = workouts_created
        if superseded:
            result["superseded_plans"] = superseded
        return Response(result, status=status.HTTP_201_CREATED)


class RuntimeWorkoutPlanDetailView(_FuelResponseGuard, APIView):
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

        return Response(_serialize_plan(plan, include_workouts=True, today=today_in_tenant_tz(tenant)))

    def patch(self, request, tenant_id, plan_id):
        err = _internal_auth_or_401(request, tenant_id)
        if err:
            return err
        tenant_or_resp = _get_tenant_or_404(tenant_id)
        if isinstance(tenant_or_resp, Response):
            return tenant_or_resp
        tenant = tenant_or_resp
        record_runtime_write_activity(tenant)

        plan = self._get_plan(tenant, plan_id)
        if not plan:
            return Response({"error": "plan_not_found"}, status=status.HTTP_404_NOT_FOUND)

        data = request.data
        updated_fields = []
        needs_regeneration = False

        if "name" in data:
            plan.name = str(data["name"]).strip()
            updated_fields.append("name")

        prior_status = plan.status
        if "status" in data and data["status"] in PlanStatus.values:
            plan.status = data["status"]
            updated_fields.append("status")

        if "notes" in data:
            plan.notes = str(data["notes"]).strip()
            updated_fields.append("notes")

        if "objective" in data:
            plan.objective = str(data["objective"]).strip()[:200]
            updated_fields.append("objective")

        if "weeks" in data:
            try:
                plan.weeks = max(1, min(52, int(data["weeks"])))
                updated_fields.append("weeks")
                needs_regeneration = True
            except (TypeError, ValueError):
                pass

        normalized_remove_days: list[str] = []
        if "remove_days" in data:
            normalized_remove_days, remove_err = _validate_normalize_remove_days(data["remove_days"])
            if remove_err is not None:
                return remove_err
            _emit_weekday_key_style(tenant, data["remove_days"], tool_name="runtime-fuel-plan-detail")

        schedule_supplied = "schedule_json" in data and isinstance(data["schedule_json"], dict)
        replace_schedule = data.get("replace_schedule") is True
        if schedule_supplied:
            raw_schedule = data["schedule_json"]
            # ``null`` is how week_overrides denotes a rest day, so it is a
            # plausible (but unsafe) attempt to remove a base-template day.
            # Base schedules merge by default: point the caller at the two
            # explicit deletion surfaces before ordinary shape validation. This
            # is the sole implicit-removal guard: every null day gets the same
            # self-correcting response, independent of current workout contents.
            null_days = set()
            for raw_key, raw_val in raw_schedule.items():
                if raw_val is not None:
                    continue
                day_int, key_err = _normalize_weekday_key(raw_key)
                if key_err is None and day_int is not None:
                    null_days.add(str(day_int))
            if null_days:
                return _implicit_schedule_removal_error(null_days)

            # require_detail=False: a day that omits detail_json here means
            # "leave the existing prescription alone" (its injected empty key is
            # stripped below), so only a day that explicitly supplied an empty
            # strength/calisthenics detail_json is rejected — a status/duration
            # edit of a legacy plan must not be retro-wedged.
            normalized_schedule, sched_err = _validate_normalize_schedule(raw_schedule, require_detail=False)
            if sched_err is not None:
                return sched_err
            _emit_weekday_key_style(tenant, raw_schedule.keys(), tool_name="runtime-fuel-plan-detail")
            # _validate_normalize_schedule injects a ``detail_json`` key on every
            # day (empty when none was supplied). On the PATCH/reconcile path the
            # reconciler treats a present-but-empty ``detail_json`` as an explicit
            # "clear it" instruction, which would wipe a workout's existing
            # prescription. Preserve the "silence = leave alone" contract by
            # stripping the injected key for days that supplied no detail_json.
            # Re-key the caller's raw payload onto the canonical weekday keys
            # first: with name keys accepted, ``raw_schedule["wednesday"]`` no
            # longer answers to normalized key "2", and a straight lookup would
            # miss — stripping a detail_json the caller really did send, i.e.
            # silently discarding a prescription edit. Validation above already
            # proved every key resolves.
            raw_by_weekday = {}
            for raw_key, raw_val in raw_schedule.items():
                day_int, _ = _normalize_weekday_key(raw_key)
                if day_int is not None:
                    raw_by_weekday[str(day_int)] = raw_val
            for day_str, day_def in normalized_schedule.items():
                src = raw_by_weekday.get(day_str, {})
                if isinstance(day_def, dict) and "detail_json" not in (src if isinstance(src, dict) else {}):
                    day_def.pop("detail_json", None)

            collisions = set(normalized_schedule) & set(normalized_remove_days)
            if collisions:
                return _schedule_remove_collision_error(collisions)

            # Stored plans predate the name-key normalization contract in a few
            # paths. Canonicalize before copying so the reconciler cannot silently
            # discard a carried-forward name-keyed day.
            current_schedule = _normalize_stored_schedule_keys(plan.schedule_json)
            if replace_schedule:
                effective_schedule = dict(normalized_schedule)
            else:
                effective_schedule = dict(current_schedule)
                category_changed_days = set()
                for day_str, incoming_day in normalized_schedule.items():
                    merged_day = dict(incoming_day)
                    existing_day = current_schedule.get(day_str)
                    if (
                        "detail_json" not in merged_day
                        and isinstance(existing_day, dict)
                        and "detail_json" in existing_day
                    ):
                        merged_day["detail_json"] = existing_day["detail_json"]
                    effective_schedule[day_str] = merged_day
                    if not isinstance(existing_day, dict) or existing_day.get("category", "other") != merged_day.get(
                        "category", "other"
                    ):
                        category_changed_days.add(day_str)

                # Carry-forward happens before this validation. A new day or a
                # category flip must satisfy the same prescription requirement as
                # a fresh template, so mobility detail cannot become an empty
                # strength/calisthenics workout after the first validation pass.
                if category_changed_days:
                    changed_schedule = {day: effective_schedule[day] for day in category_changed_days}
                    validated_changed, changed_err = _validate_normalize_schedule(
                        changed_schedule,
                        require_detail=True,
                    )
                    if changed_err is not None:
                        return changed_err
                    effective_schedule.update(validated_changed)

            for day_str in normalized_remove_days:
                effective_schedule.pop(day_str, None)

            plan.schedule_json = effective_schedule
            updated_fields.append("schedule_json")
            needs_regeneration = True

        elif normalized_remove_days:
            current_schedule = _normalize_stored_schedule_keys(plan.schedule_json)
            for day_str in normalized_remove_days:
                current_schedule.pop(day_str, None)
            plan.schedule_json = current_schedule
            updated_fields.append("schedule_json")
            needs_regeneration = True

        if "week_overrides" in data:
            # Bound against the plan's CURRENT week count — including a "weeks"
            # value this same PATCH just set above, so extending the plan and
            # adding an override for the new final week works in one call.
            normalized_overrides, ov_err = _validate_normalize_week_overrides(
                data["week_overrides"],
                weeks=plan.weeks,
                tenant=tenant,
                tool_name="runtime-fuel-plan-detail",
            )
            if ov_err is not None:
                return ov_err
            plan.week_overrides = normalized_overrides
            updated_fields.append("week_overrides")
            needs_regeneration = True

        if "days_per_week" in data:
            try:
                plan.days_per_week = max(1, min(7, int(data["days_per_week"])))
                updated_fields.append("days_per_week")
            except (TypeError, ValueError):
                pass

        if updated_fields:
            from apps.pii.store_authoring import author_store_fields

            pii_values = {
                field: getattr(plan, field)
                for field in ("name", "notes", "objective", "schedule_json", "week_overrides")
                if field in updated_fields
            }
            authored, receipts = author_store_fields(
                tenant,
                pii_values,
                model_label="fuel.WorkoutPlan",
                seam="fuel.runtime.plan.update",
                writer="runtime",
                receipts=plan.pii_receipts,
                defer_detection=True,
            )
            for field, value in authored.items():
                setattr(plan, field, value)
            if pii_values:
                plan.pii_receipts = receipts
                updated_fields.append("pii_receipts")
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

            rec = reconcile_plan_state(
                plan,
                plan.schedule_json,
                plan.weeks,
                today=today_in_tenant_tz(tenant),
                week_overrides=plan.week_overrides,
            )
            try:
                counts = apply_reconciliation(
                    rec,
                    plan=plan,
                    tenant=tenant,
                    writer="runtime",
                    edit_lock_check=_is_edit_locked,
                )
                logger.info("fuel.plan_reconciled plan=%s counts=%s", plan.id, counts)
            except Exception:
                logger.exception("fuel.plan_reconcile_failed plan=%s", plan.id)
                return Response(
                    {"error": "regen_failed"},
                    status=status.HTTP_500_INTERNAL_SERVER_ERROR,
                )

        # Activating a plan enforces the single-active invariant: archive any
        # OTHER active plan (unless concurrent was explicitly requested). Gate on
        # an actual transition INTO active (prior_status != ACTIVE) — a redundant
        # status='active' on an already-active plan must NOT silently archive a
        # second active plan (and hard-delete its future workouts). Done BEFORE
        # the cron reconcile so the desired set reflects the supersede.
        superseded: list[str] = []
        if (
            "status" in updated_fields
            and plan.status == PlanStatus.ACTIVE
            and prior_status != PlanStatus.ACTIVE
            and not bool(data.get("concurrent", False))
        ):
            superseded = _supersede_other_active_plans(tenant, plan)

        # Manage fuel cron based on status/schedule/name changes (best-effort).
        # A pure rename must reconcile too: the legacy cron name is
        # ``_fuel:{plan.name}``, so renaming without this would strand the
        # old-named cron forever (and add a new one) — a duplicate fire on the
        # legacy (non-session) flow. ``action="update"`` removes every
        # ``_fuel:*`` then re-adds the current one, clearing the orphan.
        # No-op for session tenants (the gate in _manage_fuel_cron returns early).
        if "status" in updated_fields or needs_regeneration or "name" in updated_fields:
            if plan.status == "active":
                _manage_fuel_cron(tenant, plan, action="update")
            else:
                # Paused, completed, archived → remove cron
                _manage_fuel_cron(tenant, plan, action="remove")

        resp = _serialize_plan(plan, include_workouts=True, today=today_in_tenant_tz(tenant))
        if superseded:
            resp["superseded_plans"] = superseded
        return Response(resp)

    def delete(self, request, tenant_id, plan_id):
        err = _internal_auth_or_401(request, tenant_id)
        if err:
            return err
        tenant_or_resp = _get_tenant_or_404(tenant_id)
        if isinstance(tenant_or_resp, Response):
            return tenant_or_resp
        tenant = tenant_or_resp
        record_runtime_write_activity(tenant)

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

        from .services import plan_progress_fields, rest_dates_for_window

        err = _internal_auth_or_401(request, tenant_id)
        if err:
            return err
        tenant_or_resp = _get_tenant_or_404(tenant_id)
        if isinstance(tenant_or_resp, Response):
            return tenant_or_resp
        tenant = tenant_or_resp

        now = datetime.now(tz=UTC)
        # Tenant-local "today". Bare ``date.today()`` is computed in UTC, so a
        # tenant west of UTC in their evening (or east past midnight) would get
        # the wrong day's plan window and daily-note slug. See line ~1248.
        today = today_in_tenant_tz(tenant)
        horizon_14d_end = today + timedelta(days=14)
        horizon_48h_end = now + timedelta(hours=48)

        # 1. today_plan — the daily-note Fuel section when present. ``exists``
        # means specifically "a prep cron already wrote today's section": the
        # cron's idempotence gate depends on that exact meaning, so we must NOT
        # widen it. The Postgres fallback below adds today's scheduled Workout
        # rows as a separate ``workouts`` list (without touching ``exists``), so
        # the guidance can still see a scheduled session the note never captured.
        today_doc = Document.objects.filter(tenant=tenant, kind="daily", slug=str(today)).first()
        today_plan_body = _parse_fuel_section(today_doc.markdown) if today_doc else None
        today_plan = {
            "exists": bool(today_plan_body),
            "iso_date": str(today),
            "raw_section": today_plan_body,
            "workouts": [],
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
                # Prescribed/actual intensity — for a planned row this is the
                # target_rpe the assistant set; for a done row it's the logged
                # RPE. The prep cron reads audit, so surfacing it is also the
                # first rung toward recovery-aware re-tuning. null = unset.
                "rpe": w.rpe,
            }
            for w in next_14d_qs
        ]

        # Active plans, fetched once — they drive both the programmed rest days
        # injected into the 14d horizon here and the ``active_plans`` summary
        # further down. One active-plans query for the whole view.
        active_plan_objs = list(
            WorkoutPlan.objects.filter(tenant=tenant, status=PlanStatus.ACTIVE).order_by("-created_at")
        )

        # Programmed rest days are first-class in the horizon: a date a plan
        # covers but trains no session on, with no real row. Injected as
        # {date, status:"rest", activity:"Rest day"} so a rest day reads as
        # on-plan adherence, never a blank gap. Never on a date with a real row.
        real_horizon_dates = {w["date"] for w in next_14d}
        for rd in sorted(rest_dates_for_window(tenant, today, horizon_14d_end, plans=active_plan_objs)):
            if str(rd) in real_horizon_dates:
                continue
            next_14d.append({"date": str(rd), "status": "rest", "activity": "Rest day"})
        next_14d.sort(key=lambda w: w["date"])

        # today_plan fallback — the daily-note Fuel section is authored only by
        # the prep cron (active plan + training day + cron already fired) and is
        # absent from the default note template, so keying "is there a plan
        # today?" off the section alone was a false negative whenever a session
        # was scheduled but never scraped. Postgres is the source of truth:
        # surface today's Workout rows (already in next_14d) as a separate list
        # the guidance uses, so the audit doesn't report "no plan today" — and
        # invite a duplicate — when one is on the calendar. ``exists`` stays
        # daily-note-only on purpose (the prep cron's idempotence gate).
        today_plan["workouts"] = [w for w in next_14d if w["date"] == str(today)]

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

        # orphan_crons: _fuel:{8-hex} cron whose Workout (by short id) isn't in next_14d.
        # Derived from the queryset (real rows), not the list — the injected rest-day
        # entries carry no ``id`` and must not participate in cron reconciliation.
        next_14d_short_ids = {str(w.id).split("-")[0] for w in next_14d_qs}
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

        # 5. active_plans — the user's current program(s). The assistant must
        # know these BEFORE creating a plan so it can tell the difference
        # between "change my plan" (update the existing one) and "new program"
        # (which replaces it by default), and only run concurrent plans when the
        # user explicitly asks.
        active_plans = [
            {
                "id": str(p.id),
                "name": p.name,
                "status": p.status,
                "start_date": str(p.start_date),
                "weeks": p.weeks,
                "days_per_week": p.days_per_week,
                "objective": p.objective,
                # Additive program-progress: end_date / days_remaining / current_week.
                **plan_progress_fields(p, today),
            }
            for p in active_plan_objs
        ]

        return Response(
            {
                "today_plan": today_plan,
                "active_plans": active_plans,
                "next_14d_workouts": next_14d,
                "fuel_crons": fuel_crons,
                "cron_list_error": cron_list_error,
                "conflicts": {
                    "duplicate_fires": duplicate_fires,
                    "orphan_crons": orphan_crons,
                    "orphan_workouts": orphan_workouts,
                },
                "guidance": _audit_guidance(today_plan, fuel_crons, duplicate_fires, active_plans),
            }
        )


def _audit_guidance(today_plan: dict, fuel_crons: list, duplicate_fires: list, active_plans: list | None = None) -> str:
    """Single-line instruction for the agent based on the audit state.

    Appends a plan-management note whenever the user already has an active plan,
    so the agent surfaces it and disambiguates edit / replace / concurrent
    rather than silently creating a second active plan (the root cause of the
    duplicate-plan + duplicate-cron mess).
    """
    active_plans = active_plans or []
    plan_note = ""
    if active_plans:
        names = ", ".join(f"'{p['name']}'" for p in active_plans)
        if len(active_plans) == 1:
            plan_note = (
                f" PLAN STATE: the user already has an active plan ({names}). If they want to CHANGE it "
                "(swap exercises, more days, rename, deload), UPDATE that plan with nbhd_fuel_update_plan — "
                "do NOT create a new one. If they want a fresh program, creating one REPLACES the current "
                "plan by default (the old one is archived) — tell them that's what you're doing. Only pass "
                "concurrent=true to nbhd_fuel_create_plan if the user explicitly says they want to run two "
                "plans at the same time."
            )
        else:
            plan_note = (
                f" PLAN STATE: the user has {len(active_plans)} concurrent active plans ({names}). Confirm "
                "which one they mean before editing, and remember a plain create replaces ALL of them unless "
                "concurrent=true."
            )

    if duplicate_fires:
        base = (
            "STOP — duplicate cron firings detected. Surface the duplicates to the user "
            "and offer to remove them. Do NOT add more crons until they are resolved."
        )
    elif today_plan.get("exists"):
        base = (
            "today_plan.raw_section is the locked plan description for today. Deliver "
            "THAT plan to the user verbatim — do not invent a different one. To UPDATE "
            "or DELETE today's workout (e.g. swap an exercise, change weights), find "
            "the matching workout_id in next_14d_workouts[i].id (match by date) and "
            "call nbhd_fuel_update_workout or nbhd_fuel_delete_workout directly. "
            "To inspect its full exercises, sets, reps, and metrics first, call "
            "nbhd_fuel_get_workout with that workout_id. "
            "Workout IDs are already in this response — do NOT call nbhd_fuel_summary "
            "just to retrieve them."
        )
    elif today_plan.get("workouts") and all(w.get("status") == "rest" for w in today_plan["workouts"]):
        # Pure programmed rest day: today_plan.workouts holds only the injected
        # rest stub(s), no real Workout row. Without this branch the generic
        # "already on the calendar … deliver the planned session" wording below
        # would fire — instructing the agent to push a session onto a rest day,
        # the exact harm rest days exist to prevent. The injection sites already
        # guarantee rest stubs never coexist with a real row on the same date,
        # but the every-entry-is-rest predicate is the safe gate regardless:
        # any real row present routes to the existing branch below.
        base = (
            "Today is a programmed rest day — part of the user's plan, not a gap "
            "and not a missed session. Do NOT propose or deliver a training "
            "session; acknowledge the rest day and support recovery. If the user "
            "explicitly wants to train anyway, help — but frame it as their "
            "choice against a planned rest day. (Rest entries also appear in "
            "next_14d_workouts with status 'rest'.)"
        )
    elif today_plan.get("workouts"):
        base = (
            "A workout is already on the calendar for today (today_plan.workouts, also "
            "in next_14d_workouts) — the daily note has no Fuel section, but Postgres "
            "is the source of truth. Do NOT treat today as unplanned or propose a fresh "
            "workout over it: deliver the planned session, or acknowledge it if a row is "
            "already done/skipped (check each row's status). Workout IDs are inline for "
            "nbhd_fuel_get_workout / nbhd_fuel_update_workout / nbhd_fuel_delete_workout — do NOT call "
            "nbhd_fuel_summary just to retrieve them."
        )
    else:
        base = (
            "No locked plan for today. Safe to propose one. Before scheduling, check "
            "next_14d_workouts so your proposal fits the existing program. Workout IDs "
            "for any update or delete are in next_14d_workouts[i].id."
        )
    return base + plan_note
