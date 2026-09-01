"""Subprocess integration and gated real-model measurements for the shared detector."""

from __future__ import annotations

import concurrent.futures
import json
import math
import os
import platform
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from unittest import skipUnless
from unittest.mock import patch

from django.test import SimpleTestCase

from apps.pii.config import DEFAULT_DETECTOR_ENGINE, TIER_POLICIES, resolve_detector_engine
from apps.pii.eval_corpus import CASES
from apps.pii.shared_client import SharedPiiError, SharedPiiPipeline


def _frame(payload: dict) -> bytes:
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
    return struct.pack("!I", len(body)) + body


def _recv_exact(connection: socket.socket, size: int) -> bytes:
    body = b""
    while len(body) < size:
        chunk = connection.recv(size - len(body))
        if not chunk:
            raise AssertionError("truncated integration-test frame")
        body += chunk
    return body


def _raw_request(path: str, payload: dict, *, timeout: float = 10.0) -> dict:
    connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    connection.settimeout(timeout)
    connection.connect(path)
    connection.sendall(_frame(payload))
    try:
        (size,) = struct.unpack("!I", _recv_exact(connection, 4))
        return json.loads(_recv_exact(connection, size).decode())
    finally:
        connection.close()


class _ServerProcess:
    def __init__(
        self,
        directory: str,
        *,
        fake: bool,
        queue_max: int = 64,
        warm_s: float = 0.0,
        delay_s: float = 0.0,
        engine: str | None = None,
    ):
        self.engine = resolve_detector_engine(engine or os.environ.get("PII_DETECTOR_ENGINE", DEFAULT_DETECTOR_ENGINE))
        self.socket_path = str(Path(directory) / "pii.sock")
        self.stats_path = str(Path(directory) / "stats.json")
        env = os.environ.copy()
        env.update(
            {
                "PII_DETECTOR_ENGINE": self.engine,
                "PII_DETECTOR_TRANSPORT": "shared",
                "PII_SHARED_SOCKET": self.socket_path,
                "PII_SHARED_QUEUE_MAX": str(queue_max),
                "PII_SHARED_FAKE_WARM_S": str(warm_s),
                "PII_SHARED_FAKE_DELAY_S": str(delay_s),
                "PII_SHARED_FAKE_STATS_PATH": self.stats_path,
                "PYTHONPATH": str(Path(__file__).resolve().parents[2]),
            }
        )
        if not fake:
            env["HF_HUB_OFFLINE"] = "1"
        module = "apps.pii.testsupport.fake_shared_detector" if fake else "apps.pii.shared_server"
        self._stdout = Path(directory, "server.stdout").open("w+", encoding="utf-8")  # noqa: SIM115
        self._stderr = Path(directory, "server.stderr").open("w+", encoding="utf-8")  # noqa: SIM115
        self.process = subprocess.Popen(
            [sys.executable, "-m", module],
            env=env,
            stdout=self._stdout,
            stderr=self._stderr,
            text=True,
        )
        self._wait_for_socket()

    def _captured_output(self) -> tuple[str, str]:
        self._stdout.seek(0)
        self._stderr.seek(0)
        return self._stdout.read(), self._stderr.read()

    def _wait_for_socket(self, timeout: float = 180.0) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                stdout, stderr = self._captured_output()
                raise AssertionError(f"shared server exited {self.process.returncode}: {stdout}\n{stderr}")
            if Path(self.socket_path).is_socket():
                return
            time.sleep(0.01)
        raise AssertionError("timed out waiting for shared detector socket")

    def wait_ready(self, timeout: float = 180.0) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            response = _raw_request(self.socket_path, {"v": 1, "ping": True})
            if response.get("ready") is True:
                return
            time.sleep(0.02)
        raise AssertionError("timed out waiting for shared detector readiness")

    def stats(self, timeout: float = 5.0) -> dict:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                return json.loads(Path(self.stats_path).read_text())
            except (FileNotFoundError, json.JSONDecodeError):
                time.sleep(0.01)
        raise AssertionError("timed out reading fake backend stats")

    def close(self) -> None:
        try:
            if self.process.poll() is None:
                self.process.terminate()
            try:
                self.process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=5)
        finally:
            self._stdout.close()
            self._stderr.close()


