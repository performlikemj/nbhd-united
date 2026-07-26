import uuid

from django.db import models


class License(models.Model):
    class Status(models.TextChoices):
        ACTIVE = "active", "Active"
        REVOKED = "revoked", "Revoked"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    key = models.CharField(max_length=17, unique=True)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.ACTIVE)
    purchaser_email = models.EmailField(db_index=True)
    stripe_session_id = models.CharField(max_length=255, unique=True)
    stripe_customer_id = models.CharField(max_length=255, blank=True, default="")
    stripe_payment_intent_id = models.CharField(max_length=255, blank=True, default="")
    key_email_sent_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"{self.key} ({self.purchaser_email})"


class LicenseActivation(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    license = models.ForeignKey(License, related_name="activations", on_delete=models.CASCADE)
    device_id = models.CharField(max_length=64)
    activated_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["license", "device_id"],
                name="yardtalk_unique_license_device",
            )
        ]
        ordering = ["activated_at"]

    def __str__(self) -> str:
        return f"{self.license.key}: {self.device_id}"
