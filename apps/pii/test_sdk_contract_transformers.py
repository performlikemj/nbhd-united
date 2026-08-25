"""Real-SDK guards for the 2026-08-24 azure-mgmt-storage incident.

If you add a new call into this SDK, extend this file.
"""

import inspect

import torch
from django.test import SimpleTestCase
from transformers import AutoModelForTokenClassification, AutoTokenizer, pipeline


class TransformersSdkContractTest(SimpleTestCase):
    def test_pretrained_loaders_accept_our_local_and_pinned_remote_kwargs(self):
        inspect.signature(AutoTokenizer.from_pretrained).bind("local-model-path")
        inspect.signature(AutoTokenizer.from_pretrained).bind(
            "LiquidAI/model",
            trust_remote_code=True,
            revision="pinned-revision",
        )
        inspect.signature(AutoModelForTokenClassification.from_pretrained).bind("local-model-path")
        inspect.signature(AutoModelForTokenClassification.from_pretrained).bind(
            "LiquidAI/model",
            trust_remote_code=True,
            revision="pinned-revision",
        )

    def test_pipeline_and_torch_cpu_fp32_eval_shapes(self):
        inspect.signature(pipeline).bind(
            "token-classification",
            model=object(),
            tokenizer=object(),
            aggregation_strategy="simple",
            device="cpu",
        )
        model = torch.nn.Linear(2, 2).to(device="cpu", dtype=torch.float32).eval()

        self.assertEqual(model.weight.device.type, "cpu")
        self.assertEqual(model.weight.dtype, torch.float32)
        self.assertFalse(model.training)
