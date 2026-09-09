"""Content-free live composer probe; run A then C with the same request ledger.

Reads OPENROUTER_API_KEY from the environment only. At most six HTTP requests in A,
twelve across both runs, including retries and schema fallbacks. Reserve one
request for each remaining row, so a failing pin cannot exhaust the whole run.
"""

import argparse
import fcntl
import json
import logging
import os
import re
import sys
import time
from pathlib import Path
from unittest.mock import patch


class BudgetExhausted(RuntimeError):
    pass


class Capture(logging.Handler):
    def __init__(self):
        super().__init__()
        self.lines = []

    def emit(self, record):
        self.lines.append(record.getMessage())


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--phase", choices=["A", "C"], required=True)
    parser.add_argument("--ledger", type=Path, default=Path("/tmp/completion-truth-live-budget.json"))
    args = parser.parse_args()
    if not os.environ.get("OPENROUTER_API_KEY"):
        parser.error("OPENROUTER_API_KEY must be set")
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.test")
    os.environ.setdefault("SECRET_KEY", "compose-smoke-only")
    os.environ.setdefault("DATABASE_URL", "sqlite:////tmp/smoke.db")
    import django

    django.setup()
    from apps.billing.constants import DEEPSEEK_FLASH_MODEL, DEEPSEEK_MODEL, GEMMA_MODEL
    from apps.common import openrouter
    from apps.core import compose, render
    from apps.core.lesson import MeditationLesson

    signals = {
        "preferred_duration_minutes": 10,
        "recent_notes": [
            "A busy work week left me replaying unfinished conversations after dinner.",
            "A short walk helped me notice my shoulders soften; I want to carry that ease into tomorrow.",
        ],
        "recent_meditations": [
            {
                "date": "2026-09-08",
                "title": "Less Force",
                "theme": "Allow effort to soften.",
                "lesson": {"tradition": "taoist", "teaching_slug": "wu-wei"},
            },
            {
                "date": "2026-09-07",
                "title": "Fresh Attention",
                "theme": "Meet the familiar freshly.",
                "lesson": {"tradition": "zen", "teaching_slug": "beginners-mind"},
            },
            {
                "date": "2026-09-06",
                "title": "A Finite Day",
                "theme": "Give time to what matters.",
                "lesson": {"tradition": "stoic", "teaching_slug": "memento-mori"},
            },
        ],
    }
    capture = Capture()
    for name in ("apps.core.compose", "apps.common.openrouter"):
        logger = logging.getLogger(name)
        logger.handlers = [capture]
        logger.propagate = False
        logger.setLevel(logging.INFO)

    original_post = openrouter.requests.post
    original_completion = openrouter.chat_completion
    # Lock for the whole run; count BEFORE sending, including transport failures.
    with args.ledger.open("a+") as ledger:
        fcntl.flock(ledger, fcntl.LOCK_EX)
        ledger.seek(0)
        state = json.loads(ledger.read() or '{"total": 0, "phases": []}')
        if args.phase in state["phases"]:
            parser.error("This phase already ran with this ledger")
        state["phases"].append(args.phase)

        def save():
            ledger.seek(0)
            ledger.truncate()
            json.dump(state, ledger)
            ledger.flush()
            os.fsync(ledger.fileno())

        save()
        run_start = state["total"]
        for index, (label, model) in enumerate(
            [
                ("Gemma", GEMMA_MODEL),
                ("DeepSeek Flash", DEEPSEEK_FLASH_MODEL),
                ("DeepSeek Pro", DEEPSEEK_MODEL),
                ("default chain", ""),
            ]
        ):
            capture.lines.clear()
            attempts = []
            row = {"phase": args.phase, "call": label}
            limit = (min(12, run_start + 6) if args.phase == "A" else 12) - (3 - index)

            def post(*pos, limit=limit, attempts=attempts, **kw):
                if state["total"] >= limit:
                    raise BudgetExhausted("live request budget reserved for remaining calls")
                state["total"] += 1
                save()
                attempt = {"model": kw["json"]["model"], "format": kw["json"]["response_format"]["type"]}
                attempts.append(attempt)
                try:
                    response = original_post(*pos, **kw)
                except Exception as exc:
                    attempt["transport"] = type(exc).__name__
                    raise
                attempt["status"] = response.status_code
                try:
                    data = response.json()
                except ValueError:
                    attempt["response"] = "non-JSON HTTP body"
                else:
                    attempt["response"] = "usable choices" if openrouter._looks_usable(data) else "no usable choices"
                    if isinstance(data, dict):
                        error = data.get("error")
                        if isinstance(error, dict):
                            attempt["error_code"] = error.get("code")
                            # Provider error metadata only; never dump response or choices.
                            attempt["error_message"] = str(error.get("message", ""))[:120]
                return response

            def completion(*pos, **kw):
                kw["record_health"] = False
                return original_completion(*pos, **kw)

            started = time.monotonic()
            with (
                patch.object(openrouter.requests, "post", side_effect=post),
                patch.object(compose, "chat_completion", side_effect=completion),
                patch(
                    "django.db.backends.base.base.BaseDatabaseWrapper.ensure_connection",
                    side_effect=AssertionError("database access forbidden in live smoke"),
                ),
            ):
                try:
                    manifest = compose.author_manifest(signals, model=model, tenant=None)
                except Exception as exc:
                    # Old exceptions may embed response content: report classification only.
                    detail = str(exc)
                    safe = next(
                        (
                            s
                            for s in (
                                "live request budget reserved for remaining calls",
                                "non-JSON response",
                                "OpenRouter returned no usable choices",
                                "invalid manifest/lesson",
                                "invalid manifest",
                                "LLM call failed",
                                "variety_clash",
                            )
                            if s in detail
                        ),
                        "unclassified failure (detail withheld)",
                    )
                    row.update(exception=f"{type(exc).__name__}: {safe}"[:120], manifest_valid=None, lesson_valid=None)
                else:
                    errors = render.validate_manifest(manifest)
                    MeditationLesson.model_validate(manifest["lesson"])
                    row.update(
                        tradition=manifest["lesson"]["tradition"],
                        teaching_slug=manifest["lesson"]["teaching_slug"],
                        phases=len(manifest["phases"]),
                        speech_segments=sum(
                            s.get("type") == "speech" for p in manifest["phases"] for s in p["segments"]
                        ),
                        validation=errors,
                        manifest_valid=not errors,
                        lesson_valid=True,
                    )
            row["wall_seconds"] = round(time.monotonic() - started, 2)
            row["attempts"] = attempts
            row["retries_used"] = len(attempts) - len({a["model"] for a in attempts})
            row["schema_fallback_logged"] = any("schema rejected" in line for line in capture.lines)
            row["outcome"] = next((line for line in reversed(capture.lines) if "outcome=" in line), "none")
            # Only content-free validation type identifiers from old/new failure logs.
            row["validation_error_types"] = [
                match.group(1)
                for line in capture.lines
                if (match := re.search(r"invalid manifest/lesson: ([a-z_, ]+)", line))
            ]
            print(json.dumps(row), flush=True)
        print(json.dumps({"total_live_requests": state["total"]}), flush=True)


if __name__ == "__main__":
    main()
