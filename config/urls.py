from django.contrib import admin
from django.urls import include, path

from apps.integrations.runtime_views import RuntimeBYOErrorReportView, RuntimeUsageReportView
from apps.router.chat_views import ChatProgressEventView
from apps.router.views import serve_chart_image, serve_meditation_audio

urlpatterns = [
    path("admin/", admin.site.urls),
    # Chart images — unauthenticated, served for LINE image messages
    path("api/v1/charts/<uuid:tenant_id>/<str:filename>", serve_chart_image, name="serve-chart-image"),
    # Meditation audio (Core pillar) — unauthenticated, unguessable UUID filename
    path(
        "api/v1/meditations/<uuid:tenant_id>/<str:filename>",
        serve_meditation_audio,
        name="serve-meditation-audio",
    ),
    path("api/v1/auth/", include("apps.tenants.auth_urls")),
    # `byo-credentials/` MUST come before `tenants/` — the latter's
    # DefaultRouter has a catch-all `<pk>/` route that would otherwise
    # interpret `byo-credentials` as a tenant PK lookup.
    path("api/v1/tenants/byo-credentials/", include("apps.byo_models.urls")),
    path("api/v1/tenants/", include("apps.tenants.urls")),
    path("api/v1/billing/", include("apps.billing.urls")),
    path("api/v1/integrations/", include("apps.integrations.urls")),
    path("api/v1/automations/", include("apps.automations.urls")),
    path("api/v1/journal/", include("apps.journal.urls")),
    path("api/v1/lessons/", include("apps.lessons.urls")),
    path("api/v1/dashboard/", include("apps.dashboard.urls")),
    path("api/v1/finance/", include("apps.finance.urls")),
    path("api/v1/fuel/", include("apps.fuel.urls")),
    path("api/v1/core/", include("apps.core.urls")),
    path("api/v1/insights/", include("apps.insights.urls")),
    path(
        "api/v1/internal/runtime/<uuid:tenant_id>/usage/report/",
        RuntimeUsageReportView.as_view(),
        name="runtime-usage-report-internal",
    ),
    # BYO provider error reporting — container→Django. Plugin POSTs here
    # when a billing/auth error fires on a BYO route so the AI Provider
    # page surfaces the real cause to the user.
    path(
        "api/v1/internal/runtime/<uuid:tenant_id>/byo/error/",
        RuntimeBYOErrorReportView.as_view(),
        name="runtime-byo-error-report-internal",
    ),
    # Agent activity stream — container→Django. The runtime's tool-call hooks
    # POST progress (waking/thinking/tool/composing) for an in-flight turn so
    # polling clients can narrate it (and the iOS-27 Live Activity can show it).
    path(
        "api/v1/internal/runtime/<uuid:tenant_id>/chat/progress/",
        ChatProgressEventView.as_view(),
        name="chat-progress-event-internal",
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
    path("api/v1/chat/", include("apps.router.chat_urls")),
    path("api/v1/siri/", include("apps.router.siri_urls")),
    path("api/v1/push/", include("apps.router.push_urls")),
    path("api/v1/coreai/", include("apps.router.coreai_urls")),
    path("api/v1/cron-jobs/", include("apps.cron.tenant_urls")),
    path("api/v1/workspaces/", include("apps.journal.workspace_urls")),
    path("api/v1/sessions/", include("apps.journal.session_urls")),
    path("api/cron/", include("apps.cron.urls")),
    path("api/v1/cron/", include("apps.cron.urls")),
    path("stripe/", include("djstripe.urls", namespace="djstripe")),
]
