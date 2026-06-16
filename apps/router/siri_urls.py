"""URL routes for the Siri tiered-responder endpoints.

Mounted at ``/api/v1/siri/`` (see ``config/urls.py``). JWT-authed, app-target
intent code reuses the existing user JWT (no dedicated Siri scope).
"""

from django.urls import path

from apps.router.siri_views import SiriQuickStatusView, SiriRespondView

urlpatterns = [
    path("status/", SiriQuickStatusView.as_view(), name="siri-status"),
    path("respond/", SiriRespondView.as_view(), name="siri-respond"),
]
