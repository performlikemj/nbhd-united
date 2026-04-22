"""Consumer-facing Fuel API views (JWT auth, frontend)."""

import calendar
from collections import defaultdict

from django.db.models import Count, Sum
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import (
    BodyWeightLog,
    FuelGoal,
    FuelProfile,
    PersonalRecord,
    RestingHeartRateLog,
    Workout,
    WorkoutCategory,
    WorkoutTemplate,
)
from .serializers import (
    BodyWeightLogSerializer,
    FuelGoalSerializer,
    FuelProfileSerializer,
    PersonalRecordSerializer,
    RestingHeartRateLogSerializer,
    WorkoutSerializer,
    WorkoutStubSerializer,
    WorkoutTemplateSerializer,
)
from .services import (
    aggregate_calisthenics_progress,
    aggregate_cardio_progress,
    aggregate_hiit_progress,
    aggregate_strength_progress,
    detect_prs,
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

        # Create profile on enable (no-op if already exists)
        profile_status = None
        if tenant.fuel_enabled:
            profile, _created = FuelProfile.objects.get_or_create(tenant=tenant)
            profile_status = profile.onboarding_status

        return Response({"fuel_enabled": tenant.fuel_enabled, "fuel_profile_status": profile_status})


class FuelProfileView(APIView):
    """GET/PATCH the tenant's fitness profile."""

    permission_classes = [IsAuthenticated]

    def get(self, request):
        tenant = getattr(request.user, "tenant", None)
        if not tenant:
            return Response({"error": "no_tenant"}, status=status.HTTP_404_NOT_FOUND)
        try:
            profile = FuelProfile.objects.get(tenant=tenant)
        except FuelProfile.DoesNotExist:
            return Response({"error": "no_profile"}, status=status.HTTP_404_NOT_FOUND)
        return Response(FuelProfileSerializer(profile).data)

    def patch(self, request):
        tenant = getattr(request.user, "tenant", None)
        if not tenant:
            return Response({"error": "no_tenant"}, status=status.HTTP_404_NOT_FOUND)
        try:
            profile = FuelProfile.objects.get(tenant=tenant)
        except FuelProfile.DoesNotExist:
            return Response({"error": "no_profile"}, status=status.HTTP_404_NOT_FOUND)
        serializer = FuelProfileSerializer(profile, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(serializer.data)


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
        if status_filter in ("done", "planned", "rest"):
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
        workout = serializer.save()
        detect_prs(tenant, workout)
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
        updated = serializer.save()
        detect_prs(workout.tenant, updated)
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

        base_qs = Workout.objects.filter(tenant=tenant, category=cat, status="done")

        if cat in ("strength", "calisthenics"):
            # Only load fields needed for detail_json parsing, cap to 365 days
            workouts = list(base_qs.only("date", "detail_json").order_by("date")[:365])
            data = (
                aggregate_strength_progress(workouts)
                if cat == "strength"
                else aggregate_calisthenics_progress(workouts)
            )
        elif cat == "cardio":
            workouts = list(base_qs.only("date", "detail_json").order_by("date")[:365])
            data = aggregate_cardio_progress(workouts)
        elif cat == "hiit":
            workouts = list(base_qs.only("date", "duration_minutes", "detail_json").order_by("date")[:365])
            data = aggregate_hiit_progress(workouts)
        else:
            # mobility, sport, other — simple count + recent list
            total = base_qs.count()
            total_min = base_qs.aggregate(total=Sum("duration_minutes"))["total"] or 0
            sessions = list(
                base_qs.only("date", "activity", "duration_minutes")
                .order_by("-date")[:50]
                .values("date", "activity", "duration_minutes")
            )
            data = {
                "session_count": total,
                "total_minutes": total_min,
                "sessions": [
                    {"date": str(s["date"]), "activity": s["activity"], "duration_minutes": s["duration_minutes"]}
                    for s in sessions
                ],
            }

        return Response({"category": cat, "progress": data})


class WorkoutCountView(APIView):
    """GET: count workouts matching filters (lightweight alternative to listing)."""

    permission_classes = [IsAuthenticated]

    def get(self, request):
        tenant = getattr(request.user, "tenant", None)
        if not tenant:
            return Response({"error": "no_tenant"}, status=status.HTTP_404_NOT_FOUND)
        qs = Workout.objects.filter(tenant=tenant)

        cat = request.query_params.get("category")
        if cat and cat in WorkoutCategory.values:
            qs = qs.filter(category=cat)
        status_filter = request.query_params.get("status")
        if status_filter in ("done", "planned", "rest"):
            qs = qs.filter(status=status_filter)
        date_from = request.query_params.get("date_from")
        date_to = request.query_params.get("date_to")
        if date_from:
            qs = qs.filter(date__gte=date_from)
        if date_to:
            qs = qs.filter(date__lte=date_to)

        return Response({"count": qs.count()})


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


# ═════════════════════════════════════════════════════════════════════
# Workout Templates (PR 4)
# ═════════════════════════════════════════════════════════════════════


class WorkoutTemplateListView(APIView):
    """GET: list templates. POST: create template."""

    permission_classes = [IsAuthenticated]

    def get(self, request):
        tenant = getattr(request.user, "tenant", None)
        if not tenant:
            return Response({"error": "no_tenant"}, status=status.HTTP_404_NOT_FOUND)
        cat = request.query_params.get("category")
        qs = WorkoutTemplate.objects.filter(tenant=tenant)
        if cat and cat in WorkoutCategory.values:
            qs = qs.filter(category=cat)
        return Response(WorkoutTemplateSerializer(qs, many=True).data)

    def post(self, request):
        tenant = getattr(request.user, "tenant", None)
        if not tenant:
            return Response({"error": "no_tenant"}, status=status.HTTP_404_NOT_FOUND)
        serializer = WorkoutTemplateSerializer(data=request.data, context={"tenant": tenant})
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(serializer.data, status=status.HTTP_201_CREATED)


class WorkoutTemplateDetailView(APIView):
    """GET/PATCH/DELETE a single template."""

    permission_classes = [IsAuthenticated]

    def _get(self, request, template_id):
        tenant = getattr(request.user, "tenant", None)
        if not tenant:
            return None, Response({"error": "no_tenant"}, status=status.HTTP_404_NOT_FOUND)
        try:
            tmpl = WorkoutTemplate.objects.get(id=template_id, tenant=tenant)
        except WorkoutTemplate.DoesNotExist:
            return None, Response({"error": "not_found"}, status=status.HTTP_404_NOT_FOUND)
        return tmpl, None

    def get(self, request, template_id):
        tmpl, err = self._get(request, template_id)
        if err:
            return err
        return Response(WorkoutTemplateSerializer(tmpl).data)

    def patch(self, request, template_id):
        tmpl, err = self._get(request, template_id)
        if err:
            return err
        serializer = WorkoutTemplateSerializer(tmpl, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(serializer.data)

    def delete(self, request, template_id):
        tmpl, err = self._get(request, template_id)
        if err:
            return err
        tmpl.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


class WorkoutDuplicateView(APIView):
    """POST: create a new workout pre-filled from an existing one."""

    permission_classes = [IsAuthenticated]

    def post(self, request, workout_id):
        tenant = getattr(request.user, "tenant", None)
        if not tenant:
            return Response({"error": "no_tenant"}, status=status.HTTP_404_NOT_FOUND)
        try:
            source = Workout.objects.get(id=workout_id, tenant=tenant)
        except Workout.DoesNotExist:
            return Response({"error": "not_found"}, status=status.HTTP_404_NOT_FOUND)

        from datetime import date as date_cls

        new_workout = Workout.objects.create(
            tenant=tenant,
            date=date_cls.today(),
            status="planned",
            category=source.category,
            activity=source.activity,
            duration_minutes=source.duration_minutes,
            detail_json=source.detail_json,
        )
        return Response(WorkoutSerializer(new_workout).data, status=status.HTTP_201_CREATED)


# ═════════════════════════════════════════════════════════════════════
# Weekly Volume Summary (PR 5)
# ═════════════════════════════════════════════════════════════════════


class WeeklyVolumeSummaryView(APIView):
    """GET: weekly workout volume summary."""

    permission_classes = [IsAuthenticated]

    def get(self, request):
        from datetime import date as date_cls
        from datetime import timedelta

        tenant = getattr(request.user, "tenant", None)
        if not tenant:
            return Response({"error": "no_tenant"}, status=status.HTTP_404_NOT_FOUND)

        week_start_str = request.query_params.get("week_start")
        if week_start_str:
            try:
                week_start = date_cls.fromisoformat(week_start_str)
            except ValueError:
                return Response({"error": "invalid week_start"}, status=status.HTTP_400_BAD_REQUEST)
        else:
            today = date_cls.today()
            week_start = today - timedelta(days=today.weekday())  # Monday

        week_end = week_start + timedelta(days=6)

        by_category = list(
            Workout.objects.filter(
                tenant=tenant,
                status="done",
                date__gte=week_start,
                date__lte=week_end,
            )
            .values("category")
            .annotate(count=Count("id"), total_minutes=Sum("duration_minutes"))
            .order_by("category")
        )

        total_sessions = sum(c["count"] for c in by_category)
        total_minutes = sum(c["total_minutes"] or 0 for c in by_category)

        return Response(
            {
                "week_start": str(week_start),
                "week_end": str(week_end),
                "by_category": by_category,
                "totals": {"sessions": total_sessions, "minutes": total_minutes},
            }
        )


# ═════════════════════════════════════════════════════════════════════
# PR History + Goals (PR 6)
# ═════════════════════════════════════════════════════════════════════


class PRFeedView(APIView):
    """GET: recent personal records."""

    permission_classes = [IsAuthenticated]

    def get(self, request):
        tenant = getattr(request.user, "tenant", None)
        if not tenant:
            return Response({"error": "no_tenant"}, status=status.HTTP_404_NOT_FOUND)
        limit = min(int(request.query_params.get("limit", 20)), 100)
        prs = PersonalRecord.objects.filter(tenant=tenant)[:limit]
        return Response(PersonalRecordSerializer(prs, many=True).data)


class FuelGoalListView(APIView):
    """GET: list goals. POST: create goal."""

    permission_classes = [IsAuthenticated]

    def get(self, request):
        tenant = getattr(request.user, "tenant", None)
        if not tenant:
            return Response({"error": "no_tenant"}, status=status.HTTP_404_NOT_FOUND)
        goals = FuelGoal.objects.filter(tenant=tenant)
        return Response(FuelGoalSerializer(goals, many=True).data)

    def post(self, request):
        tenant = getattr(request.user, "tenant", None)
        if not tenant:
            return Response({"error": "no_tenant"}, status=status.HTTP_404_NOT_FOUND)
        serializer = FuelGoalSerializer(data=request.data, context={"tenant": tenant})
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(serializer.data, status=status.HTTP_201_CREATED)


class FuelGoalDetailView(APIView):
    """PATCH/DELETE a single goal."""

    permission_classes = [IsAuthenticated]

    def patch(self, request, goal_id):
        tenant = getattr(request.user, "tenant", None)
        if not tenant:
            return Response({"error": "no_tenant"}, status=status.HTTP_404_NOT_FOUND)
        try:
            goal = FuelGoal.objects.get(id=goal_id, tenant=tenant)
        except FuelGoal.DoesNotExist:
            return Response({"error": "not_found"}, status=status.HTTP_404_NOT_FOUND)
        serializer = FuelGoalSerializer(goal, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(serializer.data)

    def delete(self, request, goal_id):
        tenant = getattr(request.user, "tenant", None)
        if not tenant:
            return Response({"error": "no_tenant"}, status=status.HTTP_404_NOT_FOUND)
        try:
            goal = FuelGoal.objects.get(id=goal_id, tenant=tenant)
        except FuelGoal.DoesNotExist:
            return Response({"error": "not_found"}, status=status.HTTP_404_NOT_FOUND)
        goal.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


# ═════════════════════════════════════════════════════════════════════
# Resting Heart Rate (PR 7)
# ═════════════════════════════════════════════════════════════════════


class RestingHRListView(APIView):
    """GET: list entries. POST: create or upsert by date."""

    permission_classes = [IsAuthenticated]

    def get(self, request):
        tenant = getattr(request.user, "tenant", None)
        if not tenant:
            return Response({"error": "no_tenant"}, status=status.HTTP_404_NOT_FOUND)
        limit = min(int(request.query_params.get("limit", 90)), 365)
        entries = RestingHeartRateLog.objects.filter(tenant=tenant)[:limit]
        return Response(RestingHeartRateLogSerializer(entries, many=True).data)

    def post(self, request):
        tenant = getattr(request.user, "tenant", None)
        if not tenant:
            return Response({"error": "no_tenant"}, status=status.HTTP_404_NOT_FOUND)

        entry_date = request.data.get("date")
        bpm = request.data.get("bpm")
        if not entry_date or bpm is None:
            return Response({"error": "date and bpm required"}, status=status.HTTP_400_BAD_REQUEST)

        entry, created = RestingHeartRateLog.objects.update_or_create(
            tenant=tenant,
            date=entry_date,
            defaults={"bpm": int(bpm)},
        )
        return Response(
            RestingHeartRateLogSerializer(entry).data,
            status=status.HTTP_201_CREATED if created else status.HTTP_200_OK,
        )


class RestingHRDetailView(APIView):
    """PATCH/DELETE a single resting HR entry."""

    permission_classes = [IsAuthenticated]

    def patch(self, request, entry_id):
        tenant = getattr(request.user, "tenant", None)
        if not tenant:
            return Response({"error": "no_tenant"}, status=status.HTTP_404_NOT_FOUND)
        try:
            entry = RestingHeartRateLog.objects.get(id=entry_id, tenant=tenant)
        except RestingHeartRateLog.DoesNotExist:
            return Response({"error": "not_found"}, status=status.HTTP_404_NOT_FOUND)
        serializer = RestingHeartRateLogSerializer(entry, data=request.data, partial=True, context={"tenant": tenant})
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(serializer.data)

    def delete(self, request, entry_id):
        tenant = getattr(request.user, "tenant", None)
        if not tenant:
            return Response({"error": "no_tenant"}, status=status.HTTP_404_NOT_FOUND)
        try:
            entry = RestingHeartRateLog.objects.get(id=entry_id, tenant=tenant)
        except RestingHeartRateLog.DoesNotExist:
            return Response({"error": "not_found"}, status=status.HTTP_404_NOT_FOUND)
        entry.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)
