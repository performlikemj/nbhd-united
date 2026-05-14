from django.urls import path
from rest_framework_simplejwt.views import TokenObtainPairView, TokenRefreshView

from .auth_views import (
    LogoutView,
    MeView,
    PasswordResetConfirmView,
    PasswordResetRequestView,
    SignupView,
)
from .pat_views import PATCreateView, PATListView, PATRevokeView

urlpatterns = [
    path("signup/", SignupView.as_view(), name="auth-signup"),
    path("login/", TokenObtainPairView.as_view(), name="auth-login"),
    path("refresh/", TokenRefreshView.as_view(), name="auth-refresh"),
    path("logout/", LogoutView.as_view(), name="auth-logout"),
    path("me/", MeView.as_view(), name="auth-me"),
    # Password reset
    path(
        "password-reset/request/",
        PasswordResetRequestView.as_view(),
        name="auth-password-reset-request",
    ),
    path(
        "password-reset/confirm/",
        PasswordResetConfirmView.as_view(),
        name="auth-password-reset-confirm",
    ),
    # Personal Access Tokens
    path("tokens/", PATListView.as_view(), name="pat-list"),
    path("tokens/create/", PATCreateView.as_view(), name="pat-create"),
    path("tokens/<uuid:token_id>/", PATRevokeView.as_view(), name="pat-revoke"),
]
