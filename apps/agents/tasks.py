"""Celery tasks for async agent runs."""
from celery import shared_task


@shared_task
def run_agent_async(session_id: str, user_message: str):
    """Run an agent completion asynchronously."""
    from .models import AgentSession
    from .services import AgentRunner

    session = AgentSession.objects.get(id=session_id)
    runner = AgentRunner()
    runner.run(session=session, user_message=user_message)
