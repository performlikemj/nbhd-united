from django.contrib import admin

from .models import Integration


@admin.register(Integration)
class IntegrationAdmin(admin.ModelAdmin):
    list_display = ("provider", "tenant", "status", "provider_email", "connected_at")
    list_filter = ("provider", "status")
