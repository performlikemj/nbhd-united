"""Routes for the iOS Core AI on-device model manifest. Mounted at ``/api/v1/coreai/``."""

from django.urls import path

from apps.router.coreai_views import CoreAIModelManifestView

urlpatterns = [
    path(
        "model/manifest/",
        CoreAIModelManifestView.as_view(),
        name="coreai-model-manifest",
    ),
]
