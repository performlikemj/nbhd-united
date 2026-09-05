"""Fuel serializers — workout and body-weight API representations."""

import logging

from rest_framework import serializers

from apps.pii.store_authoring import OwnerStoreSerializerMixin, author_store_fields, owner_store_representation

from .models import (
    BodyWeightLog,
    FuelGoal,
    FuelProfile,
    PersonalRecord,
    RestingHeartRateLog,
    SleepLog,
    Workout,
    WorkoutPlan,
    WorkoutTemplate,
)

logger = logging.getLogger(__name__)


class _FuelPiiSerializerMixin(OwnerStoreSerializerMixin):
    def create(self, validated_data):
        if self.context.get("pii_preauthored"):
            return super().create(validated_data)
        tenant = validated_data.get("tenant") or self.context["tenant"]
        writer = self.context.get("pii_writer", "owner")
        authored, receipts = author_store_fields(
            tenant,
            validated_data,
            model_label=self.pii_model_label,
            seam=f"fuel.{self.pii_model_label}.create",
            writer=writer,
            defer_detection=writer == "runtime",
        )
        authored["pii_receipts"] = receipts
        return super().create(authored)

    def update(self, instance, validated_data):
        if self.context.get("pii_preauthored"):
            return super().update(instance, validated_data)
        writer = self.context.get("pii_writer", "owner")
        authored, receipts = author_store_fields(
            instance.tenant,
            validated_data,
            model_label=self.pii_model_label,
            seam=f"fuel.{self.pii_model_label}.update",
            writer=writer,
            receipts=instance.pii_receipts,
            defer_detection=writer == "runtime",
        )
        authored["pii_receipts"] = receipts
        return super().update(instance, authored)


def _loc_path(loc) -> str:
    """Render a pydantic error loc as a compact path string.

    Field keys and list indices only — never user-entered values — so it
    is safe for both Log Analytics lines and user-facing error messages.
    """
    out = ""
    for part in loc:
        out += f"[{part}]" if isinstance(part, int) else (f".{part}" if out else str(part))
    return out


