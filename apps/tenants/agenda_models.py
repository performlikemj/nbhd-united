"""Engagement metadata for the agenda meta-view (Phase B).

Phase A introduced the agenda envelope section — a meta-view over open
threads (tasks, goals, planned workouts, financial plans, untouched
feature intros). Phase B adds the engagement metadata that lets the
renderer be *priority-aware*: suppress threads recently surfaced to the
user, hide threads they've signaled disinterest in, and boost threads
that have been ignored long enough to deserve another natural mention.

Why a generic overlay table instead of columns on each primitive:

- Several thread sources (Tasks, Goals from journal Documents) are
  stored as markdown inside a single ``Document`` record, not as
  one-row-per-thread. A column-on-the-row approach can't express
  engagement for those.
- One overlay table means one place to query, one consistent shape,
  and one extension point — a new thread kind in Phase C/D needs to
  add an enum value, nothing else.
- Engagement is a cross-cutting concern. It belongs alongside the
  Tenant model (per-tenant overlay state) rather than fragmented
  across pillar apps.

The table is *additive*: a row's absence means "no engagement signals
captured yet" — equivalent to ``state=NASCENT`` semantically. The
renderer treats missing rows as "no constraint" and shows the thread
on its inherent merits. Rows are created on demand when an engagement
event fires.
"""

from __future__ import annotations

from django.db import models


class AgendaEngagement(models.Model):
    """Per-thread engagement metadata.

    Keyed by ``(tenant, kind, item_id)``. ``item_id`` is a stable
    identifier for the underlying thread:

    - ``feature_intro`` → the welcomes_sent feature key (``"fuel"``,
      ``"finance"``)
    - ``planned_workout`` → ``str(workout.id)``
    - ``fuel_goal`` → ``str(fuel_goal.id)``
    - ``payoff_plan`` → ``str(payoff_plan.id)``
    - ``task`` / ``goal`` (markdown) → content hash (Phase B+ — when we
      handle markdown threads)

    The renderer reads this table in bulk for the rendering tenant and
    joins it against the underlying thread query to filter / prioritize.
    """

    class Kind(models.TextChoices):
        FEATURE_INTRO = "feature_intro", "Feature introduction"
        PLANNED_WORKOUT = "planned_workout", "Planned workout"
        FUEL_GOAL = "fuel_goal", "Fuel goal"
        PAYOFF_PLAN = "payoff_plan", "Payoff plan"
        TASK = "task", "Task (markdown)"
        GOAL = "goal", "Goal (markdown)"
        # Phase D — assistant-written future-aware commitments. The
        # ``about`` / ``why`` text lives in ``metadata``. ``surface_after``
        # gates when the renderer becomes willing to suggest the agent
        # weave it into a turn.
        ASSISTANT_COMMITMENT = "assistant_commitment", "Assistant commitment"

    class State(models.TextChoices):
        NASCENT = "nascent", "Not yet introduced"
        INTRODUCED = "introduced", "Surfaced once, awaiting engagement"
        ACTIVE = "active", "User has engaged with this thread"
        DORMANT = "dormant", "Was active but has gone quiet"
        ABANDONED = "abandoned", "User signaled disinterest — don't re-surface"
        COMPLETED = "completed", "Done — no longer needs surfacing"

    tenant = models.ForeignKey(
        "tenants.Tenant",
        on_delete=models.CASCADE,
        related_name="agenda_engagements",
    )
    kind = models.CharField(max_length=32, choices=Kind.choices)
    item_id = models.CharField(
        max_length=128,
        help_text="Stable identifier for the thread (feature key, UUID, content hash, ...)",
    )

    state = models.CharField(
        max_length=16,
        choices=State.choices,
        default=State.NASCENT,
    )
    last_surfaced_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="When the assistant last proactively surfaced this thread.",
    )
    surface_after = models.DateTimeField(
        null=True,
        blank=True,
        help_text=(
            "Earliest acceptable next-surface time. Used to defer threads "
            "the user pushed back on, or to honor an explicit assistant "
            "commitment ('check in on this in 2 weeks')."
        ),
    )
    response_signals = models.JSONField(
        default=list,
        blank=True,
        help_text=(
            "Append-only log of {at, signal} dicts. Signal vocabulary: "
            "'warm' (engaged positively), 'redirect' (changed subject), "
            "'ignore' (no response), 'organic' (user brought it up first)."
        ),
    )
    metadata = models.JSONField(
        default=dict,
        blank=True,
        help_text=(
            "Per-kind extra data. For ASSISTANT_COMMITMENT: "
            "{'about': str, 'why': str}. For other kinds: empty by "
            "default — extension point for kind-specific fields."
        ),
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["tenant", "kind", "item_id"],
                name="agenda_engagement_unique_tenant_kind_item",
            ),
        ]
        indexes = [
            models.Index(fields=["tenant", "state"]),
            models.Index(fields=["tenant", "kind", "last_surfaced_at"]),
        ]

    def __str__(self) -> str:
        return f"{self.tenant_id}:{self.kind}:{self.item_id} ({self.state})"
