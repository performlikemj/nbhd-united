from django.urls import path

from .views import (
    DailyNoteEntryDetailView,
    DailyNoteEntryListView,
    DailyNoteView,
    JournalEntryDetailView,
    JournalEntryListCreateView,
    MemoryView,
    WeeklyReviewDetailView,
    WeeklyReviewListCreateView,
)

urlpatterns = [
    # Legacy JournalEntry endpoints
    path("", JournalEntryListCreateView.as_view(), name="journal-list-create"),
    path("<uuid:entry_id>/", JournalEntryDetailView.as_view(), name="journal-detail"),
    # Daily notes (markdown-first)
    path("daily/<str:date>/", DailyNoteView.as_view(), name="daily-note"),
    path("daily/<str:date>/entries/", DailyNoteEntryListView.as_view(), name="daily-note-entries"),
    path("daily/<str:date>/entries/<int:index>/", DailyNoteEntryDetailView.as_view(), name="daily-note-entry-detail"),
    # Long-term memory
    path("memory/", MemoryView.as_view(), name="memory"),
    # Weekly reviews
    path("reviews/", WeeklyReviewListCreateView.as_view(), name="weekly-review-list-create"),
    path("reviews/<uuid:review_id>/", WeeklyReviewDetailView.as_view(), name="weekly-review-detail"),
]
