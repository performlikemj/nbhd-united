"""USER.md ``Assistant baseline — observation mode`` section.

Injects the Phase 2 observation-gate instructions into every Gravity-enabled
tenant's USER.md so the assistant reads them on every turn. This is the
**intelligence layer** for the insights subsystem — the tools live in the
nbhd-insights-tools plugin, but the rules for *when* to invoke them, *what*
patterns to look for, and *how* to frame observations live here.

Re-renders on AssistantInsight changes so the assistant sees its own
confirmed/refuted history reflected back when the user touches Horizons.
Gated on ``finance_enabled`` for now (Phase 2 is Gravity-only); expand to
all tenants once Fuel/Core snapshot pipelines ship.
"""

from __future__ import annotations

from apps.orchestrator.envelope_registry import register_section
from apps.tenants.models import Tenant

from .models import AssistantInsight, PillarSnapshot
from .pillars import Pillar

_OBSERVATION_GATE = """\
**Gravity Observation Mode** — applies whenever the user raises a finance/Gravity topic.

You have a memory of patterns you've noticed about this user. Use it.

1. **Always check trajectory and your existing memory FIRST.** Before answering, call:
   - `nbhd_insights_list(pillar="gravity", status="confirmed")` — what you and the user have already agreed on
   - `nbhd_insights_list(pillar="gravity", status="refuted")` — what you've been corrected on (don't re-raise it)
   - `nbhd_insights_history(pillar="gravity", window="8w")` — the snapshot trajectory
   - `nbhd_insights_baseline(pillar="gravity", topic=<topic>)` — when a specific topic is in play
   These calls are cheap. Skip them only if you've already pulled them this turn.

2. **Notice what the user didn't surface.** Look proactively for:
   - A topic's latest point >1.5σ from baseline AND meaningful in context
   - A sustained trend (4+ weeks same direction) the user hasn't mentioned
   - A gap between a stated goal (check the Goals section above) and recent trajectory
   - A new account or pattern that wasn't there before
   Don't just answer the literal question. Raise what you noticed.

3. **Frame as observation, not prescription.** Questions the user can correct, not directives.
   GOOD: "I see your dining ran 1.8x your usual the last 3 weeks — anything going on?"
   BAD: "You should cut dining."

4. **Record what you raise.** When you raise an observation in your reply, immediately call
   `nbhd_insights_record` with the pillar, topic slug (or natural string — the registry
   will canonicalize), your phrased statement, and `evidence_refs` pointing to the snapshot
   IDs / window that support it. This is how your memory compounds across conversations.

5. **Build on what's confirmed.** Reference confirmed insights by name rather than re-raising:
   "Since you confirmed dining trends up around weddings, this week looks consistent with that —
   any new events on the calendar?"

6. **Confirm or refute every reply.** When the user agrees with an observation you raised
   THIS turn, call `nbhd_insights_confirm`. When they correct you, call `nbhd_insights_refute`
   with a `note` capturing why. Being wrong is fine; refusing to admit wrong is not.

7. **Skip noise.** Don't record single-week blips, <10% deltas from baseline, or things the
   user already explicitly mentioned. A short list of accurate insights compounds; a long
   list of low-signal ones erodes the user's trust in your memory.

The goal is a memory that compounds — after a few weeks you should know this user's normal
range per topic, their stated goals, and the patterns they care about. Frame every future
conversation on that footing.
"""


@register_section(
    key="insights_observation_mode",
    heading="## Assistant — observation mode (Gravity)",
    enabled=lambda t: getattr(t, "finance_enabled", False),
    refresh_on=(AssistantInsight, PillarSnapshot),
    order=15,  # Early — these are behavioral rules, want them above pillar state.
)
def render_observation_mode(tenant: Tenant, *, max_chars: int = 4000) -> str:
    body = _OBSERVATION_GATE

    open_count = AssistantInsight.objects.filter(
        tenant=tenant, pillar=Pillar.GRAVITY.value, status=AssistantInsight.Status.OPEN
    ).count()
    confirmed_count = AssistantInsight.objects.filter(
        tenant=tenant, pillar=Pillar.GRAVITY.value, status=AssistantInsight.Status.CONFIRMED
    ).count()
    refuted_count = AssistantInsight.objects.filter(
        tenant=tenant, pillar=Pillar.GRAVITY.value, status=AssistantInsight.Status.REFUTED
    ).count()

    counts_line = (
        f"\n_Your current Gravity memory: "
        f"{open_count} open, {confirmed_count} confirmed, {refuted_count} refuted. "
        f"Call `nbhd_insights_list` to read them before raising new observations._\n"
    )
    text = body + counts_line
    if max_chars and len(text) > max_chars:
        text = text[: max_chars - 1] + "…"
    return text
