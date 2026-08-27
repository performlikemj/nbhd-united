"""Protocol and failure-mode tests for the shared PII detector."""

from __future__ import annotations

import json
import os
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from django.conf import settings
from django.test import SimpleTestCase

from apps.pii import engine
from apps.pii.config import resolve_detector_transport
from apps.pii.redactor import redact_text
from apps.pii.shared_client import (
    MAX_RESPONSE_BYTES,
    SharedPiiError,
    SharedPiiPipeline,
    ping_shared_detector,
)
from apps.pii.shared_server import SharedDetectorServer


def _frame(payload) -> bytes:
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
    return struct.pack("!I", len(body)) + body


def _read_frame(connection: socket.socket):
    header = connection.recv(4)
    if len(header) != 4:
        return None
    (size,) = struct.unpack("!I", header)
    body = b""
    while len(body) < size:
        chunk = connection.recv(size - len(body))
        if not chunk:
            break
        body += chunk
    return json.loads(body.decode())


@contextmanager
def _scripted_server(path: str, response, *, delay: float = 0.0, raw: bool = False):
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(path)
    listener.listen(1)
    seen = []

    def serve():
        connection, _ = listener.accept()
        with connection:
            seen.append(_read_frame(connection))
            if delay:
                time.sleep(delay)
            data = response if raw else _frame(response)
            if data:
                try:
                    connection.sendall(data)
                except OSError:
                    pass
        listener.close()

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    try:
        yield seen
    finally:
        thread.join(timeout=2)
        if thread.is_alive():
            listener.close()
        Path(path).unlink(missing_ok=True)


