"""Real-SDK guards for the 2026-08-24 azure-mgmt-storage incident.

If you add a new call into this SDK, extend this file.
"""

import inspect

from django.test import SimpleTestCase
from huggingface_hub import hf_hub_download


class HuggingFaceHubSdkContractTest(SimpleTestCase):
    def test_download_signature_accepts_the_pinned_artifact_shape(self):
        inspect.signature(hf_hub_download).bind(
            "LiquidAI/model",
            "pii_hybrid_decode.py",
            revision="pinned-revision",
        )
