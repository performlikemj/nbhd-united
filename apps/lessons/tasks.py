"""QStash-callable task functions for the lessons app."""
from __future__ import annotations

import logging
from io import StringIO

logger = logging.getLogger(__name__)


def reseed_lessons_task() -> dict:
    """Delete journal-sourced lessons and re-extract from all daily notes."""
    from django.core.management import call_command

    out = StringIO()
    call_command("reseed_lessons", stdout=out)
    output = out.getvalue()
    logger.info("reseed_lessons_task: %s", output[-1000:])
    return {"ok": True, "output_tail": output[-1000:]}
