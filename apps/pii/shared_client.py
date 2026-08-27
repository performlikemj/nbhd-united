"""Unix-socket client for the process-shared PII detector."""

from __future__ import annotations

import json
import logging
import math
import os
import socket
import struct
import threading
import time
from typing import Any

from apps.pii.config import DEFAULT_DETECTOR_ENGINE, resolve_detector_engine

logger = logging.getLogger(__name__)
PROTOCOL_VERSION = 1
DEFAULT_SOCKET_PATH = "/run/nbhd/pii-detector.sock"
DEFAULT_DEADLINE_S = 5.0
MAX_REQUEST_BYTES = 1024 * 1024
MAX_RESPONSE_BYTES = 1024 * 1024
MAX_SPANS = 4096
_BREAKER_FAILURE_LIMIT = 3
_BREAKER_OPEN_S = 30.0
_SERVER_ERRORS = {
    "queue_full",
    "expired",
    "engine_mismatch",
    "bad_request",
    "too_large",
    "not_ready",
    "inference_failed",
}
_BREAKER_OUTCOMES = {"timeout", "connect", "protocol", "engine_mismatch", "bad_response"}


def _length_bucket(length: int) -> str:
    if length <= 255:
        return "0-255"
    if length <= 1023:
        return "256-1023"
    if length <= 8191:
        return "1024-8191"
    if length <= 19999:
        return "8192-19999"
    return "20000+"


class SharedPiiError(Exception):
    """A complete, fail-closed description of one shared-detector failure."""

    def __init__(self, message: str, *, outcome: str = "bad_response"):
        super().__init__(message)
        self.outcome = outcome


