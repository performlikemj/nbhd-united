"""Tenant provisioning logic."""
from django.utils.text import slugify

from .models import AgentConfig, Tenant, User


def provision_tenant(
    display_name: str,
    telegram_chat_id: int,
    telegram_user_id: int | None = None,
    telegram_username: str | None = None,
    language: str = "en",
) -> tuple[Tenant, User]:
    """Create a new tenant with user and default agent config.

    Returns (tenant, user) tuple.
    """
    # Generate unique slug
    base_slug = slugify(display_name or "user")
    slug = base_slug
    counter = 1
    while Tenant.objects.filter(slug=slug).exists():
        slug = f"{base_slug}-{counter}"
        counter += 1

    tenant = Tenant.objects.create(
        name=display_name or "New User",
        slug=slug,
    )

    user = User.objects.create_user(
        username=f"tg_{telegram_chat_id}",
        tenant=tenant,
        telegram_chat_id=telegram_chat_id,
        telegram_user_id=telegram_user_id,
        display_name=display_name or "Friend",
        language=language,
    )

    AgentConfig.objects.create(tenant=tenant)

    return tenant, user
