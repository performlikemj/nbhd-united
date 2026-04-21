"""Seed sample lessons for local development / UI testing.

Creates ~25 lessons across 6 clusters with connections, mimicking
what a real user's constellation looks like. No LLM or embedding calls.

Usage:
    python manage.py seed_sample_lessons --tenant <uuid>
    python manage.py seed_sample_lessons --tenant <uuid> --clear
"""

from __future__ import annotations

import random

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from apps.lessons.models import Lesson, LessonConnection
from apps.tenants.models import Tenant

CLUSTERS = [
    (0, "Creative Discipline", "The Forge"),
    (1, "Deep Relationships", "The Hearth"),
    (2, "Body & Energy", "The Current"),
    (3, "Leadership & Teams", "The Compass"),
    (4, "Money & Time", "The Ledger"),
    (5, "Self-Knowledge", "The Mirror"),
]

LESSONS = [
    # Creative Discipline
    (0, "Ship small, ship often. A rough version shipped teaches more than a perfect one planned.",
     "Noticed after the Q1 launch — everything I actually shipped was an early, imperfect cut.",
     ["shipping", "creative", "momentum"], "reflection", "Weekly review 2026-03-14"),
    (0, "Taste is built by looking, not by thinking about looking.",
     "", ["creative", "craft"], "conversation", "Chat Mar 22"),
    (0, "The first 45 minutes of the day decide the whole day. Guard them.",
     "Pattern across 5 weekly reviews — the days I journaled first were 3x more productive.",
     ["mornings", "focus"], "journal", "Daily Feb 01"),
    (0, "Constraints are the medium. Remove them and the work goes soft.",
     "", ["creative", "craft"], "article", "Rick Rubin — The Creative Act"),
    (0, "Quantity breeds quality. Make ten bad ones to find one great one.",
     "", ["creative", "practice"], "experience", "Studio Mar 08"),
    # Deep Relationships
    (1, "People don't need advice — they need to feel seen. Ask one more question before answering.",
     "Kept happening in 1:1s. Every time I jumped to a fix, the conversation died.",
     ["relationships", "listening"], "reflection", "1:1 debrief"),
    (1, "Saying I miss you out loud, soon, matters more than saying it well later.",
     "", ["relationships", "family"], "journal", "Call with Mom"),
    (1, "Repair is cheap if done fast. Expensive if you let it age.",
     "", ["relationships", "conflict"], "experience", "Mar 18"),
    (1, "Show up for the small things. Big gestures can't make up for not being there on Tuesdays.",
     "", ["relationships", "presence"], "conversation", "Chat Feb 11"),
    # Body & Energy
    (2, "Sleep is not a variable. Everything downstream collapses when it slips below 7.",
     "Tracked for 6 weeks — mood, output, patience all correlate sharply with sleep.",
     ["sleep", "health", "energy"], "reflection", "Week of Mar 28"),
    (2, "Walk before thinking hard. Movement unlocks what the desk can't.",
     "", ["movement", "thinking"], "experience", "Morning walks"),
    (2, "Caffeine after 2pm steals tomorrow to pay for today.",
     "", ["sleep", "caffeine"], "experience", "Feb experiment"),
    (2, "Protein at breakfast buys a steadier afternoon.",
     "", ["food", "energy"], "article", "Huberman — energy stability"),
    # Leadership
    (3, "Hire for how they disagree, not how they agree.",
     "Three good hires, three poor ones — the tell was always how they handled pushback.",
     ["hiring", "leadership"], "reflection", "Hiring retro"),
    (3, "Clarity is kindness. Vague feedback is cowardice in a nice outfit.",
     "", ["leadership", "feedback"], "conversation", "Coaching session"),
    (3, "The thing you're avoiding saying is usually the thing worth saying first.",
     "", ["leadership", "honesty"], "experience", "Mar 05"),
    (3, "Context beats instructions. Give the why, trust the how.",
     "", ["leadership", "delegation"], "reflection", "Team offsite"),
    # Money & Time
    (4, "Calendar is the budget that actually matters. Money just tells you what happened yesterday.",
     "", ["time", "money"], "journal", "Monthly review"),
    (4, "Say no to things you'd say yes to at 10pm on Tuesday.",
     "Calibration trick — if future-me wouldn't take it, past-me shouldn't commit to it.",
     ["time", "commitments"], "reflection", "Jan planning"),
    (4, "Cheap tools are expensive. Pay for the thing you'll touch 500 times this year.",
     "", ["money", "tools"], "experience", "Jan"),
    (4, "Boring money, interesting life. Reverse the ratio and you suffer.",
     "", ["money"], "article", "Housel — Psychology of Money"),
    # Self-Knowledge
    (5, "I don't have a motivation problem. I have a clarity problem in a motivation costume.",
     "Every time I called myself lazy, I didn't actually know what I was supposed to do next.",
     ["self", "clarity"], "reflection", "Therapy Feb"),
    (5, "The version of me that shows up at 6am is not the same person who wrote the plan at 11pm.",
     "", ["self", "planning"], "journal", "Daily Mar 12"),
    (5, "When in doubt, choose the harder conversation. Softness now, hardness later.",
     "", ["self", "honesty"], "experience", "Feb 19"),
    (5, "I regret what I didn't try more than what I tried and failed. Every single time.",
     "", ["self", "risk"], "reflection", "Birthday reflection"),
]

