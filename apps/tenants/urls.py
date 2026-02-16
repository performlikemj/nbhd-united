from django.urls import include, path
from rest_framework.routers import DefaultRouter

from .views import OnboardTenantView, PersonaListView, ProfileView, TenantViewSet, UpdatePreferencesView
from .telegram_views import telegram_generate_link, telegram_status, telegram_unlink
from .llm_config_views import LLMConfigView

router = DefaultRouter()
router.register("", TenantViewSet, basename="tenant")

urlpatterns = [
    path("onboard/", OnboardTenantView.as_view(), name="tenant-onboard"),
    path("profile/", ProfileView.as_view(), name="user-profile"),
    path("personas/", PersonaListView.as_view(), name="persona-list"),
    path("preferences/", UpdatePreferencesView.as_view(), name="preferences"),
    path("telegram/generate-link/", telegram_generate_link, name="telegram-generate-link"),
    path("telegram/unlink/", telegram_unlink, name="telegram-unlink"),
    path("telegram/status/", telegram_status, name="telegram-status"),
    path("settings/llm-config/", LLMConfigView.as_view(), name="llm-config"),
    path("", include(router.urls)),
]
