from django.urls import path

from .session_views import SessionCreateView, SessionDetailView, SessionListView

urlpatterns = [
    path("", SessionListView.as_view(), name="session-list"),
    path("create/", SessionCreateView.as_view(), name="session-create"),
    path("<uuid:session_id>/", SessionDetailView.as_view(), name="session-detail"),
]
