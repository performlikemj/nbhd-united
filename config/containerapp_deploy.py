"""Prepare the existing Django Container App definition for one safe deploy.

Azure Container Apps' default TCP readiness probe succeeds as soon as Gunicorn's
master binds port 8000, before worker ``post_worker_init`` hooks finish. The CI
deploy exports the live definition (without secret values), passes it through
this module, and applies it once so the new image and HTTP readiness probe land
in the same revision. All unrelated live settings and secret references remain
untouched.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from apps.pii.config import DEFAULT_DETECTOR_ENGINE, SUPPORTED_DETECTOR_ENGINES

_READINESS_PROBE = {
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


def prepare_deployment(
    app: dict,
    *,
    container_name: str,
    image: str,
    environment: dict[str, str],
) -> dict:
    """Mutate an exported Container App definition for the next revision."""
    containers = app["properties"]["template"]["containers"]
    matching_containers = [container for container in containers if container.get("name") == container_name]
    if len(matching_containers) != 1:
        raise ValueError(f"expected exactly one container named {container_name!r}, found {len(matching_containers)}")

    container = matching_containers[0]
    container["image"] = image

    env_entries = container.setdefault("env", [])
    for name, value in environment.items():
        if name == "PII_DETECTOR_ENGINE":
            value = value.strip().lower()
            if value not in SUPPORTED_DETECTOR_ENGINES:
                supported = ", ".join(sorted(SUPPORTED_DETECTOR_ENGINES))
                raise ValueError(f"unsupported PII_DETECTOR_ENGINE {value!r}; expected one of: {supported}")
        matching_entries = [entry for entry in env_entries if entry.get("name") == name]
        if len(matching_entries) > 1:
            raise ValueError(f"environment variable {name!r} is defined more than once")
        if matching_entries:
            entry = matching_entries[0]
            entry.pop("secretRef", None)
            entry["value"] = value
        else:
            env_entries.append({"name": name, "value": value})

    probes = container.setdefault("probes", [])
    non_readiness = [probe for probe in probes if probe.get("type") != "Readiness"]
    container["probes"] = [*non_readiness, dict(_READINESS_PROBE)]
    return app


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--spec", type=Path, required=True)
    parser.add_argument("--container-name", required=True)
    parser.add_argument("--image", required=True)
    parser.add_argument("--openclaw-image-tag", required=True)
    parser.add_argument("--sentry-release", required=True)
    parser.add_argument("--django-base-url", required=True)
    parser.add_argument(
        "--pii-detector-engine",
        choices=sorted(SUPPORTED_DETECTOR_ENGINES),
        default=DEFAULT_DETECTOR_ENGINE,
    )
    args = parser.parse_args()

    app = json.loads(args.spec.read_text())
    prepare_deployment(
        app,
        container_name=args.container_name,
        image=args.image,
        environment={
            "OPENCLAW_IMAGE_TAG": args.openclaw_image_tag,
            "SENTRY_RELEASE": args.sentry_release,
            "DJANGO_BASE_URL": args.django_base_url,
            "PII_DETECTOR_ENGINE": args.pii_detector_engine,
        },
    )
    args.spec.write_text(json.dumps(app, separators=(",", ":")))


if __name__ == "__main__":
    main()
