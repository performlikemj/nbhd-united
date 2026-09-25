"""Request-boundary reminder fence; never locks or wraps transport I/O."""

from rest_framework.response import Response


def cron_edits_fenced(tenant) -> bool:
    """Read the already-loaded tenant field, without queries or transactions."""
    return tenant.openclaw_migration_cron_fenced


def cron_fenced_response(*, assistant=False):
    body = {"error": "assistant_updating", "retry_after": 60}
    if assistant:
        body["detail"] = "Your assistant is updating. Please try changing your reminders again in one minute."
    return Response(body, status=409)
