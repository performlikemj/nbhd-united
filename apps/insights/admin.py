from django.contrib import admin

from .models import (
    AssistantInsight,
    PillarSnapshot,
    TopicAlias,
    TopicRegistry,
    UserVoicePref,
)


@admin.register(TopicRegistry)
class TopicRegistryAdmin(admin.ModelAdmin):
    list_display = ("pillar", "slug", "display_name", "status", "source", "created_at")
    list_filter = ("pillar", "status", "source")
    search_fields = ("slug", "display_name", "description")
    ordering = ("pillar", "slug")


@admin.register(TopicAlias)
class TopicAliasAdmin(admin.ModelAdmin):
    list_display = ("topic", "alias", "source", "created_at")
    list_filter = ("source",)
    search_fields = ("alias",)


@admin.register(AssistantInsight)
class AssistantInsightAdmin(admin.ModelAdmin):
    list_display = ("tenant", "pillar", "topic", "status", "confidence", "created_at")
    list_filter = ("pillar", "status")
    search_fields = ("statement",)
    readonly_fields = ("created_at", "last_confirmed_at", "last_refuted_at")


@admin.register(PillarSnapshot)
class PillarSnapshotAdmin(admin.ModelAdmin):
    list_display = ("tenant", "pillar", "granularity", "ts")
    list_filter = ("pillar", "granularity")
    date_hierarchy = "ts"


@admin.register(UserVoicePref)
class UserVoicePrefAdmin(admin.ModelAdmin):
    list_display = ("tenant", "pillar", "topic", "register_offset", "tone", "volume")
    list_filter = ("pillar", "tone", "volume")
