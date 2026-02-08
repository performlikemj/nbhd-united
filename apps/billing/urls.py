from django.urls import path

from .views import StripeCheckoutView, StripePortalView, stripe_webhook

urlpatterns = [
    path("webhook/", stripe_webhook, name="stripe-webhook"),
    path("portal/", StripePortalView.as_view(), name="stripe-portal"),
    path("checkout/", StripeCheckoutView.as_view(), name="stripe-checkout"),
]
