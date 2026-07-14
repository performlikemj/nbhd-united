"""APNs device-token routes. Mounted at ``/api/v1/push/`` (see config/urls.py)."""

from django.urls import path

from apps.router.push_views import PushRegisterView, PushStatusView, PushTestView

urlpatterns = [
    path("register/", PushRegisterView.as_view(), name="push-register"),
    path("status/", PushStatusView.as_view(), name="push-status"),
    path("test/", PushTestView.as_view(), name="push-test"),
]
