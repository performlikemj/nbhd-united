from django.urls import include, path
from rest_framework.routers import DefaultRouter

from .views import OnboardTenantView, TenantViewSet
from .telegram_views import telegram_generate_link, telegram_status, telegram_unlink

router = DefaultRouter()
router.register("", TenantViewSet, basename="tenant")

urlpatterns = [
    path("onboard/", OnboardTenantView.as_view(), name="tenant-onboard"),
    path("telegram/generate-link/", telegram_generate_link, name="telegram-generate-link"),
    path("telegram/unlink/", telegram_unlink, name="telegram-unlink"),
    path("telegram/status/", telegram_status, name="telegram-status"),
    path("", include(router.urls)),
]
