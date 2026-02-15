from django.contrib import admin
from django.urls import include, path

urlpatterns = [
    path("admin/", admin.site.urls),
    path("api/v1/auth/", include("apps.tenants.auth_urls")),
    path("api/v1/tenants/", include("apps.tenants.urls")),
    path("api/v1/billing/", include("apps.billing.urls")),
    path("api/v1/integrations/", include("apps.integrations.urls")),
    path("api/v1/automations/", include("apps.automations.urls")),
    path("api/v1/journal/", include("apps.journal.urls")),
    path("api/v1/dashboard/", include("apps.dashboard.urls")),
    path("api/v1/telegram/", include("apps.router.urls")),
    path("api/cron/", include("apps.cron.urls")),
    path("stripe/", include("djstripe.urls", namespace="djstripe")),
]
