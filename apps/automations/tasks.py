"""Cron-invoked automation tasks."""
from __future__ import annotations

from .scheduler import run_due_automations


def run_due_automations_task() -> dict:
    return run_due_automations()
