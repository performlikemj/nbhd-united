from django.urls import include, path
from rest_framework.routers import DefaultRouter

from .views import IntegrationViewSet, OAuthAuthorizeView, OAuthCallbackView

router = DefaultRouter()
router.register("", IntegrationViewSet, basename="integration")

urlpatterns = [
    path("authorize/<str:provider>/", OAuthAuthorizeView.as_view(), name="oauth-authorize"),
    path("callback/<str:provider>/", OAuthCallbackView.as_view(), name="oauth-callback"),
    path("", include(router.urls)),
]
