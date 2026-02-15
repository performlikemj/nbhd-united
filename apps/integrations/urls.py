from django.urls import include, path
from rest_framework.routers import DefaultRouter

from .runtime_views import (
    RuntimeCalendarEventsView,
    RuntimeCalendarFreeBusyView,
    RuntimeDailyNoteAppendView,
    RuntimeDailyNotesView,
    RuntimeGmailMessageDetailView,
    RuntimeGmailMessagesView,
    RuntimeJournalContextView,
    RuntimeJournalEntriesView,
    RuntimeUserMemoryView,
    RuntimeWeeklyReviewsView,
)
from .views import ComposioCallbackView, IntegrationViewSet, OAuthAuthorizeView, OAuthCallbackView

router = DefaultRouter()
router.register("", IntegrationViewSet, basename="integration")

urlpatterns = [
    path("authorize/<str:provider>/", OAuthAuthorizeView.as_view(), name="oauth-authorize"),
    path("callback/<str:provider>/", OAuthCallbackView.as_view(), name="oauth-callback"),
    path("composio-callback/<str:provider>/", ComposioCallbackView.as_view(), name="composio-callback"),
    path(
        "runtime/<uuid:tenant_id>/gmail/messages/",
        RuntimeGmailMessagesView.as_view(),
        name="runtime-gmail-messages",
    ),
    path(
        "runtime/<uuid:tenant_id>/gmail/messages/<str:message_id>/",
        RuntimeGmailMessageDetailView.as_view(),
        name="runtime-gmail-message-detail",
    ),
    path(
        "runtime/<uuid:tenant_id>/google-calendar/events/",
        RuntimeCalendarEventsView.as_view(),
        name="runtime-google-calendar-events",
    ),
    path(
        "runtime/<uuid:tenant_id>/google-calendar/freebusy/",
        RuntimeCalendarFreeBusyView.as_view(),
        name="runtime-google-calendar-freebusy",
    ),
    path(
        "runtime/<uuid:tenant_id>/journal-entries/",
        RuntimeJournalEntriesView.as_view(),
        name="runtime-journal-entries",
    ),
    path(
        "runtime/<uuid:tenant_id>/weekly-reviews/",
        RuntimeWeeklyReviewsView.as_view(),
        name="runtime-weekly-reviews",
    ),
    path(
        "runtime/<uuid:tenant_id>/daily-note/",
        RuntimeDailyNotesView.as_view(),
        name="runtime-daily-note",
    ),
    path(
        "runtime/<uuid:tenant_id>/daily-note/append/",
        RuntimeDailyNoteAppendView.as_view(),
        name="runtime-daily-note-append",
    ),
    path(
        "runtime/<uuid:tenant_id>/long-term-memory/",
        RuntimeUserMemoryView.as_view(),
        name="runtime-long-term-memory",
    ),
    path(
        "runtime/<uuid:tenant_id>/journal-context/",
        RuntimeJournalContextView.as_view(),
        name="runtime-journal-context",
    ),
    path("", include(router.urls)),
]
