from django.urls import path

from .views import (
    CommandClaimView,
    CommandResultView,
    CommandStartView,
    GatewayRegisterView,
    SyncCommitView,
    SyncOpenView,
    SyncPageView,
)

urlpatterns = [
    path("register/", GatewayRegisterView.as_view(), name="datebook-register"),
    path("sync/open/", SyncOpenView.as_view(), name="datebook-sync-open"),
    path("sync/page/", SyncPageView.as_view(), name="datebook-sync-page"),
    path("sync/commit/", SyncCommitView.as_view(), name="datebook-sync-commit"),
    path("commands/claim/", CommandClaimView.as_view(), name="datebook-command-claim"),
    path("commands/<uuid:command_id>/start/", CommandStartView.as_view(), name="datebook-command-start"),
    path("commands/<uuid:command_id>/result/", CommandResultView.as_view(), name="datebook-command-result"),
]
