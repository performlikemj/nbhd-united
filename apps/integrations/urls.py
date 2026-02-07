from django.urls import include, path
from rest_framework.routers import DefaultRouter

from .views import IntegrationViewSet

router = DefaultRouter()
router.register("", IntegrationViewSet, basename="integration")

urlpatterns = [
    path("", include(router.urls)),
]
