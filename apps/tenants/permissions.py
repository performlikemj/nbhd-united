"""DRF permission classes for PAT scope enforcement.

PAT-authenticated requests must carry the required scope; JWT-authenticated
requests (the subscriber console) bypass scope checks because the user has
full session-level access.
"""

from __future__ import annotations

from rest_framework import permissions

ALLOWED_PAT_SCOPES: frozenset[str] = frozenset({"sessions:write", "sessions:read"})


class HasPATScope(permissions.IsAuthenticated):
    """Require ``required_scope`` when the request is authed via a PAT.

    Subclasses set ``required_scope``. JWT requests pass through.
    """

    required_scope: str = ""

    def has_permission(self, request, view) -> bool:
        if not super().has_permission(request, view):
            return False

        pat = getattr(request, "auth_pat", None)
        if pat is None:
            return True

        return self.required_scope in (pat.scopes or [])


class HasSessionsWriteScope(HasPATScope):
    required_scope = "sessions:write"


class HasSessionsReadScope(HasPATScope):
    required_scope = "sessions:read"
