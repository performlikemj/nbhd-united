from django.contrib import admin
from django.urls import include, path

urlpatterns = [
    path("admin/", admin.site.urls),
    path("api/v1/tenants/", include("apps.tenants.urls")),
    path("api/v1/billing/", include("apps.billing.urls")),
    path("api/v1/integrations/", include("apps.integrations.urls")),
    path("api/v1/dashboard/", include("apps.dashboard.urls")),
    path("api/v1/telegram/", include("apps.router.urls")),
    path("stripe/", include("djstripe.urls", namespace="djstripe")),
]
