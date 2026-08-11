from django.urls import path

from .runtime_views import RuntimeAgendaView, RuntimeCommandStatusView, RuntimeRequestCreateView

urlpatterns = [
    path("agenda", RuntimeAgendaView.as_view(), name="datebook-runtime-agenda"),
    path("request-create", RuntimeRequestCreateView.as_view(), name="datebook-runtime-request-create"),
    path(
        "command-status/<uuid:command_id>",
        RuntimeCommandStatusView.as_view(),
        name="datebook-runtime-command-status",
    ),
]