def _env_float(name: str, default: float) -> float:
    try:
        value = float(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        return default
    return value if math.isfinite(value) and value > 0 else default


def _remaining(deadline: float) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise SharedPiiError("shared detector deadline exceeded", outcome="timeout")
    return remaining


def _recv_exact(connection: socket.socket, size: int, deadline: float) -> bytes:
    chunks: list[bytes] = []
    received = 0
    while received < size:
        connection.settimeout(_remaining(deadline))
        try:
            chunk = connection.recv(size - received)
        except TimeoutError as exc:
            raise SharedPiiError("shared detector deadline exceeded", outcome="timeout") from exc
        except OSError as exc:
            raise SharedPiiError("shared detector response read failed", outcome="protocol") from exc
        if not chunk:
            raise SharedPiiError("shared detector response was truncated", outcome="protocol")
        chunks.append(chunk)
        received += len(chunk)
    return b"".join(chunks)


def _encode_frame(payload: dict[str, Any], *, cap: int) -> bytes:
    try:
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise SharedPiiError("shared detector request could not be encoded", outcome="bad_response") from exc
    if len(body) > cap:
        raise SharedPiiError("shared detector frame exceeds byte cap", outcome="bad_response")
    return struct.pack("!I", len(body)) + body


def _decode_response(connection: socket.socket, deadline: float) -> dict[str, Any]:
    header = _recv_exact(connection, 4, deadline)
    (size,) = struct.unpack("!I", header)
    if size > MAX_RESPONSE_BYTES:
        raise SharedPiiError("shared detector response exceeds byte cap", outcome="protocol")
    body = _recv_exact(connection, size, deadline)
    try:
        response = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SharedPiiError("shared detector response is not valid JSON", outcome="bad_response") from exc
    if not isinstance(response, dict):
        raise SharedPiiError("shared detector response is not an object", outcome="bad_response")
    return response


def _validate_span(span: Any, text_length: int) -> dict[str, Any]:
    if not isinstance(span, dict) or set(span) != {"entity_group", "score", "start", "end"}:
        raise SharedPiiError("shared detector returned an invalid span shape", outcome="bad_response")
    entity_group = span["entity_group"]
    score = span["score"]
    start = span["start"]
    end = span["end"]
    if not isinstance(entity_group, str) or not entity_group:
        raise SharedPiiError("shared detector returned an invalid entity group", outcome="bad_response")
    if isinstance(score, bool) or not isinstance(score, (int, float)):
        raise SharedPiiError("shared detector returned an invalid score", outcome="bad_response")
    score = float(score)
    if not math.isfinite(score) or not 0.0 <= score <= 1.0:
        raise SharedPiiError("shared detector returned an invalid score", outcome="bad_response")
    if isinstance(start, bool) or not isinstance(start, int):
        raise SharedPiiError("shared detector returned an invalid start", outcome="bad_response")
    if isinstance(end, bool) or not isinstance(end, int):
        raise SharedPiiError("shared detector returned an invalid end", outcome="bad_response")
    if start < 0 or end > text_length or start >= end:
        raise SharedPiiError("shared detector returned invalid span offsets", outcome="bad_response")
    return {"entity_group": entity_group, "score": score, "start": start, "end": end}


class SharedPiiPipeline:
    """Callable client with one absolute deadline and a small circuit breaker."""

    def __init__(
        self,
        *,
        socket_path: str | None = None,
        engine: str | None = None,
        deadline_s: float | None = None,
        clock=time.monotonic,
    ):
        self.socket_path = socket_path or os.environ.get("PII_SHARED_SOCKET", DEFAULT_SOCKET_PATH)
        self.engine = resolve_detector_engine(engine or os.environ.get("PII_DETECTOR_ENGINE", DEFAULT_DETECTOR_ENGINE))
        self.deadline_s = (
            deadline_s if deadline_s is not None else _env_float("PII_SHARED_DEADLINE_S", DEFAULT_DEADLINE_S)
        )
        self._clock = clock
        self._breaker_lock = threading.Lock()
        self._consecutive_failures = 0
        self._open_until = 0.0
        self._half_open_probe = False

    def _before_call(self) -> bool:
        now = self._clock()
        with self._breaker_lock:
            if not self._open_until:
                return False
            if now < self._open_until or self._half_open_probe:
                raise SharedPiiError("shared detector circuit is open", outcome="circuit_open")
            self._half_open_probe = True
            return True

    def _record_success(self) -> None:
        with self._breaker_lock:
            self._consecutive_failures = 0
            self._open_until = 0.0
            self._half_open_probe = False

    def _record_failure(self, outcome: str, half_open: bool) -> None:
        with self._breaker_lock:
            self._half_open_probe = False
            if outcome not in _BREAKER_OUTCOMES:
                if half_open:
                    self._consecutive_failures = 0
                    self._open_until = 0.0
                return
            self._consecutive_failures += 1
            if half_open or self._consecutive_failures >= _BREAKER_FAILURE_LIMIT:
                self._open_until = self._clock() + _BREAKER_OPEN_S

    def _log_call(self, *, outcome: str, started: float, text_length: int, span_count: int) -> None:
        logger.info(
            "pii_detector_client engine=%s transport=shared outcome=%s latency_ms=%.3f len_bucket=%s span_count=%d",
            self.engine,
            outcome,
            (time.monotonic() - started) * 1000,
            _length_bucket(text_length),
            span_count,
        )

    def __call__(self, text: str) -> list[dict[str, Any]]:
        started = time.monotonic()
        text_length = len(text) if isinstance(text, str) else 0
        if not isinstance(text, str):
            self._log_call(outcome="bad_response", started=started, text_length=0, span_count=0)
            raise SharedPiiError("shared detector input must be text", outcome="bad_response")
        half_open = False
        try:
            half_open = self._before_call()
            spans = self._call(text)
        except SharedPiiError as exc:
            self._record_failure(exc.outcome, half_open)
            self._log_call(outcome=exc.outcome, started=started, text_length=text_length, span_count=0)
            raise
        self._record_success()
        self._log_call(outcome="ok", started=started, text_length=text_length, span_count=len(spans))
        return spans

    def _call(self, text: str) -> list[dict[str, Any]]:
        deadline = time.monotonic() + self.deadline_s
        connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            connection.settimeout(_remaining(deadline))
            try:
                connection.connect(self.socket_path)
            except TimeoutError as exc:
                raise SharedPiiError("shared detector connection timed out", outcome="timeout") from exc
            except OSError as exc:
                raise SharedPiiError("shared detector connection failed", outcome="connect") from exc

            ttl_ms = max(1, int(_remaining(deadline) * 1000))
            frame = _encode_frame(
                {"v": PROTOCOL_VERSION, "engine": self.engine, "text": text, "ttl_ms": ttl_ms},
                cap=MAX_REQUEST_BYTES,
            )
            connection.settimeout(_remaining(deadline))
            try:
                connection.sendall(frame)
            except TimeoutError as exc:
                raise SharedPiiError("shared detector request timed out", outcome="timeout") from exc
            except OSError as exc:
                raise SharedPiiError("shared detector request write failed", outcome="protocol") from exc
            response = _decode_response(connection, deadline)
        finally:
            connection.close()

        if response.get("v") != PROTOCOL_VERSION:
            raise SharedPiiError("shared detector protocol version mismatch", outcome="protocol")
        if "error" in response:
            if set(response) != {"v", "error"} or response["error"] not in _SERVER_ERRORS:
                raise SharedPiiError("shared detector returned an unknown error", outcome="bad_response")
            error = response["error"]
            outcome = error if error in {"queue_full", "expired", "engine_mismatch", "not_ready"} else "bad_response"
            raise SharedPiiError(f"shared detector returned {error}", outcome=outcome)
        if set(response) != {"v", "engine", "spans"}:
            raise SharedPiiError("shared detector returned an invalid response shape", outcome="bad_response")
        if response["engine"] != self.engine:
            raise SharedPiiError("shared detector response engine mismatch", outcome="engine_mismatch")
        raw_spans = response["spans"]
        if not isinstance(raw_spans, list) or len(raw_spans) > MAX_SPANS:
            raise SharedPiiError("shared detector returned an invalid span list", outcome="bad_response")
        return [_validate_span(span, len(text)) for span in raw_spans]


_shared_pipeline: SharedPiiPipeline | None = None
_shared_pipeline_key: tuple[str, str, float] | None = None
_shared_pipeline_lock = threading.Lock()


def get_shared_pii_pipeline() -> SharedPiiPipeline:
    """Return the process-local lightweight client singleton."""
    global _shared_pipeline, _shared_pipeline_key
    socket_path = os.environ.get("PII_SHARED_SOCKET", DEFAULT_SOCKET_PATH)
    engine = resolve_detector_engine(os.environ.get("PII_DETECTOR_ENGINE", DEFAULT_DETECTOR_ENGINE))
    deadline_s = _env_float("PII_SHARED_DEADLINE_S", DEFAULT_DEADLINE_S)
    key = (socket_path, engine, deadline_s)
    with _shared_pipeline_lock:
        if _shared_pipeline is None or _shared_pipeline_key != key:
            _shared_pipeline = SharedPiiPipeline(socket_path=socket_path, engine=engine, deadline_s=deadline_s)
            _shared_pipeline_key = key
        return _shared_pipeline


def ping_shared_detector(*, timeout_s: float = 1.0, socket_path: str | None = None) -> bool:
    """Return whether a protocol-v1 server is ready for the configured engine."""
    path = socket_path or os.environ.get("PII_SHARED_SOCKET", DEFAULT_SOCKET_PATH)
    engine = resolve_detector_engine(os.environ.get("PII_DETECTOR_ENGINE", DEFAULT_DETECTOR_ENGINE))
    deadline = time.monotonic() + timeout_s
    connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        connection.settimeout(_remaining(deadline))
        connection.connect(path)
        connection.sendall(_encode_frame({"v": PROTOCOL_VERSION, "ping": True}, cap=MAX_REQUEST_BYTES))
        response = _decode_response(connection, deadline)
    except (OSError, SharedPiiError):
        return False
    finally:
        connection.close()
    return (
        set(response) == {"v", "ready", "engine", "protocol"}
        and response["v"] == PROTOCOL_VERSION
        and response["protocol"] == PROTOCOL_VERSION
        and response["engine"] == engine
        and response["ready"] is True
    )
