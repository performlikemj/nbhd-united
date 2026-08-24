from django.urls import include, path

from .views import (
    CalendarContextsView,
    CommandClaimView,
    CommandResultView,
    CommandStartView,
    GatewayRegisterView,
    PendingGateActionsView,
    RespondGateActionView,
    SyncCommitView,
    SyncOpenView,
    SyncPageView,
)

urlpatterns = [
    path("runtime/<uuid:tenant_id>/datebook/", include("apps.datebook.runtime_urls")),
    path("register/", GatewayRegisterView.as_view(), name="datebook-register"),
    path("calendars/", CalendarContextsView.as_view(), name="datebook-calendars"),
    path("sync/open/", SyncOpenView.as_view(), name="datebook-sync-open"),
    path("sync/page/", SyncPageView.as_view(), name="datebook-sync-page"),
    path("sync/commit/", SyncCommitView.as_view(), name="datebook-sync-commit"),
    path("gate/pending/", PendingGateActionsView.as_view(), name="datebook-gate-pending"),
    path(
        "gate/<int:action_id>/respond/",
        RespondGateActionView.as_view(),
        name="datebook-gate-respond",
    ),
    path("commands/claim/", CommandClaimView.as_view(), name="datebook-command-claim"),
    path("commands/<uuid:command_id>/start/", CommandStartView.as_view(), name="datebook-command-start"),
    path("commands/<uuid:command_id>/result/", CommandResultView.as_view(), name="datebook-command-result"),
]
