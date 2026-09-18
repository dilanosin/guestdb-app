from datetime import timedelta

from django.conf import settings
from django.db import models
from django.utils import timezone


class Contact(models.Model):
    class Tier(models.TextChoices):
        TIER_A = "A", "Tier A (Quarterly)"
        TIER_B = "B", "Tier B (Semi-Annual)"
        TIER_C = "C", "Tier C (Annual)"

    class Status(models.TextChoices):
        ACTIVE = "ACTIVE", "Active"
        INACTIVE = "INACTIVE", "Inactive"
        ARCHIVED = "ARCHIVED", "Archived"

    VALIDATION_DAYS = {Tier.TIER_A: 90, Tier.TIER_B: 180, Tier.TIER_C: 365}

    first_name = models.CharField(max_length=100)
    last_name = models.CharField(max_length=100)
    position = models.CharField(max_length=150)
    company = models.CharField(max_length=150, db_index=True)
    email = models.EmailField(unique=True, db_index=True)
    telephone = models.CharField(max_length=50, blank=True)
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="contacts",
    )
    tier = models.CharField(max_length=1, choices=Tier.choices, default=Tier.TIER_C)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.ACTIVE)
    last_validated_at = models.DateTimeField(default=timezone.now)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["last_name", "first_name"]
        indexes = [
            models.Index(fields=["tier", "status"]),
            models.Index(fields=["owner", "status"]),
        ]

    def __str__(self):
        return f"{self.first_name} {self.last_name}"

    @property
    def full_name(self):
        return f"{self.first_name} {self.last_name}"

    @property
    def validation_threshold_days(self):
        return self.VALIDATION_DAYS[self.tier]

    @property
    def validation_due_date(self):
        return self.last_validated_at + timedelta(days=self.validation_threshold_days)

    @property
    def days_until_overdue(self):
        return (self.validation_due_date - timezone.now()).days

    @property
    def is_validation_overdue(self):
        if self.status != self.Status.ACTIVE:
            return False
        return timezone.now() > self.validation_due_date

    @property
    def validation_status(self):
        """Valid, Due soon (within 30% of SLA), or Overdue."""
        if self.status != self.Status.ACTIVE:
            return "inactive"
        if self.is_validation_overdue:
            return "overdue"
        if self.days_until_overdue <= int(self.validation_threshold_days * 0.3):
            return "due_soon"
        return "valid"