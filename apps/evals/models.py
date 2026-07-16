"""Eval schema — the record of every eval run and its per-case results.

Platform-level tables (like ``platform_logs``): NOT tenant-scoped, RLS-off at
runtime like the rest of the control plane. See docs/evals-directive.md.

INVARIANT #1 (the one that makes this system safe to run in production):
**No real-user content ever enters the eval pipeline.** Eval cases run against
SYNTHETIC tenants, and the rows below store COUNTS, IDS, DURATIONS and
PASS/FAIL — never message bodies, journal text, names, or any user data. That
is why this composes with encryption-at-rest by construction: there is nothing
here to encrypt, because there is nothing here that came from a real user.
"""

from django.db import models


class EvalRun(models.Model):
    """One invocation of one eval suite."""

    class Trigger(models.TextChoices):
        SCHEDULED = "scheduled", "Scheduled"
        MANUAL = "manual", "Manual"
        ROLLOUT_GATE = "rollout_gate", "Rollout gate"

    class Status(models.TextChoices):
        RUNNING = "running", "Running"
        PASS = "pass", "Pass"
        DEGRADED = "degraded", "Degraded"
        FAIL = "fail", "Fail"
        ERROR = "error", "Error"

    suite = models.CharField(max_length=64, help_text="Suite name, e.g. 'eval_smoke', 'journey', 'behavior'.")
    trigger = models.CharField(max_length=16, choices=Trigger.choices)
    git_sha = models.CharField(
        max_length=40, blank=True, default="", help_text="Control-plane build the run executed on."
    )
    image_tag = models.CharField(
        max_length=128,
        null=True,
        blank=True,
        help_text="OpenClaw image tag under test (null when the suite doesn't exercise the runtime).",
    )
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.RUNNING)
    started_at = models.DateTimeField(auto_now_add=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "eval_runs"
        ordering = ["-started_at"]
        indexes = [models.Index(fields=["suite", "-started_at"])]

    def __str__(self) -> str:
        return f"EvalRun({self.suite}, {self.status}, {self.started_at:%Y-%m-%d %H:%M})"


class EvalResult(models.Model):
    """One case outcome inside a run.

    ``details`` is a JSONB metadata sidecar for triage. **Counts, ids and
    durations ONLY — never message content, prompts, replies, journal text,
    names, or any other user data** (INVARIANT #1, see the module docstring).
    A failing case explains itself with numbers and identifiers: which probe,
    how long it took, how many rows it saw, which threshold it missed. If you
    are tempted to put a transcript in here, the case is asking the wrong
    question — assert on a computed property instead.
    """

    class Kind(models.TextChoices):
        JOURNEY = "journey", "Journey"
        BEHAVIOR = "behavior", "Behavior"
        CORPUS = "corpus", "Corpus"
        SLO = "slo", "SLO"
        # Chassis smoke — kept out of the corpus aggregation so eval_smoke rows
        # don't pollute the deterministic-corpus pass/fail counts.
        SMOKE = "smoke", "Smoke"

    run = models.ForeignKey(EvalRun, on_delete=models.CASCADE, related_name="results")
    case_id = models.CharField(max_length=64)
    kind = models.CharField(max_length=16, choices=Kind.choices)
    passed = models.BooleanField()
    # max_digits=12 so a Suite-4 SLO metric (reply-latency p95 in ms, e.g. 4500)
    # fits alongside a 1-5 judge score without forcing a details-hack.
    score = models.DecimalField(max_digits=12, decimal_places=3, null=True, blank=True)
    threshold = models.DecimalField(max_digits=12, decimal_places=3, null=True, blank=True)
    details = models.JSONField(
        default=dict,
        blank=True,
        help_text="Triage metadata: COUNTS / IDS / DURATIONS ONLY. Never message content or user data.",
    )
    judge_model = models.CharField(max_length=128, blank=True, default="")
    rubric_version = models.CharField(max_length=32, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "eval_results"
        ordering = ["created_at"]
        indexes = [models.Index(fields=["run", "passed"])]

    def __str__(self) -> str:
        return f"EvalResult({self.case_id}, {'pass' if self.passed else 'FAIL'})"
