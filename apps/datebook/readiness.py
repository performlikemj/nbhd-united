def datebook_delivery_ready(tenant) -> bool:
    """The single rollout/consent gate for every datebook surface."""

    return bool(
        tenant
        and tenant.datebook_manifest_ok
        and tenant.datebook_enabled
        and (tenant.datebook_events_consent_at or tenant.datebook_reminders_consent_at)
    )
