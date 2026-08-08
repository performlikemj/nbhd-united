from copy import deepcopy
from pathlib import Path

from django.test import Client, SimpleTestCase, override_settings

from config.containerapp_deploy import prepare_deployment


class PrepareContainerAppDeploymentTests(SimpleTestCase):
    def setUp(self):
        self.definition = {
            "properties": {
                "configuration": {"secrets": [{"name": "database-url"}]},
                "template": {
                    "containers": [
                        {
                            "name": "django",
                            "image": "registry/old:sha",
                            "env": [
                                {"name": "DATABASE_URL", "secretRef": "database-url"},
                                {"name": "SENTRY_RELEASE", "value": "old-sha"},
                            ],
                            "probes": [
                                {"type": "Liveness", "tcpSocket": {"port": 8000}},
                                {"type": "Readiness", "tcpSocket": {"port": 8000}},
                                {"type": "Startup", "tcpSocket": {"port": 8000}},
                            ],
                        }
                    ]
                },
            }
        }

    def test_deploy_preserves_live_settings_and_uses_http_worker_readiness(self):
        original = deepcopy(self.definition)

        result = prepare_deployment(
            self.definition,
            container_name="django",
            image="registry/new:sha",
            environment={
                "OPENCLAW_IMAGE_TAG": "oc-sha",
                "SENTRY_RELEASE": "new-sha",
            },
        )

        container = result["properties"]["template"]["containers"][0]
        self.assertEqual(container["image"], "registry/new:sha")
        self.assertEqual(
            next(item for item in container["env"] if item["name"] == "DATABASE_URL"),
            {"name": "DATABASE_URL", "secretRef": "database-url"},
        )
        self.assertEqual(
            next(item for item in container["env"] if item["name"] == "SENTRY_RELEASE")["value"],
            "new-sha",
        )
        self.assertEqual(
            next(item for item in container["env"] if item["name"] == "OPENCLAW_IMAGE_TAG")["value"],
            "oc-sha",
        )
        self.assertEqual(result["properties"]["configuration"], original["properties"]["configuration"])

        readiness = [probe for probe in container["probes"] if probe["type"] == "Readiness"]
        self.assertEqual(
            readiness,
            [
                {
                    "failureThreshold": 48,
                    "httpGet": {
                        "path": "/health/",
                        "port": 8000,
                        "scheme": "HTTP",
                        "httpHeaders": [
                            {"name": "Host", "value": "localhost"},
                            {"name": "X-Forwarded-Proto", "value": "https"},
                        ],
                    },
                    "periodSeconds": 5,
                    "successThreshold": 1,
                    "timeoutSeconds": 5,
                    "type": "Readiness",
                }
            ],
        )
        self.assertEqual(
            {probe["type"] for probe in container["probes"]},
            {"Liveness", "Readiness", "Startup"},
        )

    @override_settings(
        ALLOWED_HOSTS=["localhost"],
        SECURE_SSL_REDIRECT=True,
        SECURE_PROXY_SSL_HEADER=("HTTP_X_FORWARDED_PROTO", "https"),
    )
    def test_readiness_headers_cross_the_real_production_middleware_stack(self):
        client = Client()

        pod_ip_request = client.get("/health/", headers={"host": "10.0.0.42"})
        fixed_request = client.get(
            "/health/",
            headers={"host": "localhost", "x-forwarded-proto": "https"},
        )

        self.assertEqual(pod_ip_request.status_code, 400)
        self.assertEqual(fixed_request.status_code, 200)
        self.assertEqual(fixed_request.json()["status"], "ok")

    def test_missing_target_container_fails_closed(self):
        with self.assertRaisesMessage(ValueError, "expected exactly one container named 'api', found 0"):
            prepare_deployment(
                self.definition,
                container_name="api",
                image="registry/new:sha",
                environment={},
            )

    def test_ci_deploy_applies_the_prepared_readiness_spec(self):
        workflow = (Path(__file__).parents[1] / ".github/workflows/ci-cd.yml").read_text()

        self.assertIn("python3 -m config.containerapp_deploy", workflow)
        self.assertIn('--yaml "$DEPLOY_SPEC"', workflow)
