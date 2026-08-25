"""Real-SDK guards for the 2026-08-24 azure-mgmt-storage incident.

If you add a new call into this SDK, extend this file.
"""

import inspect

import httpx
from django.test import SimpleTestCase
from qstash import QStash, Receiver


class QStashSdkContractTest(SimpleTestCase):
    def test_client_constructor_and_private_transport_shape(self):
        client = QStash(token="offline-token", retry=False)

        self.assertTrue(hasattr(client, "message"))
        self.assertIsInstance(client.http._client, httpx.Client)
        client.http._client.close()

    def test_publish_and_batch_signatures_accept_our_payloads(self):
        client = QStash(token="offline-token", retry=False)

        inspect.signature(client.message.publish_json).bind(
            url="https://example.test/task",
            body={"args": [], "kwargs": {}},
            retries=3,
            deduplication_id="task-123",
            delay="5s",
        )
        inspect.signature(client.message.batch_json).bind(
            [{"url": "https://example.test/task", "body": {"args": [], "kwargs": {}}, "retries": 3}]
        )
        client.http._client.close()

    def test_receiver_constructor_and_verify_signature_accept_our_calls(self):
        receiver = Receiver(current_signing_key="current", next_signing_key="next")

        inspect.signature(receiver.verify).bind(signature="signature", body="{}", url="https://example.test/task")
