"""Post-save / post-delete signals for finance models.

Each save or delete that affects what ``envelope_finance_state`` reports
triggers a debounced refresh of ``workspace/USER.md`` so the agent's
pre-loaded snapshot stays current. The leading-edge debounce in
``push_user_md`` collapses bursts (e.g. multi-account edits) into one
file-share write.
"""

from __future__ import annotations

import logging
import threading

from django.db import transaction
from django.db.models.signals import post_delete, post_save
from django.dispatch import receiver

from apps.finance.models import FinanceAccount, FinanceTransaction, PayoffPlan

logger = logging.getLogger(__name__)


def _push(tenant_id: str) -> None:
    from apps.orchestrator.workspace_envelope import push_user_md

    try:
        push_user_md(tenant_id)
    except Exception:
        logger.warning(
            "USER.md refresh after finance save failed for tenant %s",
            str(tenant_id)[:8],
            exc_info=True,
        )


def _enqueue(tenant_id: str) -> None:
    transaction.on_commit(lambda: threading.Thread(target=_push, args=(tenant_id,), daemon=True).start())


@receiver(post_save, sender=FinanceAccount)
@receiver(post_delete, sender=FinanceAccount)
def refresh_user_md_on_account_change(sender, instance, **kwargs):
    _enqueue(str(instance.tenant_id))


@receiver(post_save, sender=FinanceTransaction)
@receiver(post_delete, sender=FinanceTransaction)
def refresh_user_md_on_transaction_change(sender, instance, **kwargs):
    _enqueue(str(instance.tenant_id))


@receiver(post_save, sender=PayoffPlan)
@receiver(post_delete, sender=PayoffPlan)
def refresh_user_md_on_payoff_plan_change(sender, instance, **kwargs):
    _enqueue(str(instance.tenant_id))
