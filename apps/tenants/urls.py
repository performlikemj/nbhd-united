from django.urls import include, path
from rest_framework.routers import DefaultRouter

from .line_views import line_generate_link, line_set_preferred_channel, line_status, line_unlink
from .runtime_views import (
    RuntimeAgendaEngagementView,
    RuntimeCommitmentRecordView,
    RuntimePreferredModelView,
    RuntimeWelcomeMarkView,
)
from .telegram_views import telegram_generate_link, telegram_status, telegram_unlink
from .views import (
    CancelDeletionView,
    DeleteAccountView,
    HeartbeatConfigView,
    OnboardTenantView,
    PersonaListView,
    PreferredModelView,
    ProfileView,
    ProvisioningStatusView,
    RefreshConfigView,
    RetryProvisioningView,
    TaskModelPreferencesView,
    TenantViewSet,
    UpdatePreferencesView,
)

router = DefaultRouter()
router.register("", TenantViewSet, basename="tenant")

urlpatterns = [
    path("onboard/", OnboardTenantView.as_view(), name="tenant-onboard"),
    path("profile/", ProfileView.as_view(), name="user-profile"),
    path("provisioning-status/", ProvisioningStatusView.as_view(), name="tenant-provisioning-status"),
    path("retry-provisioning/", RetryProvisioningView.as_view(), name="tenant-retry-provisioning"),
    path("personas/", PersonaListView.as_view(), name="persona-list"),
    path("preferences/", UpdatePreferencesView.as_view(), name="preferences"),
    path("refresh-config/", RefreshConfigView.as_view(), name="refresh-config"),
    path("telegram/generate-link/", telegram_generate_link, name="telegram-generate-link"),
    path("telegram/unlink/", telegram_unlink, name="telegram-unlink"),
    path("telegram/status/", telegram_status, name="telegram-status"),
    path("line/generate-link/", line_generate_link, name="line-generate-link"),
    path("line/unlink/", line_unlink, name="line-unlink"),
    path("line/status/", line_status, name="line-status"),
    path("line/preferred-channel/", line_set_preferred_channel, name="line-preferred-channel"),
    path("heartbeat/", HeartbeatConfigView.as_view(), name="heartbeat-config"),
    path("delete-account/", DeleteAccountView.as_view(), name="delete-account"),
    path("cancel-deletion/", CancelDeletionView.as_view(), name="cancel-deletion"),
    path("settings/preferred-model/", PreferredModelView.as_view(), name="preferred-model"),
    path("settings/task-model-preferences/", TaskModelPreferencesView.as_view(), name="task-model-preferences"),
    # Internal runtime endpoint for the agent to acknowledge welcome delivery.
    path(
        "runtime/<uuid:tenant_id>/welcomes/<str:feature>/",
        RuntimeWelcomeMarkView.as_view(),
        name="runtime-welcome-mark",
    ),
    # Internal runtime endpoint for recording agenda engagement events
    # (Phase B). OpenClaw plugins / extractors / explicit agent tool
    # calls all funnel through here.
    path(
        "runtime/<uuid:tenant_id>/agenda/<str:kind>/<str:item_id>/",
        RuntimeAgendaEngagementView.as_view(),
        name="runtime-agenda-engagement",
    ),
    # Phase D — record an assistant-written future-aware commitment.
    # Called by the nbhd_record_commitment plugin tool.
    path(
        "runtime/<uuid:tenant_id>/commitments/",
        RuntimeCommitmentRecordView.as_view(),
        name="runtime-commitment-record",
    ),
    # Assistant-callable primary-model read + switch. Reuses the same
    # tier gate as the consumer PreferredModelView so the assistant cannot
    # quietly upgrade itself past the tier ceiling.
    path(
        "runtime/<uuid:tenant_id>/preferred-model/",
        RuntimePreferredModelView.as_view(),
        name="runtime-preferred-model",
    ),
    path("", include(router.urls)),
]