class FuelProfileSerializer(_FuelPiiSerializerMixin, serializers.ModelSerializer):
    pii_model_label = "fuel.FuelProfile"

    class Meta:
        model = FuelProfile
        fields = [
            "id",
            "onboarding_status",
            "fitness_level",
            "goals",
            "limitations",
            "equipment",
            "days_per_week",
            "preferred_days",
            "preferred_time",
            "additional_context",
            "distance_unit",
            "pii_receipts",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["id", "pii_receipts", "created_at", "updated_at"]


class WorkoutPlanSerializer(_FuelPiiSerializerMixin, serializers.ModelSerializer):
    pii_model_label = "fuel.WorkoutPlan"
    workout_count = serializers.IntegerField(read_only=True, default=0)
    completed_count = serializers.IntegerField(read_only=True, default=0)
    # Derived program-progress — end_date (inclusive last day), days_remaining
    # (0 once over), current_week (1-based). See services.plan_progress_fields.
    end_date = serializers.SerializerMethodField()
    days_remaining = serializers.SerializerMethodField()
    current_week = serializers.SerializerMethodField()

    class Meta:
        model = WorkoutPlan
        fields = [
            "id",
            "name",
            "status",
            "start_date",
            "weeks",
            "days_per_week",
            "schedule_json",
            "notes",
            "objective",
            "pii_receipts",
            "workout_count",
            "completed_count",
            "end_date",
            "days_remaining",
            "current_week",
            "created_at",
            "updated_at",
        ]
        read_only_fields = [
            "id",
            "workout_count",
            "completed_count",
            "end_date",
            "days_remaining",
            "current_week",
            "pii_receipts",
            "created_at",
            "updated_at",
        ]

    def _progress(self, obj):
        """Cached per-instance program-progress dict. ``today`` comes from
        serializer context when the caller precomputed it once (the plan-list
        path — avoids an N+1 on ``obj.tenant``), else resolved from the plan's
        tenant through the tz front door.
        """
        cached = getattr(obj, "_progress_cache", None)
        if cached is None:
            from apps.common.tenant_tz import tenant_today

            from .services import plan_progress_fields

            today = self.context.get("today") or tenant_today(obj.tenant)
            cached = plan_progress_fields(obj, today)
            obj._progress_cache = cached
        return cached

    def get_end_date(self, obj):
        return self._progress(obj)["end_date"]

    def get_days_remaining(self, obj):
        return self._progress(obj)["days_remaining"]

    def get_current_week(self, obj):
        return self._progress(obj)["current_week"]

    def to_representation(self, instance):
        represented = super().to_representation(instance)
        schedule = represented.get("schedule_json")
        if not isinstance(schedule, dict):
            return represented
        policy = schedule.get("_plan_policy")
        represented["schedule_json"] = {key: value for key, value in schedule.items() if key != "_plan_policy"}
        if isinstance(policy, dict):
            represented.update(policy)
        return represented

    def create(self, validated_data):
        validated_data["tenant"] = self.context["tenant"]
        return super().create(validated_data)


class WorkoutSerializer(_FuelPiiSerializerMixin, serializers.ModelSerializer):
    pii_model_label = "fuel.Workout"
    plan_id = serializers.UUIDField(source="plan.id", read_only=True, default=None)
    plan_name = serializers.CharField(source="plan.name", read_only=True, default=None)
    # Optional at the field level so callers can supply scheduled_at instead;
    # validate() backfills date from scheduled_at when needed.
    date = serializers.DateField(required=False)

    class Meta:
        model = Workout
        fields = [
            "id",
            "date",
            "scheduled_at",
            "window_start_at",
            "window_end_at",
            "status",
            "source",
            "original_workout",
            "skip_reason",
            "category",
            "activity",
            "duration_minutes",
            "duration_seconds",
            "rpe",
            "notes",
            "notes_thread",
            "detail_json",
            "pii_receipts",
            "plan_id",
            "plan_name",
            "version",
            "edit_lock_until",
            "edit_lock_owner",
            "last_edited_by_user_at",
            "created_at",
            "updated_at",
        ]
        read_only_fields = [
            "id",
            "original_workout",
            "plan_id",
            "plan_name",
            "version",
            "edit_lock_until",
            "edit_lock_owner",
            "last_edited_by_user_at",
            "pii_receipts",
            "created_at",
            "updated_at",
        ]

    def validate_detail_json(self, value):
        """Basic shape validation per category."""
        if not isinstance(value, dict):
            raise serializers.ValidationError("detail_json must be an object.")
        return value

    def validate_rpe(self, value):
        if value is not None and not (1 <= value <= 10):
            raise serializers.ValidationError("RPE must be between 1 and 10.")
        return value

    def validate_duration_seconds(self, value):
        if value is not None and not (1 <= value <= 86400):
            raise serializers.ValidationError("duration_seconds out of range (1-86400).")
        return value

    def validate(self, attrs):
        # When scheduled_at is provided without an explicit date, derive date
        # from it in the tenant's local timezone — this keeps day-bucketed
        # queries (calendar, weekly summary) consistent with the time-of-day.
        # DRF normalizes parsed datetimes to UTC, so .date() alone would
        # bucket a JST 07:00 session onto the previous day.
        if attrs.get("scheduled_at") and not attrs.get("date"):
            tenant = self.context.get("tenant") or (self.instance.tenant if self.instance else None)
            if tenant is not None:
                from apps.common.tenant_tz import tenant_tz

                attrs["date"] = attrs["scheduled_at"].astimezone(tenant_tz(tenant)).date()
            else:
                attrs["date"] = attrs["scheduled_at"].date()
        if not attrs.get("date") and not self.instance:
            raise serializers.ValidationError({"date": "Either date or scheduled_at is required."})

        # Keep duration_minutes and duration_seconds consistent for manual
        # create/edit (the HK sync path sets both precisely, bypassing this
        # serializer). Minutes is the field every UI edits; derive seconds from
        # it ONLY when minutes actually changed or an inconsistent seconds was
        # sent — so a note-only save on an HK-synced run (which re-sends the same
        # rounded minutes but no seconds) keeps its precise stored seconds.
        if "duration_minutes" in attrs:
            mins = attrs["duration_minutes"]
            secs = attrs.get("duration_seconds")
            existing = self.instance.duration_minutes if self.instance else None
            if mins is None:
                attrs["duration_seconds"] = None
            elif secs is not None:
                if abs(secs - mins * 60) > 60:
                    attrs["duration_seconds"] = mins * 60  # inconsistent → trust minutes
                # else: keep the finer seconds the client supplied
            elif mins != existing:
                attrs["duration_seconds"] = mins * 60  # minutes changed → resync coarse
            # else: minutes unchanged, no seconds sent → preserve stored precision
        elif attrs.get("duration_seconds") is not None:
            # Seconds without minutes (a future fine-grained editor) — backfill the
            # rounded minutes so day-bucketed views and matching stay correct.
            attrs["duration_minutes"] = max(1, round(attrs["duration_seconds"] / 60))

        # Phase 1 (#593) — same deterministic registry correction the
        # runtime path applies, for frontend-origin create/edit. Local
        # import keeps the lint-autofix from reaping it between edits.
        category_changed = (
            self.instance is not None and "category" in attrs and attrs["category"] != self.instance.category
        )
        if self.instance is not None and (category_changed or "duration_minutes" in attrs):
            attrs.setdefault("detail_json", self.instance.detail_json)
        if "detail_json" in attrs:
            from .set_contract import normalize_detail, split_detail_errors, validate_detail

            base_cat = attrs.get("category") or (self.instance.category if self.instance else "other")
            base_act = attrs.get("activity") or (self.instance.activity if self.instance else None)
            incoming = attrs["detail_json"]
            stored = self.instance.detail_json if self.instance else None

            # A structurally-identical resend of the stored detail is a
            # no-op on this field — skip the strict contract entirely. The
            # web editor round-trips stored detail_json on every save, so
            # without this one legacy-invalid set (assistant- or
            # HealthKit-authored, pre-#593) poisons the workout: every
            # subsequent save — including a bundled status→"done" — 400s
            # (45 PATCH 400s in 30 days, 21 of them one user retrying a
            # single poisoned workout for 3 hours).
            if (
                self.instance is not None
                and incoming == stored
                and not category_changed
                and "duration_minutes" not in attrs
                and not (isinstance(incoming, dict) and ("segments" in incoming or "planned" in incoming))
            ):
                return attrs

            nd, ncat = normalize_detail(
                incoming, base_cat, activity=base_act, explicit_duration_minutes=attrs.get("duration_minutes")
            )[:2]
            coerced, verr = validate_detail(nd, ncat)
            if verr is None:
                attrs["detail_json"] = coerced
            else:
                new_details, legacy_details = split_detail_errors(
                    verr.details, incoming, None if category_changed else stored
                )
                # One structured line per validation failure so incidents
                # are attributable from Log Analytics. Field keys and set
                # indices only — never user-entered values (PII).
                logger.warning(
                    "fuel.workout_detail_validation_failed workout=%s tenant=%s outcome=%s "
                    "new_errors=%s preexisting_errors=%s",
                    self.instance.id if self.instance else "create",
                    getattr(self.context.get("tenant"), "id", None),
                    "rejected" if new_details else "grandfathered",
                    [(_loc_path(d["loc"]), d["type"]) for d in new_details],
                    [(_loc_path(d["loc"]), d["type"]) for d in legacy_details],
                )
                if new_details:
                    # Genuinely new invalid input still fails — as a DRF
                    # field-error array ({"field": ["msg"]}): iOS surfaces
                    # exactly that shape to users for 400/422. Only the NEW
                    # offenders are listed; pre-existing stored ones aren't
                    # actionable in this request.
                    raise serializers.ValidationError(
                        {"detail_json": [f"{_loc_path(d['loc'])}: {d['msg']}" for d in new_details]}
                    )
                # Every failing fragment already exists verbatim in the
                # stored row — legacy-invalid state the user didn't author
                # now. Persist the loss-free normalized form (registry
                # type-stamping only; ``coerced`` would replace a non-dict
                # set with an empty typed stub) so the save goes through
                # and nothing the user typed or previously stored is lost.
                attrs["detail_json"] = nd
            if ncat != base_cat:
                attrs["category"] = ncat
        return attrs

    def create(self, validated_data):
        validated_data["tenant"] = self.context["tenant"]
        return super().create(validated_data)

    def to_representation(self, instance):
        represented = super().to_representation(instance)
        tenant = self.context.get("tenant")
        plan = getattr(instance, "plan", None)
        if tenant is None or not self.context.get("rehydrate") or plan is None:
            return represented

        plan_data = owner_store_representation(
            plan,
            tenant,
            {"name": represented.get("plan_name"), "pii_receipts": plan.pii_receipts},
            model_label="fuel.WorkoutPlan",
        )
        represented["plan_name"] = plan_data["name"]
        plan_name_receipt = plan_data["pii_receipts"].get("name")
        if plan_name_receipt is not None:
            represented.setdefault("pii_receipts", {})["plan_name"] = plan_name_receipt
        return represented


class WorkoutStubSerializer(OwnerStoreSerializerMixin, serializers.ModelSerializer):
    """Lightweight serializer for calendar day cells."""

    pii_model_label = "fuel.Workout"

    # Read the raw FK column (the field name ``plan_id`` IS the source — no ``source=``,
    # which DRF rejects as redundant), not ``plan.id``. Traversing the relation
    # lazy-loads the whole WorkoutPlan row per workout just to read a PK that already
    # sits on this row — an N+1 that turns a cold-cache month calendar into ~14 extra
    # DB round-trips (brutal against the trans-Pacific DB). Raw column is the same UUID.
    plan_id = serializers.UUIDField(read_only=True, default=None)

    class Meta:
        model = Workout
        fields = [
            "id",
            "date",
            "scheduled_at",
            "category",
            "activity",
            "status",
            "duration_minutes",
            "rpe",
            "plan_id",
            "pii_receipts",
        ]
        read_only_fields = fields


class BodyWeightLogSerializer(serializers.ModelSerializer):
    class Meta:
        model = BodyWeightLog
        fields = ["id", "date", "weight_kg", "created_at"]
        read_only_fields = ["id", "created_at"]

    def create(self, validated_data):
        validated_data["tenant"] = self.context["tenant"]
        return super().create(validated_data)


class WorkoutTemplateSerializer(_FuelPiiSerializerMixin, serializers.ModelSerializer):
    pii_model_label = "fuel.WorkoutTemplate"

    class Meta:
        model = WorkoutTemplate
        fields = [
            "id",
            "name",
            "category",
            "activity",
            "duration_minutes",
            "detail_json",
            "pii_receipts",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["id", "pii_receipts", "created_at", "updated_at"]

    def validate(self, attrs):
        from .set_contract import normalize_detail, validate_detail, validate_flat_detail

        category = attrs.get("category", self.instance.category if self.instance else "other")
        detail = attrs.get("detail_json", self.instance.detail_json if self.instance else {})
        detail, category = normalize_detail(detail, category, explicit_duration_minutes=attrs.get("duration_minutes"))[
            :2
        ]
        for validator in (validate_detail, validate_flat_detail):
            detail, error = validator(detail, category)
            if error is not None:
                raise serializers.ValidationError({"detail_json": [e["msg"] for e in error.details]})
        attrs["detail_json"] = detail
        attrs["category"] = category
        return attrs

    def create(self, validated_data):
        validated_data["tenant"] = self.context["tenant"]
        return super().create(validated_data)


class PersonalRecordSerializer(serializers.ModelSerializer):
    display = serializers.SerializerMethodField()

    class Meta:
        model = PersonalRecord
        fields = [
            "id",
            "exercise_name",
            "category",
            "value",
            "previous_value",
            "metric",
            "display",
            "date",
            "created_at",
        ]
        read_only_fields = fields

    def get_display(self, obj):
        from .services import format_pr_display

        return format_pr_display(obj)


class FuelGoalSerializer(serializers.ModelSerializer):
    class Meta:
        model = FuelGoal
        fields = ["id", "exercise_name", "metric", "target_value", "target_date", "achieved_at", "created_at"]
        read_only_fields = ["id", "created_at"]

    def create(self, validated_data):
        validated_data["tenant"] = self.context["tenant"]
        return super().create(validated_data)


class RestingHeartRateLogSerializer(serializers.ModelSerializer):
    class Meta:
        model = RestingHeartRateLog
        fields = ["id", "date", "bpm", "created_at"]
        read_only_fields = ["id", "created_at"]

    def create(self, validated_data):
        validated_data["tenant"] = self.context["tenant"]
        return super().create(validated_data)


class SleepLogSerializer(_FuelPiiSerializerMixin, serializers.ModelSerializer):
    pii_model_label = "fuel.SleepLog"

    class Meta:
        model = SleepLog
        fields = ["id", "date", "duration_hours", "quality", "notes", "pii_receipts", "created_at"]
        read_only_fields = ["id", "pii_receipts", "created_at"]

    def create(self, validated_data):
        validated_data["tenant"] = self.context["tenant"]
        return super().create(validated_data)
