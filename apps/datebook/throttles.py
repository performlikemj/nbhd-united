from rest_framework.throttling import SimpleRateThrottle


class _DatebookUserThrottle(SimpleRateThrottle):
    def get_cache_key(self, request, view):
        if not request.user or not request.user.is_authenticated:
            return None
        return self.cache_format % {"scope": self.scope, "ident": str(request.user.pk)}


class DatebookSyncPageThrottle(_DatebookUserThrottle):
    # 600/hr comfortably admits an eight-page 400-item first snapshot plus retries.
    scope = "datebook_sync_page"


class DatebookCommandThrottle(_DatebookUserThrottle):
    # Claim/start/result are three calls per command; foreground recovery may replay them.
    scope = "datebook_command"


class DatebookReadThrottle(_DatebookUserThrottle):
    # Registration/open/commit are low-volume control calls, separate from bulk pages.
    scope = "datebook_read"


class DatebookRuntimeCreateThrottle(SimpleRateThrottle):
    """Separate tenant-scoped cap for internally authenticated create requests."""

    scope = "datebook_runtime_create"
    rate = "60/hour"

    def get_cache_key(self, request, view):
        tenant_id = request.headers.get("X-NBHD-Tenant-Id") or view.kwargs.get("tenant_id")
        if not tenant_id:
            return None
        return self.cache_format % {"scope": self.scope, "ident": str(tenant_id)}
