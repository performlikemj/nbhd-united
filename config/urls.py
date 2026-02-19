from django.contrib import admin
from django.urls import include, path

from apps.integrations.runtime_views import RuntimeUsageReportView

urlpatterns = [
    path("admin/", admin.site.urls),
    path("api/v1/auth/", include("apps.tenants.auth_urls")),
    path("api/v1/tenants/", include("apps.tenants.urls")),
    path("api/v1/billing/", include("apps.billing.urls")),
    path("api/v1/integrations/", include("apps.integrations.urls")),
    path("api/v1/automations/", include("apps.automations.urls")),
    path("api/v1/journal/", include("apps.journal.urls")),
    path("api/v1/dashboard/", include("apps.dashboard.urls")),
    path(
        "api/v1/internal/runtime/<uuid:tenant_id>/usage/report/",
        RuntimeUsageReportView.as_view(),
        name="runtime-usage-report-internal",
    ),
    path("api/v1/telegram/", include("apps.router.urls")),
    path("api/v1/cron-jobs/", include("apps.cron.tenant_urls")),
    path("api/cron/", include("apps.cron.urls")),
    path("api/v1/cron/", include("apps.cron.urls")),
    path("stripe/", include("djstripe.urls", namespace="djstripe")),
]
