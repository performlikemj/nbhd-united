"""Neighborhood console URLs — mounted at /api/v1/friends/ (config/urls.py)."""

from django.urls import path

from .views import (
    InviteClaimView,
    InviteCreateView,
    InviteDetailView,
    NeighborhoodView,
    PendingSharesView,
    ProfileView,
    ShareApproveView,
    SharePreviewView,
    ShareRejectView,
    UnfriendView,
    WaveCreateView,
    WaveRespondView,
)

urlpatterns = [
    path("", NeighborhoodView.as_view(), name="friends-neighborhood"),
    path("profile/", ProfileView.as_view(), name="friends-profile"),
    path("waves/", WaveCreateView.as_view(), name="friends-wave-create"),
    path(
        "waves/<uuid:friendship_id>/accept/",
        WaveRespondView.as_view(),
        {"action": "accept"},
        name="friends-wave-accept",
    ),
    path(
        "waves/<uuid:friendship_id>/decline/",
        WaveRespondView.as_view(),
        {"action": "decline"},
        name="friends-wave-decline",
    ),
    path(
        "waves/<uuid:friendship_id>/block/", WaveRespondView.as_view(), {"action": "block"}, name="friends-wave-block"
    ),
    path("shares/preview/", SharePreviewView.as_view(), name="friends-share-preview"),
    path("shares/pending/", PendingSharesView.as_view(), name="friends-shares-pending"),
    path("shares/<uuid:pending_share_id>/approve/", ShareApproveView.as_view(), name="friends-share-approve"),
    path("shares/<uuid:pending_share_id>/reject/", ShareRejectView.as_view(), name="friends-share-reject"),
    path("invites/", InviteCreateView.as_view(), name="friends-invite-create"),
    path("invites/<str:token>/claim/", InviteClaimView.as_view(), name="friends-invite-claim"),
    path("invites/<str:token>/", InviteDetailView.as_view(), name="friends-invite-detail"),
    path("<uuid:friendship_id>/", UnfriendView.as_view(), name="friends-unfriend"),
]