# (from_idx, to_idx, similarity) — indices into LESSONS
CONNECTIONS = [
    # Within-cluster
    (0, 2, 0.82), (0, 4, 0.78), (1, 3, 0.71), (2, 4, 0.69), (3, 1, 0.65),
    (5, 7, 0.84), (5, 8, 0.77), (6, 8, 0.73),
    (9, 11, 0.88), (9, 10, 0.74), (10, 12, 0.58),
    (13, 14, 0.81), (14, 15, 0.79), (13, 16, 0.72), (15, 16, 0.68),
    (17, 18, 0.80), (18, 20, 0.64), (19, 20, 0.59),
    (21, 22, 0.86), (21, 24, 0.75), (22, 23, 0.70), (23, 24, 0.66),
    # Cross-cluster bridges
    (0, 13, 0.54), (2, 9, 0.51), (21, 2, 0.56), (21, 17, 0.53),
    (5, 14, 0.58), (23, 15, 0.49), (18, 16, 0.47),
]


class Command(BaseCommand):
    help = "Create sample lessons + connections for local UI testing"

    def add_arguments(self, parser):
        parser.add_argument("--tenant", type=str, required=True, help="Tenant UUID")
        parser.add_argument("--clear", action="store_true", help="Delete existing lessons first")

    def handle(self, *args, **options):
        try:
            tenant = Tenant.objects.get(id=options["tenant"])
        except Tenant.DoesNotExist:
            raise CommandError(f"Tenant {options['tenant']} not found")

        if options["clear"]:
            deleted, _ = Lesson.objects.filter(tenant=tenant).delete()
            self.stdout.write(f"Cleared {deleted} existing lessons")

        # Create lessons
        rng = random.Random(42)
        created_lessons: list[Lesson] = []
        for cluster_id, text, context, tags, source_type, source_ref in LESSONS:
            _, cluster_label, _ = CLUSTERS[cluster_id]
            lesson = Lesson.objects.create(
                tenant=tenant,
                text=text,
                context=context,
                tags=tags,
                cluster_id=cluster_id,
                cluster_label=cluster_label,
                source_type=source_type,
                source_ref=source_ref,
                status="approved",
                approved_at=timezone.now(),
                # Random 2D positions within cluster neighborhoods
                position_x=round(rng.uniform(-0.8, 0.8), 3),
                position_y=round(rng.uniform(-0.8, 0.8), 3),
            )
            created_lessons.append(lesson)

        self.stdout.write(self.style.SUCCESS(f"Created {len(created_lessons)} lessons across {len(CLUSTERS)} clusters"))

        # Create connections
        conn_count = 0
        for from_idx, to_idx, similarity in CONNECTIONS:
            if from_idx < len(created_lessons) and to_idx < len(created_lessons):
                from_l = created_lessons[from_idx]
                to_l = created_lessons[to_idx]
                same_cluster = from_l.cluster_id == to_l.cluster_id
                LessonConnection.objects.create(
                    from_lesson=from_l,
                    to_lesson=to_l,
                    similarity=similarity,
                    connection_type="similar" if same_cluster else "builds_on",
                )
                conn_count += 1

        self.stdout.write(self.style.SUCCESS(f"Created {conn_count} connections"))
        self.stdout.write(f"\nDone. Visit /constellation to see the graph.")
