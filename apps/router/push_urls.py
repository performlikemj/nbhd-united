"""APNs device-token routes. Mounted at ``/api/v1/push/`` (see config/urls.py)."""

from django.urls import path

from apps.router.push_views import PushRegisterView, PushTestView

urlpatterns = [
    path("register/", PushRegisterView.as_view(), name="push-register"),
    path("test/", PushTestView.as_view(), name="push-test"),
]
