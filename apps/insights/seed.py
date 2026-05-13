"""Seed data for the topic registry.

Each pillar gets a starter set of canonical topics drawn from what the
corresponding tab UI surfaces today. The assistant proposes additional topics
as it observes user-specific patterns; ops promotes them.
"""

from __future__ import annotations

from django.db import transaction

from .models import TopicAlias, TopicRegistry
from .pillars import Pillar

# (slug, display_name, description, [alias, ...])
SeedRow = tuple[str, str, str, list[str]]

SEED_TOPICS: dict[str, list[SeedRow]] = {
    Pillar.GRAVITY.value: [
        ("income", "Income", "Wages, salary, and other income streams.", ["earnings", "paycheck"]),
        (
            "fixed_expenses",
            "Fixed Expenses",
            "Rent, utilities, insurance, and other recurring bills.",
            ["bills", "rent"],
        ),
        (
            "subscriptions",
            "Subscriptions",
            "Recurring software, media, and membership fees.",
            ["recurring charges", "memberships"],
        ),
        (
            "discretionary",
            "Discretionary Spending",
            "Variable spending across non-essential categories.",
            ["variable spending"],
        ),
        (
            "dining",
            "Dining",
            "Restaurants, takeout, coffee, and food delivery.",
            ["eating out", "restaurants", "takeout", "food delivery"],
        ),
        (
            "debt",
            "Debt",
            "Credit cards, loans, and balances carrying interest.",
            ["loans", "credit card"],
        ),
        (
            "savings",
            "Savings",
            "Funds set aside, emergency reserves, investments.",
            ["emergency fund"],
        ),
        (
            "large_purchases",
            "Large Purchases",
            "Single transactions above the user's discretionary norm.",
            ["big buys"],
        ),
    ],
    Pillar.FUEL.value: [
        (
            "sleep_quantity",
            "Sleep Quantity",
            "Hours of sleep per night.",
            ["sleep duration", "sleep hours"],
        ),
        (
            "sleep_quality",
            "Sleep Quality",
            "Restfulness and continuity of sleep.",
            ["restfulness", "sleep restfulness"],
        ),
        ("hydration", "Hydration", "Daily water intake.", ["water intake", "fluids"]),
        ("meals", "Meals", "Meal cadence, composition, and timing.", ["eating", "diet"]),
        (
            "energy_level",
            "Energy Level",
            "Subjective energy across the day.",
            ["energy", "alertness"],
        ),
        (
            "exercise",
            "Exercise",
            "Workouts, activity, and recovery.",
            ["workouts", "training", "activity"],
        ),
        ("alcohol", "Alcohol", "Drinking patterns and frequency.", ["drinking"]),
        ("caffeine", "Caffeine", "Coffee, tea, and stimulant intake.", ["coffee", "tea"]),
    ],
}


@transaction.atomic
def seed_topics() -> dict[str, int]:
    """Upsert the seed topic registry. Idempotent.

    Returns a count of topics and aliases newly created per pillar.
    """
    counts: dict[str, int] = {}
    for pillar_value, rows in SEED_TOPICS.items():
        created_topics = 0
        created_aliases = 0
        for slug, display_name, description, aliases in rows:
            topic, was_created = TopicRegistry.objects.get_or_create(
                pillar=pillar_value,
                slug=slug,
                defaults={
                    "display_name": display_name,
                    "description": description,
                    "status": TopicRegistry.Status.CANONICAL,
                    "source": TopicRegistry.Source.SEED,
                },
            )
            if was_created:
                created_topics += 1
            for alias in aliases:
                _, alias_created = TopicAlias.objects.get_or_create(
                    topic=topic,
                    alias=alias,
                    defaults={"source": TopicAlias.Source.SEED},
                )
                if alias_created:
                    created_aliases += 1
        counts[pillar_value] = created_topics
        counts[f"{pillar_value}__aliases"] = created_aliases
    return counts
