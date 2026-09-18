"""Create demo users for each role defined in the PRD.

Emails/passwords:
- admin            / admin12345   (already created superuser if present)
- commercial       / demo12345     (Commercial Manager)
- organizer        / demo12345     (Event Organizers)
- itadmin          / demo12345     (IT Administrator)
- am1..am6         / demo12345     (Account Managers)
"""

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.core.management.base import BaseCommand

User = get_user_model()


class Command(BaseCommand):
    help = "Seed demo users for all role groups."

    def handle(self, *args, **options):
        role_users = [
            ("commercial", "Commercial Manager", "Commercial", "Manager"),
            ("organizer", "Event Organizers", "Event", "Organizer"),
            ("itadmin", "IT Administrator", "IT", "Administrator"),
        ] + [(f"am{i}", "Account Managers", f"Account Manager {i}", "") for i in range(1, 7)]

        for username, group_name, first, last in role_users:
            group = Group.objects.get(name=group_name)
            user, created = User.objects.get_or_create(
                username=username,
                defaults={
                    "email": f"{username}@example.com",
                    "first_name": first,
                    "last_name": last,
                },
            )
            if created:
                user.set_password("demo12345")
                user.save()
            user.groups.add(group)
            self.stdout.write(
                self.style.SUCCESS(
                    f"{'Created' if created else 'Updated'} {username} -> {group_name}"
                )
            )

        # Idempotent: point any unused account managers at the group too.
        self.stdout.write(self.style.SUCCESS("Demo users ready. Password for all: demo12345"))