from django.contrib import admin

from .models import AgentSession, MemoryItem, Message


@admin.register(AgentSession)
class AgentSessionAdmin(admin.ModelAdmin):
    list_display = ("title", "tenant", "is_active", "message_count", "updated_at")
    list_filter = ("is_active",)


@admin.register(Message)
class MessageAdmin(admin.ModelAdmin):
    list_display = ("role", "session", "tokens_used", "model_used", "created_at")
    list_filter = ("role",)


@admin.register(MemoryItem)
class MemoryItemAdmin(admin.ModelAdmin):
    list_display = ("key", "category", "tenant", "updated_at")
    list_filter = ("category",)
    search_fields = ("key", "value")
