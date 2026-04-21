"""Consumer-facing Fuel API views (JWT auth, frontend)."""

import calendar
from collections import defaultdict

from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import BodyWeightLog, Workout, WorkoutCategory
from .serializers import BodyWeightLogSerializer, WorkoutSerializer, WorkoutStubSerializer
from .services import (
    aggregate_calisthenics_progress,
    aggregate_cardio_progress,
    aggregate_hiit_progress,
    aggregate_strength_progress,
)


class FuelSettingsView(APIView):
    """PATCH: toggle fuel_enabled for the tenant."""

    permission_classes = [IsAuthenticated]

    def patch(self, request):
        tenant = getattr(request.user, "tenant", None)
        if not tenant:
            return Response({"error": "no_tenant"}, status=status.HTTP_404_NOT_FOUND)
        fuel_enabled = request.data.get("fuel_enabled")
        if fuel_enabled is None:
            return Response(
                {"error": "fuel_enabled is required"},
                status=status.HTTP_400_BAD_REQUEST,
            )
        tenant.fuel_enabled = bool(fuel_enabled)
        tenant.save(update_fields=["fuel_enabled"])
        tenant.bump_pending_config()
        return Response({"fuel_enabled": tenant.fuel_enabled})


class WorkoutListView(APIView):
    """GET: list workouts. POST: create workout."""

    permission_classes = [IsAuthenticated]

    def get(self, request):
        tenant = getattr(request.user, "tenant", None)
        if not tenant:
            return Response({"error": "no_tenant"}, status=status.HTTP_404_NOT_FOUND)
        qs = Workout.objects.filter(tenant=tenant)

        # Optional filters
        cat = request.query_params.get("category")
        if cat and cat in WorkoutCategory.values:
            qs = qs.filter(category=cat)
        status_filter = request.query_params.get("status")
        if status_filter in ("done", "planned"):
            qs = qs.filter(status=status_filter)
        date_from = request.query_params.get("date_from")
        date_to = request.query_params.get("date_to")
        if date_from:
            qs = qs.filter(date__gte=date_from)
        if date_to:
            qs = qs.filter(date__lte=date_to)

        limit = min(int(request.query_params.get("limit", 100)), 500)
        qs = qs[:limit]
        serializer = WorkoutSerializer(qs, many=True)
        return Response(serializer.data)

    def post(self, request):
        tenant = getattr(request.user, "tenant", None)
        if not tenant:
            return Response({"error": "no_tenant"}, status=status.HTTP_404_NOT_FOUND)
        serializer = WorkoutSerializer(data=request.data, context={"tenant": tenant})
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(serializer.data, status=status.HTTP_201_CREATED)


class WorkoutDetailView(APIView):
    """GET/PATCH/DELETE a single workout."""

    permission_classes = [IsAuthenticated]

    def _get_workout(self, request, workout_id):
        tenant = getattr(request.user, "tenant", None)
        if not tenant:
            return None, Response({"error": "no_tenant"}, status=status.HTTP_404_NOT_FOUND)
        try:
            workout = Workout.objects.get(id=workout_id, tenant=tenant)
        except Workout.DoesNotExist:
            return None, Response({"error": "not_found"}, status=status.HTTP_404_NOT_FOUND)
        return workout, None

    def get(self, request, workout_id):
        workout, err = self._get_workout(request, workout_id)
        if err:
            return err
        return Response(WorkoutSerializer(workout).data)

    def patch(self, request, workout_id):
        workout, err = self._get_workout(request, workout_id)
        if err:
            return err
        serializer = WorkoutSerializer(
            workout,
            data=request.data,
            partial=True,
            context={"tenant": workout.tenant},
        )
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(serializer.data)

    def delete(self, request, workout_id):
        workout, err = self._get_workout(request, workout_id)
        if err:
            return err
        workout.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


