"""Shape contract for generated ``openclaw.json`` configs.

OpenClaw's runtime is forgiving on unknown keys and its redactor masks
schema-validation warnings on stdout/stderr — so a misspelled key or a
wrong-enum value in ``apps/orchestrator/config_generator.py`` can ship
silently and break a feature without surfacing in tests or logs (see
``feedback_openclaw_config_schema_check.md``).

This test pins the *shape* of the keys we explicitly emit, sourced from
``npm pack openclaw@<canary-version>`` schema inspection done at merge
time. It does NOT validate against the full OpenClaw schema — that would
require a Node sidecar in CI. It catches:

  - typos / casing drift in keys we own
  - wrong enum values (e.g. ``promptStyle: "balansed"``)
  - out-of-range numerics
  - regressions where a new tenant flag flips a value to the wrong type

Pre-merge discipline still applies: when adding or changing a key here,
extract the canary's OpenClaw version source via ``npm pack openclaw@<v>``
and grep ``dist/`` to confirm the schema. Then add an assertion below.

Source-of-truth references (OpenClaw 2026.5.7, the canary version at
this test's authoring time) — re-verify when the canary bumps:

  - commitments shape:  ``dist/runtime-schema-OL6hE5dN.js:18704-18711``
  - heartbeat fallback: ``dist/heartbeat-runner-DpQCcYf2.js:365``
  - activeHours shape:  ``dist/heartbeat-runner-DpQCcYf2.js:297-302``
  - memorySearch store: ``dist/memory-search-DbWvVOpI.js:37-42``
                        (``{agentId}`` token IS interpolated)
  - active-memory enums: ``dist/extensions/active-memory/openclaw.plugin.json``
"""

from __future__ import annotations

import re
from typing import Any

from django.test import TestCase

from apps.billing.constants import (
    ANTHROPIC_SONNET_MODEL,
    DEEPSEEK_FLASH_MODEL,
    DEEPSEEK_MODEL,
    GEMMA_MODEL,
)
from apps.byo_models.models import BYOCredential
from apps.tenants.services import create_tenant

from .config_generator import generate_openclaw_config
from .config_validator import assert_config_writable

# OpenClaw-documented enum values for the keys we set. Sourced from
# ``npm pack openclaw@2026.5.7`` — see file docstring for paths.
_HEARTBEAT_TARGET_ENUM = {"none", "last"}
_HEARTBEAT_EVERY_PATTERN = re.compile(r"^\d+[smhd]$")  # e.g. "30m", "1h"
_TIME_HHMM = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")  # 24h HH:MM
_ACTIVE_MEMORY_QUERY_MODES = {"message", "recent", "full"}
_ACTIVE_MEMORY_PROMPT_STYLES = {
    "balanced",
    "strict",
    "contextual",
    "recall-heavy",
    "precision-heavy",
    "preference-only",
}
_ACTIVE_MEMORY_ALLOWED_CHAT_TYPES = {"direct", "group", "channel", "explicit"}
_FTS_TOKENIZER_ENUM = {"unicode61", "trigram"}


def _get(config: dict, dotted: str) -> Any:
    """Walk a dotted path; return ``None`` if any segment is missing."""
    cur: Any = config
    for part in dotted.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return None
        cur = cur[part]
    return cur


