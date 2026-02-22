from django.urls import include, path
from rest_framework.routers import DefaultRouter

from .views import (
    OnboardTenantView,
    PersonaListView,
    ProfileView,
    ProvisioningStatusView,
    RefreshConfigView,
    RetryProvisioningView,
    TenantViewSet,
    UpdatePreferencesView,
)
from .telegram_views import telegram_generate_link, telegram_status, telegram_unlink
from .llm_config_views import FetchModelsView, LLMConfigView

router = DefaultRouter()
router.register("", TenantViewSet, basename="tenant")

urlpatterns = [
    path("onboard/", OnboardTenantView.as_view(), name="tenant-onboard"),
    path("profile/", ProfileView.as_view(), name="user-profile"),
    path("provisioning-status/", ProvisioningStatusView.as_view(), name="tenant-provisioning-status"),
    path("retry-provisioning/", RetryProvisioningView.as_view(), name="tenant-retry-provisioning"),
    path("personas/", PersonaListView.as_view(), name="persona-list"),
    path("preferences/", UpdatePreferencesView.as_view(), name="preferences"),
    path("refresh-config/", RefreshConfigView.as_view(), name="refresh-config"),
    path("telegram/generate-link/", telegram_generate_link, name="telegram-generate-link"),
    path("telegram/unlink/", telegram_unlink, name="telegram-unlink"),
    path("telegram/status/", telegram_status, name="telegram-status"),
    path("settings/llm-config/", LLMConfigView.as_view(), name="llm-config"),
    path("settings/llm-config/models/", FetchModelsView.as_view(), name="llm-config-models"),
    path("", include(router.urls)),
]
