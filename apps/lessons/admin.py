from django.contrib import admin

from .models import Lesson, LessonConnection


@admin.register(Lesson)
class LessonAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "tenant",
        "status",
        "source_type",
        "cluster_id",
        "cluster_label",
        "shared",
        "created_at",
    )
    list_filter = ("tenant", "status", "source_type", "shared")
    search_fields = ("text", "context", "source_ref", "tags")
    ordering = ("-created_at",)


@admin.register(LessonConnection)
class LessonConnectionAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "from_lesson",
        "to_lesson",
        "connection_type",
        "similarity",
        "created_at",
    )
    list_filter = ("connection_type",)
    search_fields = (
        "from_lesson__text",
        "to_lesson__text",
        "connection_type",
    )
    ordering = ("-created_at",)
