from django.contrib import admin
from django.urls import include, path

from apps.integrations.runtime_views import RuntimeUsageReportView
from apps.router.test_workspace_sessions import force_apply_test_tenant_config, test_workspace_session
from apps.router.views import serve_chart_image

urlpatterns = [
    path("admin/", admin.site.urls),
    # Chart images — unauthenticated, served for LINE image messages
    path("api/v1/charts/<uuid:tenant_id>/<str:filename>", serve_chart_image, name="serve-chart-image"),
    path("api/v1/auth/", include("apps.tenants.auth_urls")),
    path("api/v1/tenants/", include("apps.tenants.urls")),
    path("api/v1/billing/", include("apps.billing.urls")),
    path("api/v1/integrations/", include("apps.integrations.urls")),
    path("api/v1/automations/", include("apps.automations.urls")),
    path("api/v1/journal/", include("apps.journal.urls")),
    path("api/v1/lessons/", include("apps.lessons.urls")),
    path("api/v1/dashboard/", include("apps.dashboard.urls")),
    path("api/v1/finance/", include("apps.finance.urls")),
    path(
        "api/v1/internal/runtime/<uuid:tenant_id>/usage/report/",
        RuntimeUsageReportView.as_view(),
        name="runtime-usage-report-internal",
    ),
    # Action gating — container→Django (request + poll)
    path(
        "api/v1/internal/runtime/<uuid:tenant_id>/gate/",
        include("apps.actions.runtime_urls"),
    ),
    # Action gating — internal respond (from button callbacks)
    path(
        "api/v1/gate/",
        include("apps.actions.respond_urls"),
    ),
    path("api/v1/telegram/", include("apps.router.urls")),
    path("api/v1/line/", include("apps.router.line_urls")),
    path("api/v1/cron-jobs/", include("apps.cron.tenant_urls")),
    path("api/v1/workspaces/", include("apps.journal.workspace_urls")),
    path("api/v1/sessions/", include("apps.journal.session_urls")),
    path("api/cron/", include("apps.cron.urls")),
    path("api/v1/cron/", include("apps.cron.urls")),
    path("stripe/", include("djstripe.urls", namespace="djstripe")),
    # TODO: Remove after workspace session isolation is verified
    path("api/v1/test/workspace-sessions/", test_workspace_session, name="test-workspace-sessions"),
    path("api/v1/test/workspace-sessions/force-apply/", force_apply_test_tenant_config, name="test-force-apply"),
]
