from django.urls import path

from .views import GateRequestView, GatePollView, GateRespondView

urlpatterns = [
    path(
        "request/",
        GateRequestView.as_view(),
        name="gate-request",
    ),
    path(
        "<int:action_id>/poll/",
        GatePollView.as_view(),
        name="gate-poll",
    ),
    path(
        "<int:action_id>/respond/",
        GateRespondView.as_view(),
        name="gate-respond",
    ),
]
