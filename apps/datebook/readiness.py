def datebook_delivery_ready(tenant) -> bool:
    """Gate agent-facing runtime, envelope, and config surfaces.

    Consumer registration and sync/command endpoints use their narrower
    bootstrap gates and must not call this manifest-aware helper.
    """

    return bool(
        tenant
        and tenant.datebook_manifest_ok
        and tenant.datebook_enabled
        and (tenant.datebook_events_consent_at or tenant.datebook_reminders_consent_at)
    )
