"""Agent runner — routes messages to LLMs via litellm/OpenRouter."""
import logging

from django.conf import settings

import litellm

from apps.tenants.models import AgentConfig
from .models import AgentSession, Message

logger = logging.getLogger(__name__)

# Tiered model routing
MODEL_TIERS = {
    "free": [
        "openrouter/meta-llama/llama-3.1-8b-instruct:free",
        "openrouter/mistralai/mistral-7b-instruct:free",
    ],
    "paid": [
        "openrouter/openai/gpt-4o-mini",
        "openrouter/anthropic/claude-3.5-sonnet",
    ],
    "sponsor": [
        "openrouter/anthropic/claude-3.5-sonnet",
        "openrouter/openai/gpt-4o",
    ],
}


class AgentRunner:
    """Runs agent completions for a tenant."""

    def run(self, session: AgentSession, user_message: str) -> Message:
        """Process a user message and return the assistant response."""
        # Save user message
        user_msg = Message.objects.create(
            session=session,
            role=Message.Role.USER,
            content=user_message,
        )
        session.message_count += 1
        session.save(update_fields=["message_count", "updated_at"])

        # Load agent config
        try:
            config = AgentConfig.objects.get(tenant=session.tenant)
        except AgentConfig.DoesNotExist:
            config = None

        # Build messages for LLM
        system_prompt = (
            config.system_prompt if config
            else "You are a helpful assistant for the Neighborhood United community."
        )
        model = self._select_model(session.tenant, config)
        context_messages = self._build_context(session, system_prompt)

        # Call LLM
        try:
            response = litellm.completion(
                model=model,
                messages=context_messages,
                max_tokens=config.max_tokens_per_message if config else 2048,
                temperature=config.temperature if config else 0.7,
                api_key=settings.OPENROUTER_API_KEY,
            )
            content = response.choices[0].message.content
            tokens_used = response.usage.total_tokens if response.usage else 0
            model_used = response.model or model
        except Exception:
            logger.exception("LLM call failed for tenant %s", session.tenant_id)
            content = "I'm sorry, I'm having trouble right now. Please try again in a moment."
            tokens_used = 0
            model_used = ""

        # Save assistant message
        assistant_msg = Message.objects.create(
            session=session,
            role=Message.Role.ASSISTANT,
            content=content,
            tokens_used=tokens_used,
            model_used=model_used,
        )
        session.message_count += 1
        session.save(update_fields=["message_count", "updated_at"])

        return assistant_msg

    def _select_model(self, tenant, config) -> str:
        """Select model based on tenant's plan tier."""
        if config and config.model_override:
            return config.model_override

        tier = tenant.plan_tier if tenant else "free"
        models = MODEL_TIERS.get(tier, MODEL_TIERS["free"])
        return models[0]

    def _build_context(self, session: AgentSession, system_prompt: str) -> list[dict]:
        """Build the message context for the LLM call."""
        messages = [{"role": "system", "content": system_prompt}]

        # Last 20 messages for context
        recent = Message.objects.filter(session=session).order_by("-created_at")[:20]
        for msg in reversed(recent):
            messages.append({"role": msg.role, "content": msg.content})

        return messages
