from django.urls import include, path
from rest_framework.routers import DefaultRouter

from .runtime_views import (
    RuntimeCalendarEventsView,
    RuntimeCalendarFreeBusyView,
    RuntimeDailyNoteAppendView,
    RuntimeDailyNotesView,
    RuntimeDocumentAppendView,
    RuntimeDocumentView,
    RuntimeGmailMessageDetailView,
    RuntimeGmailMessagesView,
    RuntimeJournalContextView,
    RuntimeLessonCreateView,
    RuntimeLessonPendingView,
    RuntimeLessonSearchView,
    RuntimeJournalEntriesView,
    RuntimeJournalSearchView,
    RuntimeMemorySyncView,
    RuntimeUserMemoryView,
    RuntimeWeeklyReviewsView,
    RuntimeUsageReportView,
)
from .views import ComposioCallbackView, IntegrationViewSet, OAuthAuthorizeView, OAuthCallbackView
from apps.platform_logs.views import PlatformIssueReportView as _PlatformIssueReportView

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
    # Lessons runtime endpoints
    path(
        "runtime/<uuid:tenant_id>/lessons/",
        RuntimeLessonCreateView.as_view(),
        name="runtime-lessons",
    ),
    # Lessons search and queue endpoints (runtime clients)
    path(
        "runtime/<uuid:tenant_id>/lessons/search/",
        RuntimeLessonSearchView.as_view(),
        name="runtime-lessons-search",
    ),
    path(
        "runtime/<uuid:tenant_id>/lessons/pending/",
        RuntimeLessonPendingView.as_view(),
        name="runtime-lessons-pending",
    ),
    # Journal search
    path(
        "runtime/<uuid:tenant_id>/journal/search/",
        RuntimeJournalSearchView.as_view(),
        name="runtime-journal-search",
    ),
    # v2 Document endpoints
    path(
        "runtime/<uuid:tenant_id>/document/",
        RuntimeDocumentView.as_view(),
        name="runtime-document",
    ),
    path(
        "runtime/<uuid:tenant_id>/document/append/",
        RuntimeDocumentAppendView.as_view(),
        name="runtime-document-append",
    ),
    # Memory sync — bulk export documents as workspace files
    path(
        "runtime/<uuid:tenant_id>/memory-sync/",
        RuntimeMemorySyncView.as_view(),
        name="runtime-memory-sync",
    ),
    # Usage reporting for polling-mode runtime turns
    path(
        "runtime/<uuid:tenant_id>/usage/report/",
        RuntimeUsageReportView.as_view(),
        name="runtime-usage-report",
    ),
    # Platform issue logging
    path(
        "runtime/<uuid:tenant_id>/platform-issue/report/",
        _PlatformIssueReportView.as_view(),
        name="runtime-platform-issue-report",
    ),
    path("", include(router.urls)),
]
