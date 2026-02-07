from django.urls import include, path
from rest_framework.routers import DefaultRouter

from .views import AgentSessionViewSet, MemoryItemViewSet, MessageViewSet

router = DefaultRouter()
router.register("sessions", AgentSessionViewSet, basename="session")
router.register("messages", MessageViewSet, basename="message")
router.register("memory", MemoryItemViewSet, basename="memory")

urlpatterns = [
    path("", include(router.urls)),
]
