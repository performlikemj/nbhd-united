from django.urls import include, path
from rest_framework.routers import DefaultRouter

from .views import OnboardTenantView, TenantViewSet

router = DefaultRouter()
router.register("", TenantViewSet, basename="tenant")

urlpatterns = [
    path("onboard/", OnboardTenantView.as_view(), name="tenant-onboard"),
    path("", include(router.urls)),
]
