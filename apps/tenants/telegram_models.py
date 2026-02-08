"""Telegram link token model for QR code / deep link onboarding flow."""
import uuid

from django.db import models
from django.utils import timezone


class TelegramLinkToken(models.Model):
    """
    One-time token for linking a Telegram account to an NBHD United user.

    Flow:
    1. User signs up on web → clicks "Connect Telegram"
    2. Backend generates token, returns QR code + deep link
    3. User scans QR / clicks link → opens Telegram → sends /start TOKEN
    4. Router webhook validates token → links telegram_user_id to User
    5. Token marked as used

    Tokens expire after 10 minutes and can only be used once.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(
        "tenants.User",
        on_delete=models.CASCADE,
        related_name="telegram_link_tokens",
    )
    token = models.CharField(max_length=64, unique=True, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField()
    used = models.BooleanField(default=False)

    class Meta:
        db_table = "telegram_link_tokens"
        indexes = [
            models.Index(fields=["expires_at"]),
        ]

    def __str__(self) -> str:
        status = "used" if self.used else ("expired" if not self.is_valid else "valid")
        return f"Token for {self.user.display_name} ({status})"

    @property
    def is_valid(self) -> bool:
        return not self.used and timezone.now() < self.expires_at