@contextmanager
def _server(
    *,
    fake: bool = True,
    queue_max: int = 64,
    warm_s: float = 0.0,
    delay_s: float = 0.0,
    engine: str | None = None,
):
    with tempfile.TemporaryDirectory() as directory:
        server = _ServerProcess(
            directory,
            fake=fake,
            queue_max=queue_max,
            warm_s=warm_s,
            delay_s=delay_s,
            engine=engine,
        )
        try:
            yield server
        finally:
            server.close()


def _wait_for_stat(server: _ServerProcess, key: str, minimum: int, timeout: float = 5.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        stats = server.stats()
        if stats[key] >= minimum:
            return stats
        time.sleep(0.01)
    raise AssertionError(f"timed out waiting for fake stat {key}>={minimum}")


class SharedDetectorSubprocessIntegrationTests(SimpleTestCase):
    def test_framing_and_readiness_transition(self):
        with _server(warm_s=0.4) as server:
            warming = _raw_request(server.socket_path, {"v": 1, "ping": True})
            self.assertEqual(warming["v"], 1)
            self.assertFalse(warming["ready"])
            server.wait_ready()
            result = SharedPiiPipeline(socket_path=server.socket_path)("Hello Alice")
        self.assertEqual(result, [{"entity_group": "FIRSTNAME", "score": 0.99, "start": 6, "end": 11}])

    def test_24_thread_burst_is_serialized_to_one_active_inference(self):
        with _server(delay_s=0.01) as server:
            server.wait_ready()
            client = SharedPiiPipeline(socket_path=server.socket_path, deadline_s=10)
            with concurrent.futures.ThreadPoolExecutor(max_workers=24) as executor:
                results = list(executor.map(client, [f"Alice {index}" for index in range(24)]))
            stats = _wait_for_stat(server, "completed", 24)

        self.assertEqual(len(results), 24)
        self.assertTrue(all(result[0]["entity_group"] == "FIRSTNAME" for result in results))
        self.assertEqual(stats["calls"], 24)
        self.assertEqual(stats["completed"], 24)
        self.assertEqual(stats["max_active"], 1)

    def test_queue_full_is_rejected_without_parallel_inference(self):
        barrier = threading.Barrier(24)

        def call(client, index):
            barrier.wait()
            try:
                client(f"Alice {index}")
            except SharedPiiError as exc:
                return exc.outcome
            return "ok"

        with _server(queue_max=1, delay_s=0.25) as server:
            server.wait_ready()
            client = SharedPiiPipeline(socket_path=server.socket_path, deadline_s=10)
            with concurrent.futures.ThreadPoolExecutor(max_workers=24) as executor:
                outcomes = list(executor.map(lambda index: call(client, index), range(24)))
            stats = server.stats()

        self.assertIn("queue_full", outcomes)
        self.assertEqual(stats["queue_full"], outcomes.count("queue_full"))
        self.assertEqual(stats["max_active"], 1)

    def test_queued_request_expires_before_inference(self):
        with _server(queue_max=2, delay_s=0.25) as server:
            server.wait_ready()
            active_result = {}

            def active():
                active_result["response"] = _raw_request(
                    server.socket_path,
                    {"v": 1, "engine": server.engine, "text": "Alice active", "ttl_ms": 2000},
                )

            thread = threading.Thread(target=active, daemon=True)
            thread.start()
            _wait_for_stat(server, "active", 1)
            expired = _raw_request(
                server.socket_path,
                {"v": 1, "engine": server.engine, "text": "Alice expired", "ttl_ms": 30},
            )
            thread.join(2)
            stats = _wait_for_stat(server, "expired", 1)

        self.assertEqual(expired, {"v": 1, "error": "expired"})
        self.assertEqual(active_result["response"]["spans"][0]["entity_group"], "FIRSTNAME")
        self.assertEqual(stats["calls"], 1)

    def test_disconnected_queued_client_is_cancelled_before_inference(self):
        with _server(queue_max=2, delay_s=0.3) as server:
            server.wait_ready()
            active = threading.Thread(
                target=_raw_request,
                args=(
                    server.socket_path,
                    {"v": 1, "engine": server.engine, "text": "Alice active", "ttl_ms": 2000},
                ),
                daemon=True,
            )
            active.start()
            _wait_for_stat(server, "active", 1)
            disconnected = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            disconnected.connect(server.socket_path)
            disconnected.sendall(_frame({"v": 1, "engine": server.engine, "text": "Alice cancelled", "ttl_ms": 2000}))
            disconnected.close()
            active.join(2)
            stats = _wait_for_stat(server, "cancelled", 1)

        self.assertEqual(stats["calls"], 1)
        self.assertEqual(stats["max_active"], 1)


def _percentile(values: list[float], percentile: float) -> float:
    ordered = sorted(values)
    index = max(0, math.ceil(percentile * len(ordered)) - 1)
    return ordered[index]


def _memory_mib(pid: int) -> tuple[float, float | None]:
    rollup = Path(f"/proc/{pid}/smaps_rollup")
    if rollup.exists():
        values = {}
        for line in rollup.read_text().splitlines():
            if line.startswith(("Rss:", "Pss:")):
                key, value, _unit = line.split()
                values[key.rstrip(":")] = int(value) / 1024
        return values["Rss"], values["Pss"]
    completed = subprocess.run(
        ["ps", "-o", "rss=", "-p", str(pid)],
        check=True,
        capture_output=True,
        text=True,
    )
    return int(completed.stdout.strip()) / 1024, None


def _sized_realistic_text(size: int) -> str:
    seed = (
        "Please review the account notes for the customer before tomorrow's meeting. "
        "連絡先と予約内容を確認して、必要な変更を担当者へ共有してください。 "
    )
    return (seed * math.ceil(size / len(seed)))[:size]


@skipUnless(os.environ.get("PII_REAL_MODEL_TESTS") == "1", "Set PII_REAL_MODEL_TESTS=1 for D7 measurements")
class SharedDetectorRealModelTests(SimpleTestCase):
    def test_d7_parity_latency_and_soak(self):
        import torch

        from apps.pii import golden_check
        from apps.pii.redactor import _detect_pii

        torch.set_num_threads(1)
        try:
            torch.set_num_interop_threads(1)
        except RuntimeError:
            pass
        torch.use_deterministic_algorithms(True)
        engine = resolve_detector_engine(os.environ.get("PII_DETECTOR_ENGINE", DEFAULT_DETECTOR_ENGINE))
        if engine == "liquid":
            from apps.pii.liquid_engine import (
                LIQUID_MODEL_REPO,
                LIQUID_MODEL_REVISION,
                get_liquid_pii_pipeline,
            )

            local = get_liquid_pii_pipeline()
            default_model_source = f"{LIQUID_MODEL_REPO}@{LIQUID_MODEL_REVISION}"
        else:
            from apps.pii.engine import get_deberta_pii_pipeline

            local = get_deberta_pii_pipeline()
            default_model_source = "lakshyakh93/deberta_finetuned_pii@a038061af92047b0afbbd5ca07d7aa0521789379"
        model_source = os.environ.get("PII_MODEL_PATH", default_model_source)
        print(
            f"D7 PLATFORM platform={platform.platform()} machine={platform.machine()} "
            f"cpu_count={os.cpu_count()} python={platform.python_version()} "
            f"torch={torch.__version__} engine={engine} model={model_source}"
        )

        golden_rows = json.loads(Path(golden_check.GOLDEN_PATH).read_text())
        corpus = [row["text"] for row in golden_rows] + [case.text for case in CASES]
        policy = TIER_POLICIES["starter"]
        with _server(fake=False, engine=engine) as server:
            server.wait_ready()
            shared = SharedPiiPipeline(socket_path=server.socket_path, engine=engine, deadline_s=300)

            def canonical(spans):
                return [
                    {
                        "entity_group": span["entity_group"],
                        "score": float(span["score"]),
                        "start": span["start"],
                        "end": span["end"],
                    }
                    for span in spans
                ]

            local_raw = []
            shared_raw = []
            local_final = []
            shared_final = []
            for text in corpus:

                def recorded_local(value, local=local):
                    spans = local(value)
                    local_raw.append(canonical(spans))
                    return spans

                with patch("apps.pii.engine.get_pii_pipeline", return_value=recorded_local):
                    local_final.append(_detect_pii(text, policy["entities"], policy["score_threshold"]))

                def recorded_shared(value, shared=shared):
                    spans = shared(value)
                    shared_raw.append(canonical(spans))
                    return spans

                with patch("apps.pii.engine.get_pii_pipeline", return_value=recorded_shared):
                    shared_final.append(_detect_pii(text, policy["entities"], policy["score_threshold"]))
            self.assertEqual(shared_raw, local_raw)
            self.assertEqual(shared_final, local_final)
            print(f"D7 PARITY: raw_spans=IDENTICAL detect_pii=IDENTICAL texts={len(corpus)}")

            with patch.dict(
                os.environ,
                {
                    "PII_DETECTOR_TRANSPORT": "shared",
                    "PII_DETECTOR_ENGINE": engine,
                    "PII_SHARED_SOCKET": server.socket_path,
                    "PII_SHARED_DEADLINE_S": "300",
                },
            ):
                golden_exit = golden_check.main()
                if engine == "deberta":
                    self.assertEqual(golden_exit, 0)
                else:
                    print(f"D7 GOLDEN engine={engine} exit={golden_exit} (differences reported above)")

            sequential_calls = int(os.environ.get("PII_D7_SEQUENTIAL_CALLS", "50"))
            burst_rounds = int(os.environ.get("PII_D7_BURST_ROUNDS", "3"))
            latency_inputs = {
                "unicode4": {size: "😀" * size for size in (200, 8000, 10000, 20000)},
                "realistic": {size: _sized_realistic_text(size) for size in (200, 8000, 10000, 20000)},
            }
            for input_class, sized_inputs in latency_inputs.items():
                for size, text in sized_inputs.items():
                    sequential = []
                    for _index in range(sequential_calls):
                        started = time.perf_counter()
                        shared(text)
                        sequential.append((time.perf_counter() - started) * 1000)
                    print(
                        f"D7 LATENCY class={input_class} size={size} mode=sequential n={sequential_calls} "
                        f"p50_ms={_percentile(sequential, 0.50):.3f} "
                        f"p95_ms={_percentile(sequential, 0.95):.3f} "
                        f"p99_ms={_percentile(sequential, 0.99):.3f}"
                    )

                    burst = []
                    for _round in range(burst_rounds):
                        barrier = threading.Barrier(24)

                        def burst_call(text=text, barrier=barrier):
                            barrier.wait()
                            started = time.perf_counter()
                            shared(text)
                            return (time.perf_counter() - started) * 1000

                        with concurrent.futures.ThreadPoolExecutor(max_workers=24) as executor:
                            burst.extend(executor.map(lambda _index: burst_call(), range(24)))
                    print(
                        f"D7 LATENCY class={input_class} size={size} mode=burst24 "
                        f"n={len(burst)} rounds={burst_rounds} "
                        f"p50_ms={_percentile(burst, 0.50):.3f} "
                        f"p95_ms={_percentile(burst, 0.95):.3f} "
                        f"p99_ms={_percentile(burst, 0.99):.3f}"
                    )

            soak_calls = int(os.environ.get("PII_SHARED_SOAK_CALLS", "2000"))
            samples = []
            short_text = latency_inputs["realistic"][200]
            long_text = latency_inputs["realistic"][20000]
            for index in range(1, soak_calls + 1):
                shared(short_text if index % 2 else long_text)
                if index % 100 == 0:
                    rss_mib, pss_mib = _memory_mib(server.process.pid)
                    samples.append((index, rss_mib, pss_mib))
                    pss_display = f"{pss_mib:.3f}" if pss_mib is not None else "NA"
                    print(f"D7 SOAK sample={index} rss_mib={rss_mib:.3f} pss_mib={pss_display}")

            plateau_index = max(0, math.ceil(len(samples) * 0.75) - 1)
            baseline = samples[plateau_index][2] or samples[plateau_index][1]
            final = samples[-1][2] or samples[-1][1]
            growth = (final - baseline) / baseline if baseline else 0.0
            plateau = growth < 0.05
            rss_high = max(sample[1] for sample in samples)
            pss_values = [sample[2] for sample in samples if sample[2] is not None]
            pss_high = max(pss_values) if pss_values else None
            pss_high_display = f"{pss_high:.3f}" if pss_high is not None else "NA"
            print(
                f"D7 SOAK RESULT calls={soak_calls} rss_high_mib={rss_high:.3f} "
                f"pss_high_mib={pss_high_display} last_quarter_growth_pct={growth * 100:.3f} "
                f"plateau={'YES' if plateau else 'NO'}"
            )
            self.assertTrue(plateau)
