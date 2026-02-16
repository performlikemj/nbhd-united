from django.urls import path

from .views import (
    DailyNoteEntryDetailView,
    DailyNoteEntryListView,
    DailyNoteSectionView,
    DailyNoteView,
    JournalEntryDetailView,
    JournalEntryListCreateView,
    MemoryView,
    DailyNoteTemplateView,
    TemplateDetailView,
    TemplateListCreateView,
    WeeklyReviewDetailView,
    WeeklyReviewListCreateView,
)
from .document_views import (
    DocumentAppendView,
    DocumentDetailView,
    DocumentListCreateView,
    SidebarTreeView,
    TodayView,
)

urlpatterns = [
    # ── v2 Document API ──────────────────────────────────────────────────
    path("documents/", DocumentListCreateView.as_view(), name="document-list-create"),
    path("documents/<str:kind>/<path:slug>/append/", DocumentAppendView.as_view(), name="document-append"),
    path("documents/<str:kind>/<path:slug>/", DocumentDetailView.as_view(), name="document-detail"),
    path("today/", TodayView.as_view(), name="today"),
    path("tree/", SidebarTreeView.as_view(), name="sidebar-tree"),

    # ── Legacy endpoints (kept for backward compatibility) ───────────────
    path("", JournalEntryListCreateView.as_view(), name="journal-list-create"),
    path("<uuid:entry_id>/", JournalEntryDetailView.as_view(), name="journal-detail"),
    path("daily/<str:date>/", DailyNoteView.as_view(), name="daily-note"),
    path("daily/<str:date>/template/", DailyNoteTemplateView.as_view(), name="daily-note-template"),
    path("daily/<str:date>/sections/<str:slug>/", DailyNoteSectionView.as_view(), name="daily-note-section"),
    path("daily/<str:date>/entries/", DailyNoteEntryListView.as_view(), name="daily-note-entries"),
    path("daily/<str:date>/entries/<int:index>/", DailyNoteEntryDetailView.as_view(), name="daily-note-entry-detail"),
    path("memory/", MemoryView.as_view(), name="memory"),
    path("templates/", TemplateListCreateView.as_view(), name="template-list-create"),
    path("templates/<str:template_id>/", TemplateDetailView.as_view(), name="template-detail"),
    path("reviews/", WeeklyReviewListCreateView.as_view(), name="weekly-review-list-create"),
    path("reviews/<uuid:review_id>/", WeeklyReviewDetailView.as_view(), name="weekly-review-detail"),
]
