"""URLs for gate respond endpoint (internal, from button callbacks)."""
from django.urls import path

from .views import GateRespondView

urlpatterns = [
    path("<int:action_id>/respond/", GateRespondView.as_view(), name="gate-respond"),
]