class WorkoutCalendarView(APIView):
    """GET: workout stubs grouped by date for a given month."""

    permission_classes = [IsAuthenticated]

    def get(self, request):
        tenant = getattr(request.user, "tenant", None)
        if not tenant:
            return Response({"error": "no_tenant"}, status=status.HTTP_404_NOT_FOUND)

        try:
            year = int(request.query_params["year"])
            month = int(request.query_params["month"])
        except (KeyError, ValueError):
            return Response(
                {"error": "year and month query params required"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        _, days_in_month = calendar.monthrange(year, month)
        date_from = f"{year}-{month:02d}-01"
        date_to = f"{year}-{month:02d}-{days_in_month:02d}"

        workouts = Workout.objects.filter(tenant=tenant, date__gte=date_from, date__lte=date_to).order_by(
            "date", "created_at"
        )

        by_date = defaultdict(list)
        for w in workouts:
            by_date[str(w.date)].append(WorkoutStubSerializer(w).data)

        result = [{"date": d, "workouts": ws} for d, ws in sorted(by_date.items())]
        return Response(result)


class WorkoutProgressView(APIView):
    """GET: aggregated progress data for a given category."""

    permission_classes = [IsAuthenticated]

    def get(self, request):
        tenant = getattr(request.user, "tenant", None)
        if not tenant:
            return Response({"error": "no_tenant"}, status=status.HTTP_404_NOT_FOUND)

        cat = request.query_params.get("category", "strength")
        if cat not in WorkoutCategory.values:
            return Response({"error": "invalid category"}, status=status.HTTP_400_BAD_REQUEST)

        workouts = list(Workout.objects.filter(tenant=tenant, category=cat, status="done"))

        if cat == "strength":
            data = aggregate_strength_progress(workouts)
        elif cat == "cardio":
            data = aggregate_cardio_progress(workouts)
        elif cat == "hiit":
            data = aggregate_hiit_progress(workouts)
        elif cat == "calisthenics":
            data = aggregate_calisthenics_progress(workouts)
        else:
            # mobility, sport, other — simple count + list
            data = {
                "session_count": len(workouts),
                "total_minutes": sum(w.duration_minutes or 0 for w in workouts),
                "sessions": [
                    {"date": str(w.date), "activity": w.activity, "duration_minutes": w.duration_minutes}
                    for w in workouts
                ],
            }

        return Response({"category": cat, "progress": data})


class BodyWeightListView(APIView):
    """GET: list entries. POST: create or upsert by date."""

    permission_classes = [IsAuthenticated]

    def get(self, request):
        tenant = getattr(request.user, "tenant", None)
        if not tenant:
            return Response({"error": "no_tenant"}, status=status.HTTP_404_NOT_FOUND)
        limit = min(int(request.query_params.get("limit", 90)), 365)
        entries = BodyWeightLog.objects.filter(tenant=tenant)[:limit]
        return Response(BodyWeightLogSerializer(entries, many=True).data)

    def post(self, request):
        tenant = getattr(request.user, "tenant", None)
        if not tenant:
            return Response({"error": "no_tenant"}, status=status.HTTP_404_NOT_FOUND)

        date = request.data.get("date")
        weight_kg = request.data.get("weight_kg")
        if not date or weight_kg is None:
            return Response(
                {"error": "date and weight_kg required"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        entry, created = BodyWeightLog.objects.update_or_create(
            tenant=tenant,
            date=date,
            defaults={"weight_kg": weight_kg},
        )
        return Response(
            BodyWeightLogSerializer(entry).data,
            status=status.HTTP_201_CREATED if created else status.HTTP_200_OK,
        )


class BodyWeightDetailView(APIView):
    """PATCH/DELETE a single body-weight entry."""

    permission_classes = [IsAuthenticated]

    def patch(self, request, entry_id):
        tenant = getattr(request.user, "tenant", None)
        if not tenant:
            return Response({"error": "no_tenant"}, status=status.HTTP_404_NOT_FOUND)
        try:
            entry = BodyWeightLog.objects.get(id=entry_id, tenant=tenant)
        except BodyWeightLog.DoesNotExist:
            return Response({"error": "not_found"}, status=status.HTTP_404_NOT_FOUND)
        serializer = BodyWeightLogSerializer(entry, data=request.data, partial=True, context={"tenant": tenant})
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(serializer.data)

    def delete(self, request, entry_id):
        tenant = getattr(request.user, "tenant", None)
        if not tenant:
            return Response({"error": "no_tenant"}, status=status.HTTP_404_NOT_FOUND)
        try:
            entry = BodyWeightLog.objects.get(id=entry_id, tenant=tenant)
        except BodyWeightLog.DoesNotExist:
            return Response({"error": "not_found"}, status=status.HTTP_404_NOT_FOUND)
        entry.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)
