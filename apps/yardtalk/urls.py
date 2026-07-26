from django.urls import path

from .views import EntitlementView, LicenseClaimView, LicenseValidateView

urlpatterns = [
    path("licenses/validate/", LicenseValidateView.as_view(), name="yardtalk-license-validate"),
    path("licenses/claim/", LicenseClaimView.as_view(), name="yardtalk-license-claim"),
    path("entitlement/", EntitlementView.as_view(), name="yardtalk-entitlement"),
]