@contextmanager
def _running_server(path: str, pipeline, *, queue_max: int = 64):
    server = SharedDetectorServer(
        socket_path=path,
        engine="deberta",
        queue_max=queue_max,
        pipeline_loader=lambda _engine: pipeline,
        configure_runtime=False,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    if not server.ready.wait(1):
        raise AssertionError("test server did not become ready")
    try:
        yield server
    finally:
        server.shutdown()
        thread.join(timeout=2)


def _raw_request(path: str, payload: dict, *, timeout: float = 2.0):
    connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    connection.settimeout(timeout)
    connection.connect(path)
    connection.sendall(_frame(payload))
    try:
        return _read_frame(connection)
    finally:
        connection.close()


class SharedTransportSelectionTests(SimpleTestCase):
    def test_transport_defaults_to_local(self):
        self.assertEqual(settings.PII_DETECTOR_TRANSPORT, "local")
        self.assertEqual(resolve_detector_transport(None), "local")
        self.assertEqual(resolve_detector_transport("unsupported"), "local")

    def test_shared_transport_returns_client_before_local_engine_import(self):
        expected = object()
        with (
            patch.dict(os.environ, {"PII_DETECTOR_TRANSPORT": "shared"}),
            patch("apps.pii.shared_client.get_shared_pii_pipeline", return_value=expected) as shared,
            patch("apps.pii.engine.get_deberta_pii_pipeline") as deberta,
        ):
            self.assertIs(engine.get_pii_pipeline(), expected)
        shared.assert_called_once_with()
        deberta.assert_not_called()


class SharedPiiClientTests(SimpleTestCase):
    def test_shape_only_client_telemetry_never_logs_text(self):
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "pii.sock")
            response = {
                "v": 1,
                "engine": "deberta",
                "spans": [{"entity_group": "EMAIL", "score": 0.9, "start": 0, "end": 3}],
            }
            with (
                _scripted_server(path, response),
                self.assertLogs("apps.pii.shared_client", level="INFO") as captured,
            ):
                SharedPiiPipeline(socket_path=path)("secret@example.com")

        event = captured.output[-1]
        self.assertIn("pii_detector_client engine=deberta transport=shared outcome=ok", event)
        self.assertIn("len_bucket=0-255 span_count=1", event)
        self.assertNotIn("secret@example.com", event)

    def test_success_fully_materializes_spans(self):
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "pii.sock")
            response = {
                "v": 1,
                "engine": "deberta",
                "spans": [{"entity_group": "EMAIL", "score": 0.9, "start": 0, "end": 3}],
            }
            with _scripted_server(path, response) as seen:
                result = SharedPiiPipeline(socket_path=path)("abc")
            self.assertEqual(result, response["spans"])
            self.assertEqual(seen[0]["v"], 1)
            self.assertEqual(seen[0]["engine"], "deberta")
            self.assertEqual(seen[0]["text"], "abc")
            self.assertGreater(seen[0]["ttl_ms"], 0)

    def test_refused_connection(self):
        with tempfile.TemporaryDirectory() as directory:
            client = SharedPiiPipeline(socket_path=str(Path(directory) / "missing.sock"))
            with self.assertRaisesRegex(SharedPiiError, "connection failed") as raised:
                client("abc")
        self.assertEqual(raised.exception.outcome, "connect")

    def test_not_ready_and_server_error_paths(self):
        for code in ("not_ready", "queue_full", "expired"):
            with self.subTest(code=code), tempfile.TemporaryDirectory() as directory:
                path = str(Path(directory) / "pii.sock")
                with (
                    _scripted_server(path, {"v": 1, "error": code}),
                    self.assertRaises(SharedPiiError) as raised,
                ):
                    SharedPiiPipeline(socket_path=path)("abc")
                self.assertEqual(raised.exception.outcome, code)

    def test_one_deadline_covers_response_framing(self):
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "pii.sock")
            with (
                _scripted_server(path, b"", delay=0.1, raw=True),
                self.assertRaises(SharedPiiError) as raised,
            ):
                SharedPiiPipeline(socket_path=path, deadline_s=0.02)("abc")
            self.assertEqual(raised.exception.outcome, "timeout")

    def test_oversized_text_does_not_open_circuit(self):
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "pii.sock")
            client = SharedPiiPipeline(socket_path=path)
            for _index in range(3):
                with self.assertRaises(SharedPiiError) as raised:
                    client("x" * (2 * 1024 * 1024))
                self.assertEqual(raised.exception.outcome, "too_large")

            response = {"v": 1, "engine": "deberta", "spans": []}
            with _scripted_server(path, response):
                self.assertEqual(client("normal"), [])

    def test_circuit_opens_then_half_open_success_closes_it(self):
        clock = [100.0]
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "pii.sock")
            client = SharedPiiPipeline(socket_path=path, clock=lambda: clock[0])
            for _ in range(3):
                with self.assertRaises(SharedPiiError):
                    client("abc")
            with self.assertRaises(SharedPiiError) as opened:
                client("abc")
            self.assertEqual(opened.exception.outcome, "circuit_open")

            clock[0] += 31
            response = {"v": 1, "engine": "deberta", "spans": []}
            with _scripted_server(path, response):
                self.assertEqual(client("abc"), [])
            with _scripted_server(path, response):
                self.assertEqual(client("abc"), [])

    def test_truncated_oversized_and_malformed_frames(self):
        cases = {
            "truncated-header": b"\x00\x00",
            "truncated-body": struct.pack("!I", 10) + b"{}",
            "oversized": struct.pack("!I", MAX_RESPONSE_BYTES + 1),
            "malformed-json": struct.pack("!I", 1) + b"{",
        }
        for name, response in cases.items():
            with self.subTest(name=name), tempfile.TemporaryDirectory() as directory:
                path = str(Path(directory) / "pii.sock")
                with _scripted_server(path, response, raw=True), self.assertRaises(SharedPiiError):
                    SharedPiiPipeline(socket_path=path)("abc")

    def test_every_invalid_span_shape_is_rejected(self):
        valid = {"entity_group": "NAME", "score": 0.5, "start": 0, "end": 2}
        invalid = [
            None,
            {**valid, "extra": 1},
            {key: value for key, value in valid.items() if key != "end"},
            {**valid, "entity_group": ""},
            {**valid, "entity_group": 1},
            {**valid, "score": True},
            {**valid, "score": "0.5"},
            {**valid, "score": float("nan")},
            {**valid, "score": float("inf")},
            {**valid, "score": -0.1},
            {**valid, "score": 1.1},
            {**valid, "start": True},
            {**valid, "start": "0"},
            {**valid, "start": -1},
            {**valid, "end": True},
            {**valid, "end": "2"},
            {**valid, "end": 4},
            {**valid, "start": 2, "end": 2},
            {**valid, "start": 2, "end": 1},
        ]
        for index, span in enumerate(invalid):
            with self.subTest(index=index), tempfile.TemporaryDirectory() as directory:
                path = str(Path(directory) / "pii.sock")
                response = {"v": 1, "engine": "deberta", "spans": [span]}
                with _scripted_server(path, response), self.assertRaises(SharedPiiError) as raised:
                    SharedPiiPipeline(socket_path=path)("abc")
                self.assertEqual(raised.exception.outcome, "bad_response")

    def test_span_count_cap_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "pii.sock")
            span = {"entity_group": "NAME", "score": 0.5, "start": 0, "end": 1}
            response = {"v": 1, "engine": "deberta", "spans": [span] * 4097}
            with _scripted_server(path, response), self.assertRaises(SharedPiiError) as raised:
                SharedPiiPipeline(socket_path=path)("a")
        self.assertEqual(raised.exception.outcome, "bad_response")

    def test_protocol_and_engine_mismatch(self):
        cases = [
            ({"v": 2, "engine": "deberta", "spans": []}, "protocol"),
            ({"v": 1, "engine": "liquid", "spans": []}, "engine_mismatch"),
        ]
        for response, outcome in cases:
            with self.subTest(outcome=outcome), tempfile.TemporaryDirectory() as directory:
                path = str(Path(directory) / "pii.sock")
                with _scripted_server(path, response), self.assertRaises(SharedPiiError) as raised:
                    SharedPiiPipeline(socket_path=path)("abc")
                self.assertEqual(raised.exception.outcome, outcome)

    def test_worst_case_unicode_round_trips(self):
        for length in (8000, 10000, 20000):
            with self.subTest(length=length), tempfile.TemporaryDirectory() as directory:
                path = str(Path(directory) / "pii.sock")
                text = "\U0010ffff" * length
                response = {
                    "v": 1,
                    "engine": "deberta",
                    "spans": [{"entity_group": "NAME", "score": 1.0, "start": length - 1, "end": length}],
                }
                with _scripted_server(path, response) as seen:
                    self.assertEqual(SharedPiiPipeline(socket_path=path)(text), response["spans"])
                self.assertEqual(seen[0]["text"], text)


