"""Behavior scenario schema + loader with LOUD validation (Wave D).

Scenarios are YAML fixtures under ``apps/evals/behavior/scenarios/``. Each one is
a persona + a multi-turn script + deterministic ``hard_assertions`` +
subjective ``soft_dimensions``. A malformed fixture is a BROKEN SUITE, never a
silently skipped scenario (INVARIANT #3): the loader validates every field and
raises ``ScenarioValidationError`` on the first problem, reporting the file and
field — never guessing a default for a missing or wrong value.

The reply text a scenario elicits is SYNTHETIC (no real user), so it may flow to
the hard assertions and the judge — but nothing here or downstream writes it into
``EvalResult.details`` (INVARIANT #1).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml

from apps.evals.behavior.rubrics import rubric_v1

SCENARIOS_DIR = Path(__file__).resolve().parent / "scenarios"

# Deterministic hard-assertion types (implemented in assertions.py). A fixture
# naming any other type is rejected at load.
VALID_HARD_TYPES: frozenset[str] = frozenset({"reply_nonempty", "marker_absent", "cron_registered", "forbidden_absent"})
VALID_SOFT_DIMENSIONS: frozenset[str] = frozenset(rubric_v1.DIMENSIONS)

# The placeholder a scenario script uses to plant a fresh per-run marker.
MARKER_TOKEN = "{{marker}}"

# Bound the id so ``<id>::hard:<type>`` / ``<id>::soft:<dim>`` case ids stay under
# the chassis' 64-char cap without truncation-collisions (record() truncates).
_MAX_ID_LEN = 32
_ALLOWED_TOP_KEYS = frozenset({"id", "persona", "script", "hard_assertions", "soft_dimensions"})


class ScenarioValidationError(ValueError):
    """A scenario fixture is malformed — raised LOUDLY at load.

    A broken fixture is a broken suite, not a scenario to skip (INVARIANT #3). The
    message names the file and the offending field; it never invents a default.
    """


@dataclass(frozen=True)
class HardAssertion:
    """One deterministic check over observable evidence (DB rows / reply text)."""

    type: str
    forbidden: tuple[str, ...] = ()  # only for type == "forbidden_absent"


@dataclass(frozen=True)
class Scenario:
    id: str
    persona: str
    script: tuple[str, ...]
    hard_assertions: tuple[HardAssertion, ...]
    soft_dimensions: tuple[str, ...]

    @property
    def uses_marker(self) -> bool:
        """True iff the script plants a per-run marker via ``{{marker}}``."""
        return any(MARKER_TOKEN in line for line in self.script)


def _require(cond: bool, source: str, message: str) -> None:
    if not cond:
        raise ScenarioValidationError(f"{source}: {message}")


def _parse_hard_assertion(raw: object, source: str, script: list[str]) -> HardAssertion:
    _require(isinstance(raw, dict), source, "each hard assertion must be a mapping")
    assert isinstance(raw, dict)  # narrow for type-checkers; guarded above
    a_type = raw.get("type")
    _require(
        isinstance(a_type, str) and a_type in VALID_HARD_TYPES,
        source,
        f"hard assertion 'type' must be one of {sorted(VALID_HARD_TYPES)}, got {a_type!r}",
    )
    assert isinstance(a_type, str)

    forbidden: tuple[str, ...] = ()
    if a_type == "forbidden_absent":
        raw_forbidden = raw.get("forbidden")
        _require(
            isinstance(raw_forbidden, list) and len(raw_forbidden) > 0,
            source,
            "'forbidden_absent' requires a non-empty 'forbidden' list of strings",
        )
        assert isinstance(raw_forbidden, list)
        _require(
            all(isinstance(x, str) and x.strip() for x in raw_forbidden),
            source,
            "'forbidden' entries must be non-empty strings",
        )
        forbidden = tuple(x for x in raw_forbidden)
    elif a_type == "marker_absent":
        _require(
            any(MARKER_TOKEN in line for line in script),
            source,
            f"'marker_absent' requires the script to plant {MARKER_TOKEN}",
        )

    unexpected = set(raw) - {"type", "forbidden"}
    _require(not unexpected, source, f"unexpected keys in hard assertion: {sorted(unexpected)}")
    return HardAssertion(type=a_type, forbidden=forbidden)


def parse_scenario(data: object, *, source: str) -> Scenario:
    """Validate a parsed YAML document into a ``Scenario`` (raises on any problem)."""
    _require(isinstance(data, dict), source, "top-level document must be a mapping")
    assert isinstance(data, dict)

    unexpected = set(data) - _ALLOWED_TOP_KEYS
    _require(not unexpected, source, f"unexpected top-level keys: {sorted(unexpected)}")

    scenario_id = data.get("id")
    _require(isinstance(scenario_id, str) and scenario_id.strip(), source, "'id' must be a non-empty string")
    assert isinstance(scenario_id, str)
    scenario_id = scenario_id.strip()
    _require(len(scenario_id) <= _MAX_ID_LEN, source, f"'id' must be <= {_MAX_ID_LEN} chars, got {len(scenario_id)}")
    _require(
        "\n" not in scenario_id and "\r" not in scenario_id and ":" not in scenario_id,
        source,
        "'id' must be a single line with no ':' (it namespaces the case id)",
    )

    persona = data.get("persona")
    _require(isinstance(persona, str) and persona.strip(), source, "'persona' must be a non-empty string")
    assert isinstance(persona, str)

    raw_script = data.get("script")
    _require(isinstance(raw_script, list) and len(raw_script) > 0, source, "'script' must be a non-empty list")
    assert isinstance(raw_script, list)
    _require(
        all(isinstance(line, str) and line.strip() for line in raw_script),
        source,
        "each 'script' turn must be a non-empty string",
    )
    script = [str(line) for line in raw_script]

    raw_hard = data.get("hard_assertions")
    _require(
        isinstance(raw_hard, list) and len(raw_hard) > 0,
        source,
        "'hard_assertions' must be a non-empty list — a scenario with no deterministic "
        "check asserts nothing gating and is a broken suite (INVARIANT #3)",
    )
    assert isinstance(raw_hard, list)
    hard = tuple(_parse_hard_assertion(item, source, script) for item in raw_hard)

    raw_soft = data.get("soft_dimensions", [])
    _require(isinstance(raw_soft, list), source, "'soft_dimensions' must be a list")
    assert isinstance(raw_soft, list)
    for dim in raw_soft:
        _require(
            isinstance(dim, str) and dim in VALID_SOFT_DIMENSIONS,
            source,
            f"soft dimension {dim!r} must be one of {sorted(VALID_SOFT_DIMENSIONS)}",
        )
    soft = tuple(str(dim) for dim in raw_soft)
    _require(len(soft) == len(set(soft)), source, "duplicate soft dimensions")

    return Scenario(
        id=scenario_id,
        persona=persona.strip(),
        script=tuple(script),
        hard_assertions=hard,
        soft_dimensions=soft,
    )


def load_scenario(path: Path) -> Scenario:
    """Load and validate one scenario YAML file (raises ``ScenarioValidationError``)."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ScenarioValidationError(f"{path.name}: cannot read fixture ({type(exc).__name__})") from exc
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        # Report the error TYPE only — never echo the raw content back.
        raise ScenarioValidationError(f"{path.name}: invalid YAML ({type(exc).__name__})") from exc
    return parse_scenario(data, source=path.name)


def load_all_scenarios(directory: Path | None = None) -> list[Scenario]:
    """Load every ``*.yaml``/``*.yml`` scenario, sorted by filename, with unique ids
    enforced. Both extensions are globbed so a fixture saved as ``.yml`` cannot be
    silently ignored (a dropped scenario is a quiet coverage loss)."""
    directory = directory or SCENARIOS_DIR
    paths = sorted([*directory.glob("*.yaml"), *directory.glob("*.yml")])
    scenarios: list[Scenario] = []
    seen: dict[str, str] = {}
    for path in paths:
        scenario = load_scenario(path)
        if scenario.id in seen:
            raise ScenarioValidationError(
                f"{path.name}: duplicate scenario id {scenario.id!r} (also in {seen[scenario.id]})"
            )
        seen[scenario.id] = path.name
        scenarios.append(scenario)
    return scenarios
