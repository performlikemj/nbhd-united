"""Single-model Unix-socket PII detector server."""

from __future__ import annotations

import json
import logging
import math
import os
import queue
import select
import signal
import socket
import stat
import struct
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from errno import ECONNREFUSED, ENOENT
from numbers import Integral, Real
from pathlib import Path
from typing import Any

from apps.pii.config import DEFAULT_DETECTOR_ENGINE, resolve_detector_engine
from apps.pii.shared_client import (
    DEFAULT_SOCKET_PATH,
    MAX_REQUEST_BYTES,
    MAX_RESPONSE_BYTES,
    MAX_SPANS,
    PROTOCOL_VERSION,
    _length_bucket,
)

logger = logging.getLogger(__name__)
DEFAULT_QUEUE_MAX = 64
_READ_TIMEOUT_S = 5.0


class _FrameError(Exception):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


@dataclass
class _Job:
    connection: socket.socket
    text: str
    deadline: float
    queued_at: float
    received_at: float
    queue_depth: int
    done: threading.Event = field(default_factory=threading.Event)
    response: dict[str, Any] | None = None
    cancelled: bool = False
    cancel_reason: str | None = None


def _env_int(name: str, default: int) -> int:
    try:
        value = int(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


def _recv_exact(connection: socket.socket, size: int) -> bytes:
    chunks: list[bytes] = []
    received = 0
    while received < size:
        chunk = connection.recv(size - received)
        if not chunk:
            raise _FrameError("bad_request")
        chunks.append(chunk)
        received += len(chunk)
    return b"".join(chunks)


def _read_request(connection: socket.socket) -> dict[str, Any]:
    connection.settimeout(_READ_TIMEOUT_S)
    try:
        header = _recv_exact(connection, 4)
        (size,) = struct.unpack("!I", header)
        if size > MAX_REQUEST_BYTES:
            raise _FrameError("too_large")
        body = _recv_exact(connection, size)
        request = json.loads(body.decode("utf-8"))
    except _FrameError:
        raise
    except (TimeoutError, UnicodeDecodeError, json.JSONDecodeError, OSError, struct.error) as exc:
        raise _FrameError("bad_request") from exc
    if not isinstance(request, dict):
        raise _FrameError("bad_request")
    return request


def _encode_response(payload: dict[str, Any]) -> bytes:
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if len(body) > MAX_RESPONSE_BYTES:
        payload = {"v": PROTOCOL_VERSION, "error": "too_large"}
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    return struct.pack("!I", len(body)) + body


def _client_disconnected(connection: socket.socket) -> bool:
    try:
        readable, _, _ = select.select([connection], [], [], 0)
        if not readable:
            return False
        return connection.recv(1, socket.MSG_PEEK) == b""
    except (OSError, ValueError):
        return True


def _remove_stale_socket(path: Path) -> None:
    try:
        mode = path.lstat().st_mode
    except FileNotFoundError:
        return
    if not stat.S_ISSOCK(mode):
        raise RuntimeError("PII shared socket path exists and is not a socket")
    probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    probe.settimeout(0.5)
    try:
        probe.connect(str(path))
    except OSError as exc:
        if exc.errno not in {ECONNREFUSED, ENOENT}:
            raise
    else:
        logger.error("pii_detector_server refusing to start: socket busy")
        raise RuntimeError("PII shared socket busy")
    finally:
        probe.close()
    try:
        path.unlink()
    except FileNotFoundError:
        pass


def _local_pipeline_loader(engine: str) -> Callable[[str], list[dict[str, Any]]]:
    if engine == "liquid":
        from apps.pii.liquid_engine import get_liquid_pii_pipeline

        return get_liquid_pii_pipeline()
    from apps.pii.engine import get_deberta_pii_pipeline

    return get_deberta_pii_pipeline()


def _configure_determinism() -> None:
    os.environ["TOKENIZERS_PARALLELISM"] = "false"
    import torch

    torch.set_num_threads(1)
    torch.set_num_interop_threads(1)
    torch.use_deterministic_algorithms(True)


class SharedDetectorServer:
    """Accept protocol-v1 requests and serialize all inference through one worker."""

    def __init__(
        self,
        *,
        socket_path: str | None = None,
        engine: str | None = None,
        queue_max: int | None = None,
        pipeline_loader: Callable[[str], Callable[[str], list[dict[str, Any]]]] | None = None,
        configure_runtime: bool = True,
    ):
        self.socket_path = socket_path or os.environ.get("PII_SHARED_SOCKET", DEFAULT_SOCKET_PATH)
        self.engine = resolve_detector_engine(engine or os.environ.get("PII_DETECTOR_ENGINE", DEFAULT_DETECTOR_ENGINE))
        self.queue_max = queue_max or _env_int("PII_SHARED_QUEUE_MAX", DEFAULT_QUEUE_MAX)
        self.pipeline_loader = pipeline_loader or _local_pipeline_loader
        self.configure_runtime = configure_runtime
        self.ready = threading.Event()
        self.stopped = threading.Event()
        self._jobs: queue.Queue[_Job | None] = queue.Queue(maxsize=self.queue_max)
        self._listener: socket.socket | None = None
        self._bound = False
        self._pipeline: Callable[[str], list[dict[str, Any]]] | None = None
        self._worker: threading.Thread | None = None
        self._handlers: set[threading.Thread] = set()
        self._handlers_lock = threading.Lock()
        self._stats_lock = threading.Lock()
        self._stats = {"queue_full": 0, "expired": 0, "cancelled": 0}

    @property
    def stats(self) -> dict[str, int]:
        with self._stats_lock:
            return dict(self._stats)

    def _increment_stat(self, key: str) -> None:
        with self._stats_lock:
            self._stats[key] += 1

    def _log_event(
        self,
        *,
        outcome: str,
        text_length: int,
        queue_ms: float = 0.0,
        inference_ms: float = 0.0,
        total_ms: float = 0.0,
        span_count: int = 0,
        queue_depth: int | None = None,
        exception_name: str | None = None,
    ) -> None:
        fields = (
            "pii_detector_server engine=%s outcome=%s queue_ms=%.3f inference_ms=%.3f "
            "total_ms=%.3f len_bucket=%s span_count=%d queue_depth=%d"
        )
        values = (
            self.engine,
            outcome,
            queue_ms,
            inference_ms,
            total_ms,
            _length_bucket(text_length),
            span_count,
            self._jobs.qsize() if queue_depth is None else queue_depth,
        )
        if exception_name:
            logger.error(fields + " exception=%s", *values, exception_name)
        else:
            logger.info(fields, *values)

    def _prepare_socket(self) -> socket.socket:
        path = Path(self.socket_path)
        if not path.parent.exists():
            path.parent.mkdir(mode=0o700, parents=True)
            os.chmod(path.parent, 0o700)
        _remove_stale_socket(path)
        previous_umask = os.umask(0o077)
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            listener.bind(self.socket_path)
            self._bound = True
            listener.listen(self.queue_max + 8)
            listener.settimeout(0.2)
        except Exception:
            listener.close()
            if self._bound:
                try:
                    path.unlink()
                except OSError:
                    pass
                self._bound = False
            raise
        finally:
            os.umask(previous_umask)
        return listener

    def start(self) -> None:
        self._listener = self._prepare_socket()
        self._worker = threading.Thread(target=self._inference_loop, name="pii-inference", daemon=True)
        self._worker.start()

    def serve_forever(self) -> None:
        if self._listener is None:
            self.start()
        assert self._listener is not None
        while not self.stopped.is_set():
            try:
                connection, _ = self._listener.accept()
            except TimeoutError:
                continue
            except OSError:
                if self.stopped.is_set():
                    break
                raise
            handler = threading.Thread(target=self._handle_connection, args=(connection,), daemon=True)
            with self._handlers_lock:
                self._handlers.add(handler)
            handler.start()

    def shutdown(self) -> None:
        self.stopped.set()
        if self._listener is not None:
            self._listener.close()
        try:
            self._jobs.put_nowait(None)
        except queue.Full:
            pass
        if self._worker is not None and self._worker is not threading.current_thread():
            self._worker.join(timeout=2)
        with self._handlers_lock:
            handlers = list(self._handlers)
        for handler in handlers:
            if handler is not threading.current_thread():
                handler.join(timeout=1)
        if self._bound:
            path = Path(self.socket_path)
            try:
                if path.is_socket():
                    path.unlink()
            except OSError:
                pass
            self._bound = False

    def _inference_loop(self) -> None:
        started = time.monotonic()
        try:
            if self.configure_runtime:
                _configure_determinism()
            self._pipeline = self.pipeline_loader(self.engine)
        except Exception as exc:
            self._log_event(
                outcome="not_ready",
                text_length=0,
                total_ms=(time.monotonic() - started) * 1000,
                exception_name=type(exc).__name__,
            )
            self.stopped.set()
            return
        self.ready.set()
        logger.info("pii_detector_server ready engine=%s warm_s=%.3f", self.engine, time.monotonic() - started)

        while not self.stopped.is_set():
            try:
                job = self._jobs.get(timeout=0.2)
            except queue.Empty:
                continue
            if job is None:
                return
            inference_started = time.monotonic()
            queue_ms = (inference_started - job.queued_at) * 1000
            disconnected = _client_disconnected(job.connection)
            if job.cancelled or time.monotonic() >= job.deadline or disconnected:
                if job.cancel_reason == "disconnect" or disconnected:
                    self._increment_stat("cancelled")
                elif job.cancel_reason != "expired":
                    self._increment_stat("expired")
                job.response = {"v": PROTOCOL_VERSION, "error": "expired"}
                job.done.set()
                self._log_event(
                    outcome="expired",
                    text_length=len(job.text),
                    queue_ms=queue_ms,
                    total_ms=(time.monotonic() - job.received_at) * 1000,
                    queue_depth=job.queue_depth,
                )
                continue
            outcome = "ok"
            span_count = 0
            exception_name = None
            try:
                assert self._pipeline is not None
                raw_spans = list(self._pipeline(job.text))
                if len(raw_spans) > MAX_SPANS:
                    raise ValueError("span cap exceeded")
                spans = [self._normalize_span(span, len(job.text)) for span in raw_spans]
                response = {"v": PROTOCOL_VERSION, "engine": self.engine, "spans": spans}
                if len(_encode_response(response)) - 4 > MAX_RESPONSE_BYTES:
                    response = {"v": PROTOCOL_VERSION, "error": "too_large"}
                job.response = response
                span_count = len(spans)
            except Exception as exc:
                outcome = "inference_failed"
                exception_name = type(exc).__name__
                job.response = {"v": PROTOCOL_VERSION, "error": "inference_failed"}
            finally:
                finished = time.monotonic()
                if job.cancelled:
                    outcome = "expired"
                    span_count = 0
                self._log_event(
                    outcome=outcome,
                    text_length=len(job.text),
                    queue_ms=queue_ms,
                    inference_ms=(finished - inference_started) * 1000,
                    total_ms=(finished - job.received_at) * 1000,
                    span_count=span_count,
                    queue_depth=job.queue_depth,
                    exception_name=exception_name,
                )
                job.done.set()

    @staticmethod
    def _normalize_span(span: Any, text_length: int) -> dict[str, Any]:
        if not isinstance(span, dict):
            raise TypeError("span is not an object")
        entity_group = span["entity_group"]
        score = span["score"]
        start = span["start"]
        end = span["end"]
        if not isinstance(entity_group, str) or not entity_group:
            raise ValueError("invalid entity group")
        if (
            isinstance(score, bool)
            or not isinstance(score, Real)
            or not math.isfinite(float(score))
            or not 0.0 <= float(score) <= 1.0
        ):
            raise ValueError("invalid score")
        if (
            isinstance(start, bool)
            or not isinstance(start, Integral)
            or isinstance(end, bool)
            or not isinstance(end, Integral)
        ):
            raise ValueError("invalid offsets")
        if start < 0 or end > text_length or start >= end:
            raise ValueError("invalid offsets")
        return {"entity_group": entity_group, "score": float(score), "start": int(start), "end": int(end)}

    def _handle_connection(self, connection: socket.socket) -> None:
        received_at = time.monotonic()
        try:
            try:
                request = _read_request(connection)
            except _FrameError as exc:
                self._send(connection, {"v": PROTOCOL_VERSION, "error": exc.code})
                self._log_event(
                    outcome=exc.code,
                    text_length=0,
                    total_ms=(time.monotonic() - received_at) * 1000,
                )
                return
            if request == {"v": PROTOCOL_VERSION, "ping": True}:
                self._send(
                    connection,
                    {
                        "v": PROTOCOL_VERSION,
                        "ready": self.ready.is_set(),
                        "engine": self.engine,
                        "protocol": PROTOCOL_VERSION,
                    },
                )
                return
            text_length = len(request.get("text", "")) if isinstance(request.get("text"), str) else 0
            error = self._validate_request(request)
            if error:
                self._send(connection, {"v": PROTOCOL_VERSION, "error": error})
                self._log_event(
                    outcome=error,
                    text_length=text_length,
                    total_ms=(time.monotonic() - received_at) * 1000,
                )
                return
            if not self.ready.is_set():
                self._send(connection, {"v": PROTOCOL_VERSION, "error": "not_ready"})
                self._log_event(
                    outcome="not_ready",
                    text_length=text_length,
                    total_ms=(time.monotonic() - received_at) * 1000,
                )
                return
            deadline = time.monotonic() + request["ttl_ms"] / 1000
            if deadline <= time.monotonic():
                self._send(connection, {"v": PROTOCOL_VERSION, "error": "expired"})
                self._log_event(
                    outcome="expired",
                    text_length=text_length,
                    total_ms=(time.monotonic() - received_at) * 1000,
                )
                return
            queued_at = time.monotonic()
            job = _Job(
                connection=connection,
                text=request["text"],
                deadline=deadline,
                queued_at=queued_at,
                received_at=received_at,
                queue_depth=self._jobs.qsize(),
            )
            try:
                self._jobs.put_nowait(job)
            except queue.Full:
                self._increment_stat("queue_full")
                self._send(connection, {"v": PROTOCOL_VERSION, "error": "queue_full"})
                self._log_event(
                    outcome="queue_full",
                    text_length=text_length,
                    total_ms=(time.monotonic() - received_at) * 1000,
                )
                return
            while not job.done.wait(0.01):
                if _client_disconnected(connection):
                    job.cancelled = True
                    job.cancel_reason = "disconnect"
                    return
                if time.monotonic() >= deadline:
                    job.cancelled = True
                    job.cancel_reason = "expired"
                    self._increment_stat("expired")
                    self._send(connection, {"v": PROTOCOL_VERSION, "error": "expired"})
                    return
            if job.response is not None:
                self._send(connection, job.response)
        finally:
            connection.close()
            with self._handlers_lock:
                self._handlers.discard(threading.current_thread())

    def _validate_request(self, request: dict[str, Any]) -> str | None:
        if request.get("v") != PROTOCOL_VERSION:
            return "bad_request"
        if set(request) != {"v", "engine", "text", "ttl_ms"}:
            return "bad_request"
        if request["engine"] != self.engine:
            return "engine_mismatch"
        if not isinstance(request["text"], str):
            return "bad_request"
        ttl_ms = request["ttl_ms"]
        if isinstance(ttl_ms, bool) or not isinstance(ttl_ms, int) or ttl_ms <= 0:
            return "bad_request"
        return None

    @staticmethod
    def _send(connection: socket.socket, payload: dict[str, Any]) -> None:
        try:
            connection.sendall(_encode_response(payload))
        except OSError:
            return


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    server = SharedDetectorServer()

    def stop(_signum, _frame):
        server.shutdown()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        server.serve_forever()
    finally:
        server.shutdown()


if __name__ == "__main__":
    main()