class SharedPiiServerTests(SimpleTestCase):
    def test_stale_socket_is_replaced(self):
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "pii.sock")
            stale = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            stale.bind(path)
            stale.close()

            with (
                _running_server(path, lambda _text: []),
                patch.dict(os.environ, {"PII_SHARED_SOCKET": path, "PII_DETECTOR_ENGINE": "deberta"}),
            ):
                self.assertTrue(ping_shared_detector())

    def test_live_socket_owner_is_not_unlinked(self):
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "pii.sock")
            with _running_server(path, lambda _text: []):
                env = os.environ.copy()
                env.update(
                    {
                        "PII_SHARED_SOCKET": path,
                        "PII_DETECTOR_ENGINE": "deberta",
                        "PYTHONPATH": str(Path(__file__).resolve().parents[2]),
                    }
                )
                second = subprocess.run(
                    [sys.executable, "-m", "apps.pii.testsupport.fake_shared_detector"],
                    env=env,
                    capture_output=True,
                    text=True,
                    timeout=5,
                    check=False,
                )
                self.assertNotEqual(second.returncode, 0)
                with patch.dict(os.environ, {"PII_SHARED_SOCKET": path, "PII_DETECTOR_ENGINE": "deberta"}):
                    self.assertTrue(ping_shared_detector())

    def test_shape_only_server_telemetry_never_logs_text(self):
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "pii.sock")
            with (
                _running_server(path, lambda _text: []),
                self.assertLogs("apps.pii.shared_server", level="INFO") as captured,
            ):
                self.assertEqual(SharedPiiPipeline(socket_path=path)("secret@example.com"), [])

        event = next(line for line in captured.output if "pii_detector_server engine=" in line)
        for field in (
            "outcome=ok",
            "queue_ms=",
            "inference_ms=",
            "total_ms=",
            "len_bucket=0-255",
            "span_count=0",
            "queue_depth=",
        ):
            self.assertIn(field, event)
        self.assertNotIn("secret@example.com", event)

    def test_ping_and_success(self):
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "pii.sock")

            def pipeline(text):
                return [{"entity_group": "NAME", "score": 0.8, "start": 0, "end": len(text)}]

            with _running_server(path, pipeline):
                with patch.dict(os.environ, {"PII_SHARED_SOCKET": path, "PII_DETECTOR_ENGINE": "deberta"}):
                    self.assertTrue(ping_shared_detector())
                self.assertEqual(SharedPiiPipeline(socket_path=path)("abc")[0]["end"], 3)

    def test_queue_full_and_expired(self):
        entered = threading.Event()
        release = threading.Event()

        def pipeline(_text):
            entered.set()
            release.wait(1)
            return []

        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "pii.sock")
            with _running_server(path, pipeline, queue_max=1):
                results = {}

                def request(name, ttl_ms):
                    results[name] = _raw_request(
                        path,
                        {"v": 1, "engine": "deberta", "text": name, "ttl_ms": ttl_ms},
                    )

                active = threading.Thread(target=request, args=("active", 1000), daemon=True)
                active.start()
                self.assertTrue(entered.wait(1))
                queued = threading.Thread(target=request, args=("queued", 30), daemon=True)
                queued.start()
                time.sleep(0.01)
                full = _raw_request(path, {"v": 1, "engine": "deberta", "text": "full", "ttl_ms": 1000})
                self.assertEqual(full, {"v": 1, "error": "queue_full"})
                queued.join(1)
                self.assertEqual(results["queued"], {"v": 1, "error": "expired"})
                release.set()
                active.join(1)

    def test_shared_timeout_still_runs_real_presidio_fallback(self):
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "pii.sock")
            with _scripted_server(path, b"", delay=0.1, raw=True):
                client = SharedPiiPipeline(socket_path=path, deadline_s=0.02)
                with patch("apps.pii.engine.get_pii_pipeline", return_value=client):
                    result = redact_text("email me at person@example.com", tier="starter")
        self.assertEqual(result, "email me at [EMAIL_ADDRESS_1]")

    def test_shared_client_process_never_imports_local_model_modules(self):
        script = """
import importlib.abc
import os
import sys

blocked = {"torch", "transformers", "apps.pii.liquid_engine"}
class Blocker(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname in blocked or any(fullname.startswith(name + ".") for name in blocked):
            raise AssertionError("blocked local model import: " + fullname)
        return None
sys.meta_path.insert(0, Blocker())
os.environ["PII_DETECTOR_TRANSPORT"] = "shared"
from apps.pii.engine import get_pii_pipeline
pipeline = get_pii_pipeline()
assert pipeline.__class__.__name__ == "SharedPiiPipeline"
assert not blocked.intersection(sys.modules)
print("NO_LOCAL_MODEL_IMPORTS")
"""
        env = os.environ.copy()
        env["PYTHONPATH"] = str(Path(__file__).resolve().parents[2])
        completed = subprocess.run(
            [sys.executable, "-c", script],
            check=False,
            capture_output=True,
            text=True,
            env=env,
            timeout=10,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(completed.stdout.strip(), "NO_LOCAL_MODEL_IMPORTS")
