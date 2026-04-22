"""Internal runtime views for the OpenClaw fuel plugin."""

from __future__ import annotations

import logging
from datetime import date
from decimal import Decimal, InvalidOperation
from uuid import UUID

from rest_framework import status
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.integrations.internal_auth import InternalAuthError, validate_internal_runtime_request
from apps.tenants.middleware import set_rls_context
from apps.tenants.models import Tenant

from .models import BodyWeightLog, FuelProfile, OnboardingStatus, Workout, WorkoutCategory, WorkoutStatus

logger = logging.getLogger(__name__)

_PROFILE_FIELDS = ("onboarding_status", "fitness_level", "goals", "limitations", "equipment", "days_per_week", "additional_context")


def _serialize_profile(profile: FuelProfile) -> dict:
    return {f: getattr(profile, f) for f in _PROFILE_FIELDS}


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

        workout = Workout.objects.create(
            tenant=tenant,
            date=data.get("date", str(date.today())),
            status=workout_status,
            category=category,
            activity=data.get("activity", WorkoutCategory(category).label),
            duration_minutes=data.get("duration_minutes"),
            rpe=data.get("rpe"),
            notes=data.get("notes", ""),
            detail_json=data.get("detail_json", {}),
        )
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
        recent_data = [
            {
                "date": str(w.date),
                "category": w.category,
                "activity": w.activity,
                "duration_minutes": w.duration_minutes,
                "rpe": w.rpe,
            }
            for w in recent
        ]

        planned = Workout.objects.filter(tenant=tenant, status="planned").order_by("date")[:10]
        planned_data = [
            {
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

        return Response(
            {
                "recent_workouts": recent_data,
                "planned_workouts": planned_data,
                "latest_body_weight": weight_data,
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

        for field in ("fitness_level", "additional_context"):
            if field in data:
                setattr(profile, field, str(data[field]).strip())
                updated_fields.append(field)

        for field in ("goals", "limitations", "equipment"):
            if field in data and isinstance(data[field], list):
                setattr(profile, field, data[field])
                updated_fields.append(field)

        if "days_per_week" in data:
            val = data["days_per_week"]
            if val is None or (isinstance(val, int) and 1 <= val <= 7):
                profile.days_per_week = val
                updated_fields.append("days_per_week")

        if updated_fields:
            updated_fields.append("updated_at")
            profile.save(update_fields=updated_fields)

        return Response(_serialize_profile(profile))


class RuntimeBodyWeightView(APIView):
    """POST: log body weight from the AI assistant."""

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
        weight_date = data.get("date", str(date.today()))
        weight_val = data.get("weight_kg")
        if weight_val is None:
            return Response({"error": "weight_kg is required"}, status=status.HTTP_400_BAD_REQUEST)

        try:
            weight_kg = Decimal(str(weight_val))
        except (InvalidOperation, ValueError):
            return Response({"error": "weight_kg must be a valid number"}, status=status.HTTP_400_BAD_REQUEST)

        entry, created = BodyWeightLog.objects.update_or_create(
            tenant=tenant,
            date=weight_date,
            defaults={"weight_kg": weight_kg},
        )
        return Response(
            {"date": str(entry.date), "weight_kg": str(entry.weight_kg), "created": created},
            status=status.HTTP_201_CREATED if created else status.HTTP_200_OK,
        )
