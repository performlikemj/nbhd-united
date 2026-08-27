"""Lightweight subprocess backend used by shared-detector integration tests."""

from __future__ import annotations

import json
import math
import os
import signal
import threading
import time
from pathlib import Path
from typing import Any

from apps.pii.shared_server import SharedDetectorServer


def _env_float(name: str, default: float) -> float:
    try:
        value = float(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        return default
    return value if math.isfinite(value) and value >= 0 else default


class _AlicePipeline:
    def __init__(self, delay_s: float):
        self.delay_s = delay_s
        self._lock = threading.Lock()
        self._stats = {"calls": 0, "completed": 0, "active": 0, "max_active": 0}

    @property
    def stats(self) -> dict[str, int]:
        with self._lock:
            return dict(self._stats)

    def __call__(self, text: str) -> list[dict[str, Any]]:
        with self._lock:
            self._stats["calls"] += 1
            self._stats["active"] += 1
            self._stats["max_active"] = max(self._stats["max_active"], self._stats["active"])
        try:
            time.sleep(self.delay_s)
            start = text.find("Alice")
            if start < 0:
                return []
            return [{"entity_group": "FIRSTNAME", "score": 0.99, "start": start, "end": start + 5}]
        finally:
            with self._lock:
                self._stats["active"] -= 1
                self._stats["completed"] += 1


def _write_stats(path: Path, server: SharedDetectorServer, pipeline: _AlicePipeline) -> None:
    stats = pipeline.stats
    stats.update(server.stats)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(stats, sort_keys=True))
    os.replace(temporary, path)


def main() -> None:
    warm_s = _env_float("PII_SHARED_FAKE_WARM_S", 0.0)
    pipeline = _AlicePipeline(_env_float("PII_SHARED_FAKE_DELAY_S", 0.0))

    def load_pipeline(_engine: str) -> _AlicePipeline:
        time.sleep(warm_s)
        return pipeline

    server = SharedDetectorServer(pipeline_loader=load_pipeline, configure_runtime=False)
    stats_path_value = os.environ.get("PII_SHARED_FAKE_STATS_PATH")
    stats_path = Path(stats_path_value) if stats_path_value else None
    writer_stopped = threading.Event()

    def write_stats() -> None:
        while not writer_stopped.wait(0.01):
            if stats_path is not None:
                _write_stats(stats_path, server, pipeline)

    writer = threading.Thread(target=write_stats, name="pii-test-stats", daemon=True)
    writer.start()

    def stop(_signum, _frame) -> None:
        server.shutdown()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        server.serve_forever()
    finally:
        writer_stopped.set()
        writer.join(timeout=1)
        if stats_path is not None:
            _write_stats(stats_path, server, pipeline)
        server.shutdown()


if __name__ == "__main__":
    main()
