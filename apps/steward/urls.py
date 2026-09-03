from django.urls import path

from apps.steward import views

app_name = "steward"

urlpatterns = [
    path("facts/", views.StewardFactsView.as_view(), name="facts"),
    path("heartbeat/", views.heartbeat, name="heartbeat"),
    path("evidence/", views.evidence, name="evidence"),
]
