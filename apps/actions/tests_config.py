"""Tests for tier-based GWS skill loading and gate tool config."""

from django.test import TestCase

from apps.tenants.models import Tenant


def _make_user(**kwargs):
    from django.contrib.auth import get_user_model

    User = get_user_model()
    defaults = {
        "username": f"cfgtest_{Tenant.objects.count()}",
        "email": f"cfgtest_{Tenant.objects.count()}@test.com",
        "password": "testpass",
    }
    defaults.update(kwargs)
    return User.objects.create_user(**defaults)


def _make_tenant(user, **kwargs):
    defaults = {
        "user": user,
        "status": Tenant.Status.ACTIVE,
        "container_fqdn": "test.example.com",
        "container_id": f"oc-cfg-{user.username[:10]}",
        "model_tier": "starter",
    }
    defaults.update(kwargs)
    return Tenant.objects.create(**defaults)


def _make_google_integration(tenant):
    from apps.integrations.models import Integration

    return Integration.objects.create(
        tenant=tenant,
        provider="google",
        status=Integration.Status.ACTIVE,
    )


class StarterTierGWSConfigTest(TestCase):
    """Starter tier should only get read-only GWS skills."""

    def test_starter_gets_read_only_gws_skills(self):
        from apps.orchestrator.config_generator import generate_openclaw_config

        user = _make_user(username="starter1")
        tenant = _make_tenant(user, model_tier="starter", container_id="oc-starter1")
        _make_google_integration(tenant)

        config = generate_openclaw_config(tenant)
        extra_dirs = config.get("skills", {}).get("load", {}).get("extraDirs", [])

        # Should have read-only skills
        self.assertIn("/opt/nbhd/skills/gws-shared", extra_dirs)
        self.assertIn("/opt/nbhd/skills/gws-gmail-triage", extra_dirs)
        self.assertIn("/opt/nbhd/skills/gws-calendar-agenda", extra_dirs)

        # Should NOT have destructive skills
        self.assertNotIn("/opt/nbhd/skills/gws-gmail", extra_dirs)
        self.assertNotIn("/opt/nbhd/skills/gws-gmail-send", extra_dirs)
        self.assertNotIn("/opt/nbhd/skills/gws-calendar", extra_dirs)
        self.assertNotIn("/opt/nbhd/skills/gws-drive", extra_dirs)
        self.assertNotIn("/opt/nbhd/skills/gws-tasks", extra_dirs)

    def test_starter_still_gets_gate_skill(self):
        from apps.orchestrator.config_generator import generate_openclaw_config

        user = _make_user(username="starter2")
        tenant = _make_tenant(user, model_tier="starter", container_id="oc-starter2")
        _make_google_integration(tenant)

        config = generate_openclaw_config(tenant)
        extra_dirs = config.get("skills", {}).get("load", {}).get("extraDirs", [])

        self.assertIn("/opt/nbhd/skills/nbhd-action-gate", extra_dirs)


class GateEnvVarsConfigTest(TestCase):
    """Gate tool needs NBHD_TENANT_ID and NBHD_API_BASE_URL."""

    def test_gate_env_vars_set(self):
        from apps.orchestrator.config_generator import generate_openclaw_config

        user = _make_user(username="envtest1")
        tenant = _make_tenant(user, container_id="oc-envtest1")
        _make_google_integration(tenant)

        config = generate_openclaw_config(tenant)
        env = config.get("env", {})

        self.assertEqual(env.get("NBHD_TENANT_ID"), str(tenant.id))
        self.assertIn("NBHD_API_BASE_URL", env)

    def test_no_gate_env_without_google(self):
        from apps.orchestrator.config_generator import generate_openclaw_config

        user = _make_user(username="envtest2")
        tenant = _make_tenant(user, container_id="oc-envtest2")
        # No Google integration

        config = generate_openclaw_config(tenant)
        env = config.get("env", {})

        self.assertNotIn("NBHD_TENANT_ID", env)
