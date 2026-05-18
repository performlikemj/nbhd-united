"""Sweep duplicate FinanceTransaction rows + reverse their balance impact.

Background: until the dedup guard landed in `RuntimeFinanceTransactionsView`,
agents calling `nbhd_finance_record_payment` blindly created a new row every
time, even when the same (account, type, amount, date) was already on file.
Silent 20s timeouts during the May 2026 window meant the agent retried
multiple times and the user re-prompted across days, so each real payment was
recorded 2-4 times. The view also subtracted the amount from `current_balance`
on every insert, so duplicates compounded the balance drift.

This command is idempotent: it groups rows by
`(tenant, account, transaction_type, amount, date)`, keeps the oldest row in
each cluster as canonical, deletes the rest, and reverses each deleted row's
balance impact (clamped at `original_balance` for debt accounts so the
restoration never overshoots the starting line).

Default behaviour is dry-run. Pass `--apply` to commit.
"""

from __future__ import annotations

from collections import defaultdict
from decimal import Decimal
from uuid import UUID

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction as db_transaction
from django.db.models import Count

from apps.finance.models import FinanceAccount, FinanceTransaction

ZERO = Decimal("0")


class Command(BaseCommand):
    help = "Delete duplicate FinanceTransaction rows and reverse their balance impact."

    def add_arguments(self, parser):
        parser.add_argument(
            "--tenant",
            dest="tenant_id",
            default=None,
            help="Restrict to a single tenant UUID. Omit to sweep the whole fleet.",
        )
        parser.add_argument(
            "--apply",
            action="store_true",
            help="Actually delete + adjust balances. Default is dry-run.",
        )
        parser.add_argument(
            "--verbose",
            action="store_true",
            help="Print every duplicate row that would be deleted.",
        )

    def handle(self, *args, **options):
        tenant_filter = options.get("tenant_id")
        apply_changes = options.get("apply", False)
        verbose = options.get("verbose", False)

        if tenant_filter:
            try:
                tenant_uuid = UUID(tenant_filter)
            except (TypeError, ValueError) as exc:
                raise CommandError(f"--tenant must be a valid UUID: {exc}") from exc
        else:
            tenant_uuid = None

        mode = "APPLY" if apply_changes else "DRY-RUN"
        scope = f"tenant={tenant_uuid}" if tenant_uuid else "fleet-wide"
        self.stdout.write(self.style.NOTICE(f"[{mode}] dedupe_finance_transactions ({scope})"))

        # Find duplicate clusters
        cluster_qs = (
            FinanceTransaction.objects.values("tenant_id", "account_id", "transaction_type", "amount", "date")
            .annotate(cnt=Count("id"))
            .filter(cnt__gt=1)
        )
        if tenant_uuid:
            cluster_qs = cluster_qs.filter(tenant_id=tenant_uuid)

        clusters = list(cluster_qs)
        if not clusters:
            self.stdout.write(self.style.SUCCESS("No duplicate clusters found."))
            return

        total_clusters = len(clusters)
        total_excess = sum(c["cnt"] - 1 for c in clusters)
        # Per-account balance adjustments: account_id -> Decimal delta (positive = restore balance)
        balance_adjustments: dict[str, Decimal] = defaultdict(lambda: ZERO)
        # Rows to delete
        to_delete_ids: list = []
        # Per-tenant counters for the summary
        per_tenant_clusters: dict[str, int] = defaultdict(int)
        per_tenant_excess: dict[str, int] = defaultdict(int)

        for cluster in clusters:
            rows = list(
                FinanceTransaction.objects.filter(
                    tenant_id=cluster["tenant_id"],
                    account_id=cluster["account_id"],
                    transaction_type=cluster["transaction_type"],
                    amount=cluster["amount"],
                    date=cluster["date"],
                ).order_by("created_at", "id")
            )
            if len(rows) < 2:
                continue
            canonical, duplicates = rows[0], rows[1:]
            per_tenant_clusters[str(cluster["tenant_id"])] += 1
            per_tenant_excess[str(cluster["tenant_id"])] += len(duplicates)

            for dup in duplicates:
                to_delete_ids.append(dup.id)
                # Reverse the balance impact: payments/refunds reduced balance, so add back
                if dup.transaction_type in ("payment", "refund"):
                    balance_adjustments[str(cluster["account_id"])] += dup.amount
                elif dup.transaction_type in ("charge", "interest"):
                    balance_adjustments[str(cluster["account_id"])] -= dup.amount

            if verbose:
                self.stdout.write(
                    f"  cluster t={str(cluster['tenant_id'])[:8]} acct={str(cluster['account_id'])[:8]} "
                    f"{cluster['transaction_type']} ${cluster['amount']} on {cluster['date']}: "
                    f"keep {canonical.id} (created {canonical.created_at.isoformat()}), "
                    f"drop {len(duplicates)} ({', '.join(str(d.id)[:8] for d in duplicates)})"
                )

        # Compute new balances with clamping
        account_ids = list(balance_adjustments.keys())
        accounts = {str(a.id): a for a in FinanceAccount.objects.filter(id__in=account_ids)}
        balance_plan: dict[str, dict] = {}
        for acct_id, delta in balance_adjustments.items():
            acct = accounts.get(acct_id)
            if acct is None:
                continue
            proposed = acct.current_balance + delta
            clamped_reason = None
            # Cap at original_balance for debt accounts so restoration never
            # overshoots the starting line (handles legacy max(0, ...) clamps)
            if acct.is_debt and acct.original_balance is not None and proposed > acct.original_balance:
                proposed = acct.original_balance
                clamped_reason = "capped at original_balance"
            if proposed < ZERO:
                proposed = ZERO
                clamped_reason = "floored at 0"
            balance_plan[acct_id] = {
                "account": acct,
                "delta": delta,
                "current": acct.current_balance,
                "proposed": proposed,
                "clamped": clamped_reason,
            }

        # Report
        self.stdout.write("")
        self.stdout.write(self.style.NOTICE(f"Found {total_clusters} duplicate clusters, {total_excess} excess rows."))
        self.stdout.write("Per-tenant breakdown:")
        for tid, clusters_n in sorted(per_tenant_clusters.items(), key=lambda kv: -per_tenant_excess[kv[0]]):
            self.stdout.write(f"  {tid}: clusters={clusters_n} excess_rows={per_tenant_excess[tid]}")

        self.stdout.write("")
        self.stdout.write("Per-account balance corrections:")
        for acct_id, plan in sorted(balance_plan.items(), key=lambda kv: -abs(kv[1]["delta"])):
            acct = plan["account"]
            note = f"  [{plan['clamped']}]" if plan["clamped"] else ""
            self.stdout.write(
                f"  {acct.nickname[:30]:30s} ({str(acct.tenant_id)[:8]}) "
                f"current=${plan['current']:>12} delta=${plan['delta']:>+12} "
                f"-> ${plan['proposed']:>12}{note}"
            )

        if not apply_changes:
            self.stdout.write("")
            self.stdout.write(self.style.WARNING("DRY-RUN: nothing was changed. Re-run with --apply to commit."))
            return

        # APPLY
        self.stdout.write("")
        self.stdout.write(self.style.NOTICE("Applying changes…"))
        with db_transaction.atomic():
            deleted, _ = FinanceTransaction.objects.filter(id__in=to_delete_ids).delete()
            updated = 0
            for plan in balance_plan.values():
                acct = plan["account"]
                acct.current_balance = plan["proposed"]
                acct.save(update_fields=["current_balance", "updated_at"])
                updated += 1
        self.stdout.write(
            self.style.SUCCESS(f"Done. Deleted {deleted} duplicate rows, adjusted {updated} account balances.")
        )
