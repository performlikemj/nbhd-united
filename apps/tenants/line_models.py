"""LINE link token model for deep link account linking flow."""

import uuid

from django.db import models
from django.utils import timezone


class LineLinkToken(models.Model):
    """
    One-time token for linking a LINE account to an NBHD United user.

    Flow:
    1. User signs up on web → clicks "Connect LINE"
    2. Backend generates token, returns LINE deep link:
       https://line.me/R/oaMessage/@BOT_ID/?link_TOKEN
    3. User taps the link → LINE opens → bot receives message containing token
    4. Django validates token → sets line_user_id on User
    5. Token marked as used

    Tokens expire after 15 minutes and can only be used once.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(
        "tenants.User",
        on_delete=models.CASCADE,
        related_name="line_link_tokens",
    )
    token = models.CharField(max_length=64, unique=True, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField()
    used = models.BooleanField(default=False)

    class Meta:
        db_table = "line_link_tokens"
        indexes = [
            models.Index(fields=["expires_at"]),
        ]

    def __str__(self) -> str:
        status = "used" if self.used else ("expired" if not self.is_valid else "valid")
        return f"LINEToken for {self.user.display_name} ({status})"

    @property
    def is_valid(self) -> bool:
        return not self.used and timezone.now() < self.expires_at
