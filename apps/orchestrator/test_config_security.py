"""Tests for config security audit."""

from django.test import TestCase

from apps.platform_logs.models import PlatformIssueLog
from apps.tenants.services import create_tenant

from .config_generator import generate_openclaw_config
from .config_security import audit_config_security


class ConfigSecurityAuditTest(TestCase):
    def setUp(self):
        self.tenant = create_tenant(
            display_name="Security Test",
            telegram_chat_id=888777666,
        )

    def test_generated_config_passes_audit(self):
        """A normally generated config should have zero errors."""
        config = generate_openclaw_config(self.tenant)
        findings = audit_config_security(config)
        errors = [f for f in findings if f.severity == "error"]
        self.assertEqual(errors, [], f"Generated config has security errors: {errors}")

    # ── Gateway bind ──

    def test_gateway_bind_0000_flagged(self):
        config = generate_openclaw_config(self.tenant)
        config["gateway"]["bind"] = "0.0.0.0"
        findings = audit_config_security(config)
        self.assertTrue(any(f.check == "gateway_bind" for f in findings))

    def test_gateway_bind_loopback_passes(self):
        config = generate_openclaw_config(self.tenant)
        config["gateway"]["bind"] = "loopback"
        findings = audit_config_security(config)
        self.assertFalse(any(f.check == "gateway_bind" for f in findings))

    # ── Auth token ──

    def test_literal_token_flagged(self):
        config = generate_openclaw_config(self.tenant)
        config["gateway"]["auth"]["token"] = "my-secret-token-12345"
        findings = audit_config_security(config)
        self.assertTrue(any(f.check == "gateway_token_literal" for f in findings))

    def test_env_ref_token_passes(self):
        config = generate_openclaw_config(self.tenant)
        config["gateway"]["auth"]["token"] = "${NBHD_INTERNAL_API_KEY}"
        findings = audit_config_security(config)
        self.assertFalse(any(f.check == "gateway_token_literal" for f in findings))

    # ── Elevated execution ──

    def test_elevated_enabled_flagged(self):
        config = generate_openclaw_config(self.tenant)
        config["tools"]["elevated"]["enabled"] = True
        findings = audit_config_security(config)
        self.assertTrue(any(f.check == "elevated_enabled" for f in findings))

    # ── Gateway deny ──

    def test_gateway_not_denied_flagged(self):
        config = generate_openclaw_config(self.tenant)
        config["tools"]["deny"] = [t for t in config["tools"]["deny"] if t != "gateway"]
        findings = audit_config_security(config)
        self.assertTrue(any(f.check == "gateway_not_denied" for f in findings))

    # ── Env secret leak ──

    def test_env_secret_pattern_flagged(self):
        config = generate_openclaw_config(self.tenant)
        config.setdefault("env", {})["LEAKED_KEY"] = "sk-ant-api03-leaked-key-here"
        findings = audit_config_security(config)
        self.assertTrue(any(f.check == "env_secret_leak" for f in findings))

    def test_env_vault_ref_passes(self):
        config = generate_openclaw_config(self.tenant)
        config.setdefault("env", {})["SAFE_KEY"] = "${MY_VAULT_SECRET}"
        findings = audit_config_security(config)
        self.assertFalse(any(f.check == "env_secret_leak" for f in findings))


class AuditAndLogIntegrationTest(TestCase):
    """Test that _audit_and_log writes to PlatformIssueLog."""

    def setUp(self):
        self.tenant = create_tenant(
            display_name="Audit Log Test",
            telegram_chat_id=777666555,
        )

    def test_clean_config_creates_no_logs(self):
        from .services import _audit_and_log

        config = generate_openclaw_config(self.tenant)
        _audit_and_log(self.tenant, config, stage="test")
        self.assertEqual(PlatformIssueLog.objects.filter(tenant=self.tenant).count(), 0)

    def test_error_finding_raises_and_logs(self):
        from .services import _audit_and_log

        config = generate_openclaw_config(self.tenant)
        config["gateway"]["bind"] = "0.0.0.0"

        with self.assertRaises(ValueError):
            _audit_and_log(self.tenant, config, stage="test_provision")

        logs = PlatformIssueLog.objects.filter(tenant=self.tenant)
        self.assertEqual(logs.count(), 1)
        log = logs.first()
        self.assertEqual(log.category, PlatformIssueLog.Category.CONFIG_ISSUE)
        self.assertEqual(log.severity, PlatformIssueLog.Severity.HIGH)
        self.assertIn("gateway_bind", log.summary)
        self.assertEqual(log.tool_name, "config_security_audit")
