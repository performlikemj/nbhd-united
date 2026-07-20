from django.urls import path

from .yardtalk_views import EntitlementView, LicenseValidateView

urlpatterns = [
    path("licenses/validate/", LicenseValidateView.as_view(), name="yardtalk-license-validate"),
    path("entitlement/", EntitlementView.as_view(), name="yardtalk-entitlement"),
]
