from django.urls import include, path
from rest_framework.routers import DefaultRouter

from .runtime_views import (
    RuntimeCalendarEventsView,
    RuntimeCalendarFreeBusyView,
    RuntimeGmailMessageDetailView,
    RuntimeGmailMessagesView,
)
from .views import IntegrationViewSet, OAuthAuthorizeView, OAuthCallbackView

router = DefaultRouter()
router.register("", IntegrationViewSet, basename="integration")

urlpatterns = [
    path("authorize/<str:provider>/", OAuthAuthorizeView.as_view(), name="oauth-authorize"),
    path("callback/<str:provider>/", OAuthCallbackView.as_view(), name="oauth-callback"),
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
    path("", include(router.urls)),
]
