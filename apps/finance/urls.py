from django.urls import path

from .views import (
    FinanceAccountDetailView,
    FinanceAccountListView,
    FinanceDashboardView,
    FinanceSettingsView,
    FinanceSnapshotListView,
    FinanceTransactionListView,
    PayoffPlanListView,
)
from .runtime_views import (
    RuntimeFinanceAccountsView,
    RuntimeFinanceArchiveAccountView,
    RuntimeFinanceBalanceUpdateView,
    RuntimeFinancePayoffView,
    RuntimeFinanceSummaryView,
    RuntimeFinanceTransactionsView,
    RuntimeFinanceUnarchiveAccountView,
)

urlpatterns = [
    # Consumer-facing (frontend, JWT auth)
    path("settings/", FinanceSettingsView.as_view(), name="finance-settings"),
    path("dashboard/", FinanceDashboardView.as_view(), name="finance-dashboard"),
    path("accounts/", FinanceAccountListView.as_view(), name="finance-accounts"),
    path(
        "accounts/<uuid:account_id>/",
        FinanceAccountDetailView.as_view(),
        name="finance-account-detail",
    ),
    path("transactions/", FinanceTransactionListView.as_view(), name="finance-transactions"),
    path("payoff-plans/", PayoffPlanListView.as_view(), name="finance-payoff-plans"),
    path("snapshots/", FinanceSnapshotListView.as_view(), name="finance-snapshots"),
    # Runtime (OpenClaw plugin, internal auth)
    path(
        "runtime/<uuid:tenant_id>/accounts/",
        RuntimeFinanceAccountsView.as_view(),
        name="runtime-finance-accounts",
    ),
    path(
        "runtime/<uuid:tenant_id>/accounts/archive/",
        RuntimeFinanceArchiveAccountView.as_view(),
        name="runtime-finance-archive-account",
    ),
    path(
        "runtime/<uuid:tenant_id>/accounts/unarchive/",
        RuntimeFinanceUnarchiveAccountView.as_view(),
        name="runtime-finance-unarchive-account",
    ),
    path(
        "runtime/<uuid:tenant_id>/transactions/",
        RuntimeFinanceTransactionsView.as_view(),
        name="runtime-finance-transactions",
    ),
    path(
        "runtime/<uuid:tenant_id>/balance/",
        RuntimeFinanceBalanceUpdateView.as_view(),
        name="runtime-finance-balance",
    ),
    path(
        "runtime/<uuid:tenant_id>/payoff/calculate/",
        RuntimeFinancePayoffView.as_view(),
        name="runtime-finance-payoff",
    ),
    path(
        "runtime/<uuid:tenant_id>/summary/",
        RuntimeFinanceSummaryView.as_view(),
        name="runtime-finance-summary",
    ),
]
