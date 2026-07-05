"""Neighborhood console URLs — mounted at /api/v1/friends/ (config/urls.py)."""

from django.urls import path

from .views import (
    InviteClaimView,
    InviteCreateView,
    InviteDetailView,
    NeighborhoodView,
    ProfileView,
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
    path("invites/", InviteCreateView.as_view(), name="friends-invite-create"),
    path("invites/<str:token>/claim/", InviteClaimView.as_view(), name="friends-invite-claim"),
    path("invites/<str:token>/", InviteDetailView.as_view(), name="friends-invite-detail"),
    path("<uuid:friendship_id>/", UnfriendView.as_view(), name="friends-unfriend"),
]
