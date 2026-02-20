from django.urls import include, path
from rest_framework.routers import DefaultRouter

from .views import LessonViewSet

router = DefaultRouter()
router.register("", LessonViewSet, basename="lesson")

urlpatterns = [
    path("", include(router.urls)),
]
