"""Router signal handlers."""

from django.db.models.signals import post_delete
from django.dispatch import receiver

from apps.router.models import ChatThread


@receiver(post_delete, sender=ChatThread)
def _bust_main_thread_cache(sender, instance, **kwargs):
    """Invalidate the cached main-thread id when an is_main thread is deleted.

    The ?since= feed caches a tenant's main-thread id (apps/router/chat_views.py)
    to skip a per-poll lookup. That id is treated as immutable, but a delete +
    recreate (admin recovery, teardown, re-seed) would mint a NEW id while the
    cache still held the old one — leaving the feed labelling non-app rows with a
    thread id that no longer exists, for up to the cache TTL. Busting the key on
    deletion closes that window: the next poll re-derives (and re-creates) the
    main thread and caches the fresh id. Fires for ORM and cascade deletes alike.
    """
    if not getattr(instance, "is_main", False):
        return
    from apps.router.chat_views import invalidate_main_thread_cache

    invalidate_main_thread_cache(instance.tenant_id)