class OpenclawSchemaShapeTest(TestCase):
    """Generated config must match the verified OpenClaw schema shapes.

    These assertions are intentionally narrow: they only check keys
    ``config_generator.py`` explicitly emits. Anything OpenClaw adds at
    runtime (defaults filled in by the gateway) is out of scope.
    """

    def setUp(self):
        self.tenant = create_tenant(
            display_name="SchemaShape",
            telegram_chat_id=998877,
        )
        self.config = generate_openclaw_config(self.tenant)

    # ── Heartbeat ─────────────────────────────────────────────────────

    def test_heartbeat_every_matches_pattern(self):
        every = _get(self.config, "agents.defaults.heartbeat.every")
        self.assertIsNotNone(every, "agents.defaults.heartbeat.every missing")
        self.assertRegex(every, _HEARTBEAT_EVERY_PATTERN)

    def test_heartbeat_target_is_valid_enum_when_set(self):
        target = _get(self.config, "agents.defaults.heartbeat.target")
        if target is not None:
            self.assertIn(target, _HEARTBEAT_TARGET_ENUM)

    def test_heartbeat_active_hours_shape_when_set(self):
        active = _get(self.config, "agents.defaults.heartbeat.activeHours")
        if active is None:
            return
        self.assertIsInstance(active, dict)
        self.assertIn("start", active)
        self.assertIn("end", active)
        self.assertRegex(active["start"], _TIME_HHMM)
        self.assertRegex(active["end"], _TIME_HHMM)
        if "timezone" in active:
            self.assertIsInstance(active["timezone"], str)

    def test_heartbeat_ack_max_chars_is_nonneg_int_when_set(self):
        ack = _get(self.config, "agents.defaults.heartbeat.ackMaxChars")
        if ack is not None:
            self.assertIsInstance(ack, int)
            self.assertGreaterEqual(ack, 0)

    # ── Commitments ───────────────────────────────────────────────────

    def test_commitments_shape_when_set(self):
        commitments = self.config.get("commitments")
        if commitments is None:
            return
        self.assertIsInstance(commitments, dict)
        if "enabled" in commitments:
            self.assertIsInstance(commitments["enabled"], bool)
        if "maxPerDay" in commitments:
            self.assertIsInstance(commitments["maxPerDay"], int)
            # Plausibility band — OpenClaw default is 3, anything outside
            # 1–50 is almost certainly a bug, not a legitimate config.
            self.assertGreaterEqual(commitments["maxPerDay"], 1)
            self.assertLessEqual(commitments["maxPerDay"], 50)

    # ── memorySearch ──────────────────────────────────────────────────

    def test_memory_search_enabled_is_bool(self):
        enabled = _get(self.config, "agents.defaults.memorySearch.enabled")
        self.assertIsInstance(enabled, bool)

    def test_memory_search_store_path_is_string_when_set(self):
        path = _get(self.config, "agents.defaults.memorySearch.store.path")
        if path is None:
            return
        self.assertIsInstance(path, str)
        # If we use the ``{agentId}`` token, it must be the exact literal
        # OpenClaw replaces — a typo like ``{agent_id}`` would silently
        # leave the literal in the path and write everyone to one file.
        if "{" in path:
            self.assertIn("{agentId}", path, msg=f"unexpected token in {path}")

    def test_memory_search_fts_tokenizer_when_set(self):
        tok = _get(self.config, "agents.defaults.memorySearch.store.fts.tokenizer")
        if tok is not None:
            self.assertIn(tok, _FTS_TOKENIZER_ENUM)

    # ── active-memory plugin ──────────────────────────────────────────

    def test_active_memory_plugin_shape_when_present(self):
        plugin = _get(self.config, "plugins.entries.active-memory")
        if plugin is None:
            return
        self.assertIsInstance(plugin, dict)
        self.assertIn("enabled", plugin)
        self.assertIsInstance(plugin["enabled"], bool)
        cfg = plugin.get("config")
        if cfg is None:
            return
        if "queryMode" in cfg:
            self.assertIn(cfg["queryMode"], _ACTIVE_MEMORY_QUERY_MODES)
        if "promptStyle" in cfg:
            self.assertIn(cfg["promptStyle"], _ACTIVE_MEMORY_PROMPT_STYLES)
        if "allowedChatTypes" in cfg:
            self.assertIsInstance(cfg["allowedChatTypes"], list)
            for v in cfg["allowedChatTypes"]:
                self.assertIn(v, _ACTIVE_MEMORY_ALLOWED_CHAT_TYPES)
        if "timeoutMs" in cfg:
            self.assertIsInstance(cfg["timeoutMs"], int)
            self.assertGreaterEqual(cfg["timeoutMs"], 250)
            self.assertLessEqual(cfg["timeoutMs"], 120_000)
        if "setupGraceTimeoutMs" in cfg:
            self.assertIsInstance(cfg["setupGraceTimeoutMs"], int)
            self.assertGreaterEqual(cfg["setupGraceTimeoutMs"], 0)
            self.assertLessEqual(cfg["setupGraceTimeoutMs"], 30_000)
        if "maxSummaryChars" in cfg:
            self.assertIsInstance(cfg["maxSummaryChars"], int)
            self.assertGreater(cfg["maxSummaryChars"], 0)
        if "agents" in cfg:
            self.assertIsInstance(cfg["agents"], list)
            self.assertTrue(all(isinstance(a, str) for a in cfg["agents"]))

    # ── Bootstrap budget ──────────────────────────────────────────────

    def test_bootstrap_max_chars_set_above_default(self):
        """Per-file bootstrap budget must exceed OC's 12 000 default.

        USER.md routinely runs ~15 KB once the insights observation-mode
        prompt + per-tenant goals/tasks/journal are rendered. At the
        default 12 000, OC silently truncates the tail (Privacy
        Placeholders, Recent journal, Fuel/Gravity state) before
        injection. Schema-shape verified via openclaw@2026.5.20
        ``dist/pi-embedded-helpers-*.js`` reading
        ``agents.defaults.bootstrapMaxChars``.
        """
        v = _get(self.config, "agents.defaults.bootstrapMaxChars")
        self.assertIsInstance(v, int)
        self.assertGreater(v, 12000)

    def test_bootstrap_total_max_chars_set_above_default(self):
        """Total bootstrap budget must exceed OC's 60 000 default."""
        v = _get(self.config, "agents.defaults.bootstrapTotalMaxChars")
        self.assertIsInstance(v, int)
        self.assertGreater(v, 60000)

    # ── PDF tool pin ──────────────────────────────────────────────────

    def test_pdf_model_pinned_to_vision_model_for_platform_tenants(self):
        """The built-in ``pdf`` tool only registers when a PDF-capable model
        resolves, and its factory-availability check has NO "resolved session
        model has vision" fast-path (unlike the ``image`` tool). So we pin
        ``agents.defaults.pdfModel``.

        The pin used to MIRROR ``agents.defaults.model``, which shipped broken:
        the pdf tool resolves against the STATIC registry, DeepSeek was never
        declared there, and every platform-key PDF died on ``Unknown model``.
        The pin is now Gemma — declared in ``OPENROUTER_DECLARED_MODELS`` and
        vision-capable, so text-layer AND scanned PDFs both work. Schema-shape
        verified against openclaw@2026.5.28
        ``dist/zod-schema.agent-runtime-*.js`` (``pdfModel: AgentToolModel``,
        union of string | {primary, fallbacks, timeoutMs}) and
        ``dist/zod-schema-*.js`` (``pdfMaxBytesMb: number().positive()``).
        """
        pdf_model = _get(self.config, "agents.defaults.pdfModel")
        self.assertIsNotNone(pdf_model, "agents.defaults.pdfModel missing — pdf tool won't register")
        self.assertEqual(pdf_model.get("primary"), GEMMA_MODEL)
        # Empty on purpose — a text-only fallback would have to be declared in
        # the static registry too, which would override the DeepSeek chat
        # models' live capability metadata. See _build_pdf_model_config.
        self.assertEqual(pdf_model.get("fallbacks"), [])
        # AgentToolModel shape: primary must be a non-empty string (this is what
        # flips OpenClaw's ``hasToolModelConfig`` → the tool becomes available).
        self.assertIsInstance(pdf_model.get("primary"), str)
        self.assertTrue(pdf_model["primary"].strip())

        max_mb = _get(self.config, "agents.defaults.pdfMaxBytesMb")
        self.assertEqual(max_mb, 10)

    def test_pdf_pin_is_declared_in_static_model_registry(self):
        """The pin is only useful if the pdf tool can RESOLVE it.

        ``resolveModelFromRegistry`` reads ``models.json``, which OpenClaw builds
        from the root ``models.providers`` block plus its own bundled catalog —
        dynamically-resolved OpenRouter models never land there. So whatever
        ``pdfModel.primary`` points at must appear in this block, addressed by
        its BARE provider-local slug (the ref is split at the first ``/`` into
        provider + model, matching OpenClaw's own ``moonshotai/kimi-k2.6``
        entries). And it must advertise ``image``, or scanned PDFs fall back to
        text-only extraction and error out with no text layer.
        """
        declared = _get(self.config, "models.providers.openrouter.models")
        self.assertIsInstance(declared, list)
        by_id = {m["id"]: m for m in declared}

        pdf_primary = _get(self.config, "agents.defaults.pdfModel.primary")
        bare_slug = pdf_primary.removeprefix("openrouter/")
        self.assertIn(
            bare_slug,
            by_id,
            f"pdfModel.primary {pdf_primary!r} is not declared in models.providers.openrouter "
            "— the pdf tool will throw 'Unknown model' for every PDF",
        )

        gemma = by_id[bare_slug]
        self.assertIn("image", gemma["input"], "pdf model must advertise image input for scanned PDFs")
        self.assertIn("text", gemma["input"])
        # ModelDefinitionSchema is .strict() — an unknown key rejects the whole
        # config at container load.
        self.assertLessEqual(
            set(gemma),
            {
                "id",
                "name",
                "api",
                "baseUrl",
                "reasoning",
                "input",
                "cost",
                "contextWindow",
                "contextTokens",
                "maxTokens",
                "params",
                "agentRuntime",
                "headers",
                "compat",
                "mediaInput",
                "metadataSource",
            },
        )
        # id + name are the only REQUIRED fields, both non-empty strings.
        self.assertTrue(gemma["id"].strip())
        self.assertTrue(gemma["name"].strip())

    def test_declared_gemma_cost_matches_the_billing_rate(self):
        """The declared ``cost`` block and ``GEMMA_RATE`` must not drift apart.

        The declaration is what the OpenClaw runtime reports a turn cost; the
        billing constant is what ``record_usage`` actually charges the tenant.
        Nothing reconciles the two, so a one-sided edit quotes one price and
        bills another — invisibly, and only on the model that handles PDFs.
        """
        from apps.billing.constants import GEMMA_RATE

        declared = _get(self.config, "models.providers.openrouter.models")
        by_id = {m["id"]: m for m in declared}
        gemma = by_id[GEMMA_MODEL.removeprefix("openrouter/")]
        self.assertEqual(gemma["cost"]["input"], GEMMA_RATE["input"])
        self.assertEqual(gemma["cost"]["output"], GEMMA_RATE["output"])

    def test_deepseek_chat_models_are_not_statically_declared(self):
        """Guard the deliberate scope of ``OPENROUTER_DECLARED_MODELS``.

        For openrouter (no ``preferRuntimeResolvedModel`` hook in the plugin),
        OpenClaw's ``resolveModelWithRegistry`` returns a static registry hit and
        never calls ``resolveDynamicModel``. Declaring the DeepSeek chat models
        here would freeze their ``reasoning``/``maxTokens`` metadata at whatever
        we hardcode — a silent chat regression on the tenant-selectable
        reasoning model. If a future change adds them, it must be deliberate.
        """
        declared_ids = {m["id"] for m in _get(self.config, "models.providers.openrouter.models")}
        for model_id in (DEEPSEEK_MODEL, DEEPSEEK_FLASH_MODEL):
            self.assertNotIn(model_id.removeprefix("openrouter/"), declared_ids)

    def test_generated_config_passes_write_validation_gate(self):
        """The exact write-time gate every config-to-share write funnels through
        (``azure_client.upload_config_to_file_share`` → ``assert_config_writable``)
        must accept the config the generator now emits. This is the guard against
        generator/validator drift: if ``pdfModel``/``pdfMaxBytesMb`` (or any
        future key) are emitted but missing from the validator's
        ``_AGENTS_DEFAULTS_ALLOWED_KEYS``, the gate would REJECT every fleet
        config write and silently no-op the bump.
        """
        # Raises InvalidTenantConfigError on any Django-owned schema violation.
        assert_config_writable(self.config)

    # ── Sanity: known top-level keys ──────────────────────────────────

    def test_no_unexpected_top_level_keys(self):
        """Guardrail against typos at the top level.

        OpenClaw silently ignores unknown top-level keys, so a typo like
        ``commitmnets`` would compile clean but do nothing. Pin the
        allowlist; if a new key lands in config_generator, this assertion
        forces an explicit decision to add it.
        """
        allowed = {
            "agents",
            "auth",
            "channels",
            "commitments",
            "cron",
            "env",
            "gateway",
            "logging",
            "messages",
            "models",
            "plugins",
            "session",  # session.reset.{mode,idleMinutes} — verified in openclaw@2026.5.7 runtime-schema; added 2026-05-14 (CONTINUITY_workspace-routing-fix.md, Phase 5)
            "telemetry",
            "tools",
            "workspace",
        }
        unexpected = set(self.config.keys()) - allowed
        self.assertFalse(
            unexpected,
            f"Unexpected top-level key(s) in openclaw.json: {sorted(unexpected)}. "
            "Either add them to the allowlist (after npm-pack-verifying the schema) "
            "or fix the typo.",
        )


