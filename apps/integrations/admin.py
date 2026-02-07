from django.contrib import admin

from .models import Integration, UserSecret


@admin.register(Integration)
class IntegrationAdmin(admin.ModelAdmin):
    list_display = ("provider", "tenant", "status", "provider_email", "updated_at")
    list_filter = ("provider", "status")


@admin.register(UserSecret)
class UserSecretAdmin(admin.ModelAdmin):
    list_display = ("name", "tenant", "hint", "updated_at")
