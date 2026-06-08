from django.urls import path

from .usage_views import DailyUsageView, DonationPreferenceView, TransparencyView, UsageSummaryView
from .views import (
    CreditBalanceView,
    CreditCheckoutView,
    StripeCheckoutView,
    StripePortalView,
    stripe_webhook,
)

urlpatterns = [
    path("webhook/", stripe_webhook, name="stripe-webhook"),
    path("portal/", StripePortalView.as_view(), name="stripe-portal"),
    path("checkout/", StripeCheckoutView.as_view(), name="stripe-checkout"),
    # Prepaid credit top-ups
    path("credits/", CreditBalanceView.as_view(), name="credits-balance"),
    path("credits/checkout/", CreditCheckoutView.as_view(), name="credits-checkout"),
    # Usage dashboard
    path("usage/summary/", UsageSummaryView.as_view(), name="usage-summary"),
    path("usage/daily/", DailyUsageView.as_view(), name="usage-daily"),
    path("usage/transparency/", TransparencyView.as_view(), name="usage-transparency"),
    path("donation-preference/", DonationPreferenceView.as_view(), name="donation-preference"),
]
