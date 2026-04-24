"""Consumer-facing Fuel API views (JWT auth, frontend)."""

import calendar
import logging
from collections import defaultdict
from datetime import date as date_cls

from django.db.models import Count, Sum
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView


def _safe_int(value, default):
    """Parse an int from query params, returning default on failure."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


from .models import (
    BodyWeightLog,
    FuelGoal,
    FuelProfile,
    PersonalRecord,
    RestingHeartRateLog,
    SleepLog,
    Workout,
    WorkoutCategory,
    WorkoutPlan,
    WorkoutStatus,
    WorkoutTemplate,
)
from .serializers import (
    BodyWeightLogSerializer,
    FuelGoalSerializer,
    FuelProfileSerializer,
    PersonalRecordSerializer,
    RestingHeartRateLogSerializer,
    SleepLogSerializer,
    WorkoutPlanSerializer,
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

_FUEL_WELCOME_PROMPT = (
    "Fuel was just enabled for this user. Send them a brief, warm welcome "
    "via `nbhd_send_to_user` letting them know their fitness assistant is "
    "ready. Keep it to 2-3 sentences — invite them to start a conversation "
    "about their fitness goals whenever they're ready. Don't start the full "
    "onboarding questionnaire in this message — just let them know the "
    "feature is live and you're here when they want to set things up.\n\n"
    "**Do NOT ask questions in this message.** Just welcome them and let "
    "them know they can come to you when ready."
)

_logger = logging.getLogger(__name__)


def _schedule_fuel_welcome(tenant):
    """Create a one-shot cron that sends a Fuel welcome message.

    Fires ~5 minutes after enablement (gives config time to deploy).
    Uses a date-specific cron expression so it fires exactly once,
    then the prompt instructs the agent to self-remove the cron.
    Best-effort — failures are logged, not raised.
    """
    import zoneinfo
    from datetime import datetime, timedelta

    try:
        from apps.cron.gateway_client import invoke_gateway_tool

        user_tz = str(getattr(tenant.user, "timezone", "") or "UTC")
        try:
            tz = zoneinfo.ZoneInfo(user_tz)
        except Exception:
            tz = zoneinfo.ZoneInfo("UTC")

        fire_at = datetime.now(tz) + timedelta(minutes=5)
        # Date-specific cron expr: fires once (minute hour day month *)
        cron_expr = f"{fire_at.minute} {fire_at.hour} {fire_at.day} {fire_at.month} *"

        welcome_message = (
            _FUEL_WELCOME_PROMPT
            + "\n\n---\n"
            + "After sending the welcome, remove this cron: `cron remove _fuel:welcome`"
        )

        invoke_gateway_tool(
            tenant,
            "cron.add",
            {
                "job": {
                    "name": "_fuel:welcome",
                    "schedule": {"kind": "cron", "expr": cron_expr, "tz": user_tz},
                    "sessionTarget": "isolated",
                    "payload": {
                        "kind": "agentTurn",
                        "message": welcome_message,
                    },
                    "delivery": {"mode": "none"},
                    "enabled": True,
                }
            },
        )
        _logger.info("Scheduled fuel welcome cron for tenant %s (fires at %s)", tenant.id, fire_at.isoformat())
    except Exception:
        _logger.warning(
            "Failed to schedule fuel welcome for tenant %s (user will get onboarding on next message)",
            tenant.id,
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

        # Deploy config immediately so the assistant picks up the fuel
        # plugin on the user's next message — don't wait for the hourly
        # apply_pending_configs cron.
        try:
            from apps.cron.publish import publish_task

            publish_task("apply_single_tenant_config", str(tenant.id))
        except Exception:
            _logger.warning(
                "Failed to enqueue immediate config deploy for tenant %s (will apply on next cron cycle)",
                tenant.id,
            )

        # Schedule a one-shot welcome message for newly enabled Fuel.
        # Fires 5 min after enablement (gives config time to deploy), then
        # auto-deletes. The assistant sends a brief nudge via the user's
        # channel — actual onboarding starts when they respond.
        if tenant.fuel_enabled and profile_status == "pending":
            _schedule_fuel_welcome(tenant)

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
        if status_filter and status_filter in WorkoutStatus.values:
            qs = qs.filter(status=status_filter)
        date_from = request.query_params.get("date_from")
        date_to = request.query_params.get("date_to")
        if date_from:
            qs = qs.filter(date__gte=date_from)
        if date_to:
            qs = qs.filter(date__lte=date_to)

        limit = min(_safe_int(request.query_params.get("limit"), 100), 500)
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
            if not (1 <= month <= 12) or not (1900 <= year <= 2100):
                raise ValueError("out of range")
        except (KeyError, ValueError, TypeError):
            return Response(
                {"error": "year (1900-2100) and month (1-12) query params required"},
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
        if status_filter and status_filter in WorkoutStatus.values:
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
        limit = min(_safe_int(request.query_params.get("limit"), 90), 365)
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
        limit = min(_safe_int(request.query_params.get("limit"), 20), 100)
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
        limit = min(_safe_int(request.query_params.get("limit"), 90), 365)
        entries = RestingHeartRateLog.objects.filter(tenant=tenant)[:limit]
        return Response(RestingHeartRateLogSerializer(entries, many=True).data)

    def post(self, request):
        tenant = getattr(request.user, "tenant", None)
        if not tenant:
            return Response({"error": "no_tenant"}, status=status.HTTP_404_NOT_FOUND)

        entry_date = request.data.get("date")
        bpm_raw = request.data.get("bpm")
        if not entry_date or bpm_raw is None:
            return Response({"error": "date and bpm required"}, status=status.HTTP_400_BAD_REQUEST)

        bpm = _safe_int(bpm_raw, None)
        if bpm is None or not (20 <= bpm <= 250):
            return Response(
                {"error": "bpm must be an integer between 20 and 250"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            date_cls.fromisoformat(str(entry_date))
        except (ValueError, TypeError):
            return Response({"error": "date must be YYYY-MM-DD format"}, status=status.HTTP_400_BAD_REQUEST)

        entry, created = RestingHeartRateLog.objects.update_or_create(
            tenant=tenant,
            date=entry_date,
            defaults={"bpm": bpm},
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


# ═════════════════════════════════════════════════════════════════════
# Sleep Tracking
# ═════════════════════════════════════════════════════════════════════


class SleepListView(APIView):
    """GET: list entries. POST: create or upsert by date."""

    permission_classes = [IsAuthenticated]

    def get(self, request):
        tenant = getattr(request.user, "tenant", None)
        if not tenant:
            return Response({"error": "no_tenant"}, status=status.HTTP_404_NOT_FOUND)
        limit = min(_safe_int(request.query_params.get("limit"), 90), 365)
        entries = SleepLog.objects.filter(tenant=tenant)[:limit]
        return Response(SleepLogSerializer(entries, many=True).data)

    def post(self, request):
        tenant = getattr(request.user, "tenant", None)
        if not tenant:
            return Response({"error": "no_tenant"}, status=status.HTTP_404_NOT_FOUND)

        entry_date = request.data.get("date")
        duration_raw = request.data.get("duration_hours")
        if not entry_date or duration_raw is None:
            return Response({"error": "date and duration_hours required"}, status=status.HTTP_400_BAD_REQUEST)

        try:
            date_cls.fromisoformat(str(entry_date))
        except (ValueError, TypeError):
            return Response({"error": "date must be YYYY-MM-DD format"}, status=status.HTTP_400_BAD_REQUEST)

        try:
            from decimal import Decimal, InvalidOperation

            duration = Decimal(str(duration_raw))
        except (InvalidOperation, ValueError, TypeError):
            return Response({"error": "duration_hours must be a number"}, status=status.HTTP_400_BAD_REQUEST)

        if duration < 0 or duration > 24:
            return Response({"error": "duration_hours must be between 0 and 24"}, status=status.HTTP_400_BAD_REQUEST)

        quality = None
        quality_raw = request.data.get("quality")
        if quality_raw is not None:
            quality = _safe_int(quality_raw, None)
            if quality is not None and not (1 <= quality <= 5):
                quality = None

        entry, created = SleepLog.objects.update_or_create(
            tenant=tenant,
            date=entry_date,
            defaults={
                "duration_hours": duration,
                "quality": quality,
                "notes": str(request.data.get("notes", "")).strip(),
            },
        )
        return Response(
            SleepLogSerializer(entry).data,
            status=status.HTTP_201_CREATED if created else status.HTTP_200_OK,
        )


class SleepDetailView(APIView):
    """PATCH/DELETE a single sleep entry."""

    permission_classes = [IsAuthenticated]

    def patch(self, request, entry_id):
        tenant = getattr(request.user, "tenant", None)
        if not tenant:
            return Response({"error": "no_tenant"}, status=status.HTTP_404_NOT_FOUND)
        try:
            entry = SleepLog.objects.get(id=entry_id, tenant=tenant)
        except SleepLog.DoesNotExist:
            return Response({"error": "not_found"}, status=status.HTTP_404_NOT_FOUND)
        serializer = SleepLogSerializer(entry, data=request.data, partial=True, context={"tenant": tenant})
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(serializer.data)

    def delete(self, request, entry_id):
        tenant = getattr(request.user, "tenant", None)
        if not tenant:
            return Response({"error": "no_tenant"}, status=status.HTTP_404_NOT_FOUND)
        try:
            entry = SleepLog.objects.get(id=entry_id, tenant=tenant)
        except SleepLog.DoesNotExist:
            return Response({"error": "not_found"}, status=status.HTTP_404_NOT_FOUND)
        entry.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


# ── Workout Plans ────────────────────────────────────────────────────


class WorkoutPlanListView(APIView):
    """GET: list plans. POST: create plan."""

    permission_classes = [IsAuthenticated]

    def get(self, request):
        tenant = getattr(request.user, "tenant", None)
        if not tenant:
            return Response({"error": "no_tenant"}, status=status.HTTP_404_NOT_FOUND)

        qs = WorkoutPlan.objects.filter(tenant=tenant)
        status_filter = request.query_params.get("status")
        if status_filter:
            qs = qs.filter(status=status_filter)

        result = []
        for plan in qs.order_by("-created_at")[:20]:
            data = WorkoutPlanSerializer(plan).data
            data["workout_count"] = Workout.objects.filter(plan=plan).count()
            data["completed_count"] = Workout.objects.filter(plan=plan, status=WorkoutStatus.DONE).count()
            result.append(data)

        return Response(result)

    def post(self, request):
        tenant = getattr(request.user, "tenant", None)
        if not tenant:
            return Response({"error": "no_tenant"}, status=status.HTTP_404_NOT_FOUND)
        serializer = WorkoutPlanSerializer(data=request.data, context={"tenant": tenant})
        serializer.is_valid(raise_exception=True)
        plan = serializer.save()

        # Expand the schedule template into individual planned Workout rows
        if plan.schedule_json and plan.start_date and plan.weeks:
            from .runtime_views import _expand_plan_workouts

            _expand_plan_workouts(plan, tenant, plan.schedule_json, plan.start_date, plan.weeks)

        data = serializer.data
        data["workout_count"] = Workout.objects.filter(plan=plan).count()
        return Response(data, status=status.HTTP_201_CREATED)


class WorkoutPlanDetailView(APIView):
    """GET/PATCH/DELETE a single workout plan."""

    permission_classes = [IsAuthenticated]

    def get(self, request, plan_id):
        tenant = getattr(request.user, "tenant", None)
        if not tenant:
            return Response({"error": "no_tenant"}, status=status.HTTP_404_NOT_FOUND)
        try:
            plan = WorkoutPlan.objects.get(id=plan_id, tenant=tenant)
        except WorkoutPlan.DoesNotExist:
            return Response({"error": "not_found"}, status=status.HTTP_404_NOT_FOUND)
        data = WorkoutPlanSerializer(plan).data
        data["workout_count"] = Workout.objects.filter(plan=plan).count()
        data["completed_count"] = Workout.objects.filter(plan=plan, status=WorkoutStatus.DONE).count()
        return Response(data)

    def patch(self, request, plan_id):
        tenant = getattr(request.user, "tenant", None)
        if not tenant:
            return Response({"error": "no_tenant"}, status=status.HTTP_404_NOT_FOUND)
        try:
            plan = WorkoutPlan.objects.get(id=plan_id, tenant=tenant)
        except WorkoutPlan.DoesNotExist:
            return Response({"error": "not_found"}, status=status.HTTP_404_NOT_FOUND)

        old_schedule = plan.schedule_json
        old_weeks = plan.weeks

        serializer = WorkoutPlanSerializer(plan, data=request.data, partial=True, context={"tenant": tenant})
        serializer.is_valid(raise_exception=True)
        plan = serializer.save()

        # Regenerate planned workouts if schedule or weeks changed
        if plan.schedule_json != old_schedule or plan.weeks != old_weeks:
            from datetime import date as _date

            from .runtime_views import _expand_plan_workouts

            today = _date.today()
            Workout.objects.filter(plan=plan, status=WorkoutStatus.PLANNED, date__gte=today).delete()
            elapsed_days = (today - plan.start_date).days
            elapsed_weeks = max(0, elapsed_days // 7)
            remaining_weeks = max(0, plan.weeks - elapsed_weeks)
            if remaining_weeks > 0:
                regen_start = max(today, plan.start_date)
                _expand_plan_workouts(plan, tenant, plan.schedule_json, regen_start, remaining_weeks)

        data = WorkoutPlanSerializer(plan).data
        data["workout_count"] = Workout.objects.filter(plan=plan).count()
        data["completed_count"] = Workout.objects.filter(plan=plan, status=WorkoutStatus.DONE).count()
        return Response(data)

    def delete(self, request, plan_id):
        tenant = getattr(request.user, "tenant", None)
        if not tenant:
            return Response({"error": "no_tenant"}, status=status.HTTP_404_NOT_FOUND)
        try:
            plan = WorkoutPlan.objects.get(id=plan_id, tenant=tenant)
        except WorkoutPlan.DoesNotExist:
            return Response({"error": "not_found"}, status=status.HTTP_404_NOT_FOUND)
        # Delete planned workouts, preserve completed ones
        Workout.objects.filter(plan=plan, status=WorkoutStatus.PLANNED).delete()
        Workout.objects.filter(plan=plan).exclude(status=WorkoutStatus.PLANNED).update(plan=None)
        plan.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)
