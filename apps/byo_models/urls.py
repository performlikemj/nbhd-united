"""URL patterns for BYO credential endpoints.

Mounted at `/api/v1/tenants/byo-credentials/` from `config/urls.py`.
"""

from django.urls import path

from apps.byo_models.views import BYOCredentialDetailView, BYOCredentialListView

urlpatterns = [
    path("", BYOCredentialListView.as_view(), name="byo-credentials-list"),
    path("<uuid:cred_id>/", BYOCredentialDetailView.as_view(), name="byo-credentials-detail"),
]
