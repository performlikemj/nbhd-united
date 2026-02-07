import uuid

from django.db import models

from apps.tenants.models import Tenant


class AgentSession(models.Model):
    """A conversation session between a user and their agent."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE, related_name="sessions")
    title = models.CharField(max_length=255, blank=True, default="")
    is_active = models.BooleanField(default=True)
    message_count = models.IntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "agent_sessions"
        ordering = ["-updated_at"]

    def __str__(self):
        return self.title or f"Session {self.id!s:.8}"


class Message(models.Model):
    """A single message in a session."""

    class Role(models.TextChoices):
        USER = "user", "User"
        ASSISTANT = "assistant", "Assistant"
        SYSTEM = "system", "System"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    session = models.ForeignKey(AgentSession, on_delete=models.CASCADE, related_name="messages")
    role = models.CharField(max_length=20, choices=Role.choices)
    content = models.TextField()
    metadata = models.JSONField(default=dict, blank=True)
    tokens_used = models.IntegerField(default=0)
    model_used = models.CharField(max_length=255, blank=True, default="")
    cost_estimate = models.DecimalField(max_digits=10, decimal_places=6, default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "messages"
        ordering = ["created_at"]

    def __str__(self):
        return f"[{self.role}] {self.content[:50]}"


class MemoryItem(models.Model):
    """Long-term memory that persists across sessions."""

    class Category(models.TextChoices):
        GENERAL = "general", "General"
        PREFERENCE = "preference", "Preference"
        FACT = "fact", "Fact"
        INSTRUCTION = "instruction", "Instruction"
        CONTACT = "contact", "Contact"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE, related_name="memory_items")
    key = models.CharField(max_length=255)
    value = models.TextField()
    category = models.CharField(
        max_length=20, choices=Category.choices, default=Category.GENERAL
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "memory_items"
        unique_together = [("tenant", "key")]

    def __str__(self):
        return f"{self.key}: {self.value[:50]}"
