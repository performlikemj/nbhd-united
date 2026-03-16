from django.urls import path

from .views import DashboardView, HorizonsView, UsageHistoryView

urlpatterns = [
    path("", DashboardView.as_view(), name="dashboard"),
    path("usage/", UsageHistoryView.as_view(), name="usage-history"),
    path("horizons/", HorizonsView.as_view(), name="horizons"),
]
