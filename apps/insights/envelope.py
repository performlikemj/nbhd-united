"""Gravity observation-mode prompt + USER.md memory-counts section.

``_OBSERVATION_GATE`` is the always-loaded **intelligence layer** for the
insights subsystem — the tools live in the nbhd-insights-tools plugin, but
the rules for *when* to invoke them, *what* patterns to look for, and *how*
to frame observations live here. It is exposed via
:func:`render_observation_mode_rules` so
``apps.orchestrator.personas.render_workspace_files`` can append it to
**AGENTS.md** (where per-turn behavioral rules belong per docs.openclaw.ai).

The voice-register selection rules used to live here too, as a second
~3.4 KB constant appended to AGENTS.md. They no longer do. A
finance + friends-propose tenant renders ~24.9 KB of AGENTS.md — over
OpenClaw's ``BOOTSTRAP_MAX_CHARS`` (24 KB) injection cap — so the register
block, ordered last as the sacrificial tail, was being silently truncated
mid-rules on the two real tenants in that shape. Moving it to a
``rules/X.md`` pointer is out: the assistant loads a referenced rules file
at most once per conversation and won't re-reference it (the "white whale"
— see feedback_ondemand_rules_files_unreliable). Instead the register rules
now ride the ``nbhd_insights_signals`` tool response
(:data:`apps.insights.signals.REGISTER_GUIDANCE`), delivered DETERMINISTICALLY
at the moment of need — the gate below already mandates that call per
finance topic, and that mandate survives truncation. The gate says so in
one line so the assistant knows to read the guidance off each signals call.

USER.md carries only the small dynamic counts block (via the
``insights_observation_mode`` envelope section) — the agent's own
confirmed/refuted memory reflected back.

Gated on ``finance_enabled`` for now (Phase 2 is Gravity-only); expand
to all tenants once Fuel/Core snapshot pipelines ship.
"""

from __future__ import annotations

from apps.orchestrator.envelope_registry import register_section
from apps.tenants.models import Tenant

from .models import AssistantInsight, PillarSnapshot
from .pillars import Pillar

_OBSERVATION_GATE = """\
**Gravity Observation Mode** — whenever the user raises a finance/Gravity topic. You hold a memory of this user's patterns; use it.

1. **Pull memory + trajectory FIRST, before answering:** `nbhd_insights_list(pillar="gravity", status="confirmed")` (agreed), `nbhd_insights_list(pillar="gravity", status="refuted")` (corrected — don't re-raise), `nbhd_insights_history(pillar="gravity", window="8w")` (trajectory), and `nbhd_insights_baseline(pillar="gravity", topic=<topic>)` per topic in play. Cheap; skip only what you already pulled this turn.

2. **Per topic, call `nbhd_insights_signals(pillar, topic)`.** Its response carries the voice-register guidance — read it and pick your register (observation / hypothesis / soft / direct) from it every turn. Register shapes only HOW you frame the reply; it overrides nothing below.

3. **Surface what the user didn't.** Watch for: a latest point >1.5σ from baseline and contextually meaningful; a sustained 4+ week trend they haven't named; a gap between a stated goal (Goals section above) and the trajectory; a new account or pattern. Don't just answer the literal question — raise it.

4. **Observation, not prescription** — something they can correct. GOOD: "Your dining ran 1.8x usual the last 3 weeks — anything going on?" BAD: "You should cut dining."

5. **Record what you raise:** `nbhd_insights_record` with pillar, topic slug (or a natural string — it canonicalizes), your statement, and `evidence_refs` (the snapshot IDs / window behind it). This is how memory compounds.

6. **Build on confirmed insights** — reference them by name instead of re-raising.

7. **Confirm or refute every reply.** User agrees with something you raised THIS turn → `nbhd_insights_confirm`; corrects you → `nbhd_insights_refute` with a `note` on why. Being wrong is fine; not admitting it isn't.

8. **Skip noise** — no single-week blips, no <10% deltas, nothing they already mentioned. A short accurate list compounds; a long low-signal one erodes trust.

Aim: a memory that compounds — within a few weeks you know this user's normal range per topic, their goals, and the patterns they care about.
"""


def render_observation_mode_rules(tenant: Tenant) -> str:
    """Return the Gravity observation-mode prompt block.

    Imported by :func:`apps.orchestrator.personas.render_workspace_files`
    and appended to AGENTS.md when ``tenant.finance_active`` is true.
    Behavioral rules that fire on EVERY finance reply belong in AGENTS.md
    per the OpenClaw workspace docs; this is the trimmed gate.

    The voice-register selection rules are NOT here anymore — they ride the
    ``nbhd_insights_signals`` tool response (:data:`apps.insights.signals.REGISTER_GUIDANCE`)
    so they reach the model deterministically at the moment of need,
    without spending always-loaded bootstrap budget that pushed this block
    past the injection cap on finance + friends-propose tenants. The gate
    tells the assistant to read that guidance off each signals call.
    """
    return _OBSERVATION_GATE


@register_section(
    key="insights_observation_mode",
    heading="## Assistant — observation memory (Gravity)",
    enabled=lambda t: getattr(t, "finance_active", False),
    refresh_on=(AssistantInsight, PillarSnapshot),
    order=15,
)
def render_observation_mode(tenant: Tenant, *, max_chars: int = 600) -> str:
    """Tiny USER.md section: counts only.

    The behavioral rules (observation gate + register selection) live in
    AGENTS.md now — see :func:`render_observation_mode_rules` and
    ``apps.orchestrator.personas.render_workspace_files``. This section
    just surfaces the agent's current memory state so it sees its own
    confirmed/refuted history reflected back when the user touches
    Horizons.
    """
    open_count = AssistantInsight.objects.filter(
        tenant=tenant, pillar=Pillar.GRAVITY.value, status=AssistantInsight.Status.OPEN
    ).count()
    confirmed_count = AssistantInsight.objects.filter(
        tenant=tenant, pillar=Pillar.GRAVITY.value, status=AssistantInsight.Status.CONFIRMED
    ).count()
    refuted_count = AssistantInsight.objects.filter(
        tenant=tenant, pillar=Pillar.GRAVITY.value, status=AssistantInsight.Status.REFUTED
    ).count()

    text = (
        f"_Gravity memory: {open_count} open, {confirmed_count} confirmed, {refuted_count} refuted. "
        f"Call `nbhd_insights_list` to read them. "
        f'See AGENTS.md → "Gravity Observation Mode" for the gate; the voice-register rules ride each '
        f"`nbhd_insights_signals` response._"
    )
    if max_chars and len(text) > max_chars:
        text = text[: max_chars - 1] + "…"
    return text
