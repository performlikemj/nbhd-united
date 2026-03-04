from django.urls import path

from .line_webhook import LineWebhookView

urlpatterns = [
    path("webhook/", LineWebhookView.as_view(), name="line-webhook"),
]