class ByoPdfModelPinTest(TestCase):
    """A BYO-Anthropic primary must keep the native Anthropic PDF path.

    Anthropic is a native-PDF provider in OpenClaw
    (``providerSupportsNativePdf``): the file goes to Claude whole, scanned
    pages included, billed to the tenant's own subscription. Re-pointing these
    tenants at the platform Gemma pin would move their document reads onto the
    platform OpenRouter key AND drop them from the native path onto local
    extraction — strictly worse on both cost and capability.
    """

    def setUp(self):
        self.tenant = create_tenant(
            display_name="ByoPdfPin",
            telegram_chat_id=998878,
        )
        self.tenant.byo_models_enabled = True
        self.tenant.preferred_model = ANTHROPIC_SONNET_MODEL
        self.tenant.save(update_fields=["byo_models_enabled", "preferred_model"])
        BYOCredential.objects.create(
            tenant=self.tenant,
            provider=BYOCredential.Provider.ANTHROPIC,
            mode=BYOCredential.Mode.CLI_SUBSCRIPTION,
            key_vault_secret_name="byo-anthropic-test",
            status=BYOCredential.Status.VERIFIED,
        )
        self.config = generate_openclaw_config(self.tenant)

    def test_byo_primary_keeps_its_own_model_for_pdf(self):
        chat_primary = _get(self.config, "agents.defaults.model.primary")
        pdf_model = _get(self.config, "agents.defaults.pdfModel")
        self.assertEqual(chat_primary, ANTHROPIC_SONNET_MODEL, "fixture did not produce a BYO primary")
        self.assertEqual(pdf_model.get("primary"), ANTHROPIC_SONNET_MODEL)
        self.assertNotEqual(pdf_model.get("primary"), GEMMA_MODEL)
        # resolve_tenant_models empties the fallback chain for a BYO primary so a
        # billing failure surfaces instead of silently dropping to a metered
        # model — the PDF pin inherits that.
        self.assertEqual(pdf_model.get("fallbacks"), [])

    def test_byo_tenant_still_gets_the_openrouter_declaration(self):
        """Harmless for BYO tenants, and emitted for them anyway.

        It only extends the ``openrouter`` provider's model list; a BYO primary
        is ``anthropic/*``, resolved through a different provider entirely, so
        there is nothing here that can shadow their chat models.
        """
        declared = _get(self.config, "models.providers.openrouter.models")
        self.assertTrue(any(m["id"] == GEMMA_MODEL.removeprefix("openrouter/") for m in declared))

    def test_byo_config_passes_write_validation_gate(self):
        assert_config_writable(self.config)
