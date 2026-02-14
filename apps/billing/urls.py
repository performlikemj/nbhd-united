from django.urls import path

from .usage_views import DailyUsageView, TransparencyView, UsageSummaryView
from .views import StripeCheckoutView, StripePortalView, stripe_webhook

urlpatterns = [
    path("webhook/", stripe_webhook, name="stripe-webhook"),
    path("portal/", StripePortalView.as_view(), name="stripe-portal"),
    path("checkout/", StripeCheckoutView.as_view(), name="stripe-checkout"),
    # Usage dashboard
    path("usage/summary/", UsageSummaryView.as_view(), name="usage-summary"),
    path("usage/daily/", DailyUsageView.as_view(), name="usage-daily"),
    path("usage/transparency/", TransparencyView.as_view(), name="usage-transparency"),
]
