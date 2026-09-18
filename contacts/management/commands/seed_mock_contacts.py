"""Seed 1,000 synthetic contacts per the PDF specification.

Distribution:
- Tiers: 15% Tier A, 35% Tier B, 50% Tier C
- Validation: 70% valid within SLA, 20% overdue past tier threshold, 10% Inactive/Archived
- Owners: evenly mapped across simulated Account Managers
"""

import random
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand
from django.utils import timezone
from faker import Faker

from contacts.models import Contact

User = get_user_model()

COMMERCIAL_POSITIONS = [
    "Commercial Manager",
    "Account Manager",
    "Commercial Director",
    "Business Development Manager",
    "Head of Commercial",
    "Senior Account Manager",
    "Key Account Manager",
    "Sales Director",
    "Commercial Analyst",
    "Managing Director",
    "CEO",
    "CFO",
    "Operations Director",
    "Partnerships Lead",
    "Client Relationship Manager",
]


class Command(BaseCommand):
    help = "Generate mock contacts for development/testing."

    def add_arguments(self, parser):
        parser.add_argument("--total", type=int, default=1000, help="Number of contacts to seed.")

    def handle(self, *args, **options):
        total = options["total"]
        fake = Faker()
        random.seed(42)

        owners = self._ensure_owners()
        tier_counts = {"A": int(total * 0.15), "B": int(total * 0.35), "C": int(total * 0.50)}
        tier_counts["C"] += total - sum(tier_counts.values())

        created = 0
        for tier, count in tier_counts.items():
            for i in range(count):
                owner = random.choice(owners)
                status, last_validated = self._pick_validation_state()
                email = fake.unique.email()

                Contact.objects.create(
                    first_name=fake.first_name(),
                    last_name=fake.last_name(),
                    position=random.choice(
                        COMMERCIAL_POSITIONS
                    )
                    if random.random() < 0.35
                    else fake.job(),
                    company=fake.company(),
                    email=email,
                    telephone=fake.phone_number(),
                    owner=owner,
                    tier=tier,
                    status=status,
                    last_validated_at=timezone.now() - last_validated,
                )
                created += 1

        self.stdout.write(self.style.SUCCESS(f"Seeded {created} contacts."))
        self.stdout.write(
            "Tier split: "
            + ", ".join(f"{k}: {v}" for k, v in tier_counts.items())
        )

    def _ensure_owners(self):
        """Relationship Owners as a mix of all role groups."""
        from django.contrib.auth.models import Group

        role_names = ["Account Managers", "Commercial Manager", "IT Administrator"]
        owners = list(
            User.objects.filter(groups__name__in=role_names).distinct()
        )
        if not owners:
            owners = list(User.objects.all())
        return owners

    def _pick_validation_state(self):
        """70% valid, 20% overdue, 10% inactive/archived."""
        roll = random.random()
        now = timezone.now()
        if roll < 0.10:
            status = random.choice([Contact.Status.INACTIVE, Contact.Status.ARCHIVED])
            return status, timedelta(days=random.randint(120, 800))
        if roll < 0.30:
            return Contact.Status.ACTIVE, timedelta(days=200)
        days_ago = random.randint(1, 100)
        return Contact.Status.ACTIVE, timedelta(days=days_ago)