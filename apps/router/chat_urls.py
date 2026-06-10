"""URL routes for the rich-client (iOS/web) chat ingress.

Mounted at ``/api/v1/chat/`` (see ``config/urls.py``).
"""

from django.urls import path

from apps.router.chat_views import (
    ChatContextView,
    ChatLocalTurnView,
    ChatMessageDetailView,
    ChatMessageView,
    ChatThreadListView,
    ChatThreadMessagesView,
)

urlpatterns = [
    path("messages/", ChatMessageView.as_view(), name="chat-message-create"),
    path("context/", ChatContextView.as_view(), name="chat-context"),
    path("turns/", ChatLocalTurnView.as_view(), name="chat-local-turn"),
    path(
        "messages/<str:client_msg_id>/",
        ChatMessageDetailView.as_view(),
        name="chat-message-detail",
    ),
    path("threads/", ChatThreadListView.as_view(), name="chat-thread-list"),
    path(
        "threads/<uuid:thread_id>/messages/",
        ChatThreadMessagesView.as_view(),
        name="chat-thread-messages",
    ),
]
