from django.urls import include, path
from rest_framework.routers import DefaultRouter

from .views import AgentConfigViewSet, TenantViewSet

router = DefaultRouter()
router.register("tenants", TenantViewSet, basename="tenant")
router.register("agent-config", AgentConfigViewSet, basename="agent-config")

urlpatterns = [
    path("", include(router.urls)),
]
