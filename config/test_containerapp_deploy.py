from copy import deepcopy
from pathlib import Path

from django.test import Client, SimpleTestCase, override_settings

from apps.pii.config import DEFAULT_DETECTOR_ENGINE, DEFAULT_DETECTOR_TRANSPORT
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
                                {"name": "PII_DETECTOR_ENGINE", "value": "liquid"},
                                {"name": "PII_DETECTOR_TRANSPORT", "value": "shared"},
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
                "PII_DETECTOR_ENGINE": DEFAULT_DETECTOR_ENGINE,
                "PII_DETECTOR_TRANSPORT": DEFAULT_DETECTOR_TRANSPORT,
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
        self.assertEqual(
            next(item for item in container["env"] if item["name"] == "PII_DETECTOR_ENGINE")["value"],
            "deberta",
        )
        self.assertEqual(
            next(item for item in container["env"] if item["name"] == "PII_DETECTOR_TRANSPORT")["value"],
            "local",
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

    def test_unsupported_pii_detector_engine_fails_closed(self):
        with self.assertRaisesMessage(
            ValueError,
            "unsupported PII_DETECTOR_ENGINE 'experimental'; expected one of: deberta, liquid",
        ):
            prepare_deployment(
                self.definition,
                container_name="django",
                image="registry/new:sha",
                environment={"PII_DETECTOR_ENGINE": "experimental"},
            )

    def test_unsupported_pii_detector_transport_fails_closed(self):
        with self.assertRaisesMessage(
            ValueError,
            "unsupported PII_DETECTOR_TRANSPORT 'remote'; expected one of: local, shared",
        ):
            prepare_deployment(
                self.definition,
                container_name="django",
                image="registry/new:sha",
                environment={"PII_DETECTOR_TRANSPORT": "remote"},
            )

    def test_ci_deploy_applies_the_prepared_readiness_spec(self):
        workflow = (Path(__file__).parents[1] / ".github/workflows/ci-cd.yml").read_text()

        self.assertIn("python3 -m config.containerapp_deploy", workflow)
        self.assertIn('--yaml "$DEPLOY_SPEC"', workflow)
        self.assertIn("PII_DETECTOR_ENGINE: deberta", workflow)
        self.assertIn("PII_DETECTOR_TRANSPORT: shared", workflow)
        self.assertIn("PII_MODEL_TAG=deberta-only-a038061af92047b0", workflow)
        self.assertIn("--build-arg INCLUDE_LIQUID=false", workflow)
        self.assertIn("shelved Liquid bundle present in serving image", workflow)
        self.assertNotIn("PII_MODEL_TAG=pii-models-v3", workflow)
        self.assertNotIn("PII_MODEL_TAG=deberta-finetuned-pii-v2", workflow)
        self.assertIn("--pii-detector-engine ${{ env.PII_DETECTOR_ENGINE }}", workflow)
        self.assertIn("--pii-detector-transport ${{ env.PII_DETECTOR_TRANSPORT }}", workflow)

        dockerfile = (Path(__file__).parents[1] / "Dockerfile").read_text()
        self.assertIn(
            "COPY --from=nbhdunited.azurecr.io/pii-model:deberta-only-a038061af92047b0",
            dockerfile,
        )
        self.assertNotIn("pii-models-v3", dockerfile)
        self.assertNotIn("deberta-finetuned-pii-v2", dockerfile)

        model_dockerfile = (Path(__file__).parents[1] / "Dockerfile.pii-model").read_text()
        self.assertIn("ARG INCLUDE_LIQUID=false", model_dockerfile)
        self.assertIn("revision='a038061af92047b0afbbd5ca07d7aa0521789379'", model_dockerfile)
