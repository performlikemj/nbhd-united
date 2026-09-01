"""Emit a canonical cross-platform manifest for the real PII engines.

The corpus is synthetic and tracked in this repository. Model progress and
library diagnostics belong on stderr; stdout is reserved for one canonical JSON
document so callers can compare it byte-for-byte across operating systems.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Iterable
from pathlib import Path
from typing import Any
from unittest.mock import patch

from apps.pii.config import TIER_POLICIES


def _ensure_django() -> None:
    from django.conf import settings

    if settings.configured:
        return
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.base")
    import django

    django.setup()


def _load_corpus(name: str) -> list[dict[str, str]]:
    if name not in {"golden", "golden+eval"}:
        raise ValueError(f"unsupported corpus: {name}")

    from apps.pii.golden_check import GOLDEN_PATH

    golden_rows = json.loads(Path(GOLDEN_PATH).read_text(encoding="utf-8"))
    rows = [{"id": row["id"], "source": "golden", "text": row["text"]} for row in golden_rows]
    if name == "golden+eval":
        from apps.pii.eval_corpus import CASES

        rows.extend({"id": case.id, "source": "eval", "text": case.text} for case in CASES)
    return sorted(rows, key=lambda row: (row["source"], row["id"], row["text"]))


def _canonical_raw_spans(spans: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    canonical = [
        {
            "end": int(span["end"]),
            "entity_group": str(span["entity_group"]),
            "score": float(span["score"]),
            "start": int(span["start"]),
            "word": str(span.get("word", "")),
        }
        for span in spans
    ]
    return sorted(
        canonical,
        key=lambda span: (
            span["start"],
            span["end"],
            span["entity_group"],
            span["score"],
            span["word"],
        ),
    )


def _canonical_detected(spans: Iterable[Any], text: str) -> list[dict[str, Any]]:
    canonical = [
        {
            "end": int(span.end),
            "entity_type": str(span.entity_type),
            "score": float(span.score),
            "source": str(span.source),
            "start": int(span.start),
            "text": text[int(span.start) : int(span.end)],
        }
        for span in spans
    ]
    return sorted(
        canonical,
        key=lambda span: (
            span["start"],
            span["end"],
            span["entity_type"],
            span["source"],
            span["score"],
            span["text"],
        ),
    )


def build_manifest(corpus_name: str, engines: Iterable[str]) -> dict[str, Any]:
    _ensure_django()
    from apps.pii.engine import get_pii_pipeline
    from apps.pii.redactor import _detect_pii

    corpus = _load_corpus(corpus_name)
    policy = TIER_POLICIES["starter"]
    engine_manifests: dict[str, list[dict[str, Any]]] = {}
    for engine in engines:
        print(f"span-manifest: loading {engine}", file=sys.stderr, flush=True)
        with patch.dict(
            os.environ,
            {"PII_DETECTOR_ENGINE": engine, "PII_DETECTOR_TRANSPORT": "local"},
        ):
            pipeline = get_pii_pipeline()
            cases = []
            for row in corpus:
                raw_spans = pipeline(row["text"])
                detected = _detect_pii(
                    row["text"],
                    policy["entities"],
                    policy["score_threshold"],
                )
                cases.append(
                    {
                        "detect_pii": _canonical_detected(detected, row["text"]),
                        "id": row["id"],
                        "raw_spans": _canonical_raw_spans(raw_spans),
                        "source": row["source"],
                        "text": row["text"],
                    }
                )
        engine_manifests[engine] = cases
        print(f"span-manifest: completed {engine} ({len(cases)} texts)", file=sys.stderr, flush=True)

    return {
        "corpus": corpus_name,
        "engines": engine_manifests,
        "schema_version": 1,
    }


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--corpus", choices=("golden", "golden+eval"), required=True)
    parser.add_argument(
        "--engine",
        choices=("both", "deberta", "liquid"),
        default="both",
        help="engine subset to include (default: both)",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    engines = ("deberta", "liquid") if args.engine == "both" else (args.engine,)
    manifest = build_manifest(args.corpus, engines)
    print(json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
