"""Create demo users for each role defined in the PRD.

Emails/passwords:
- admin            / admin12345   (already created superuser if present)
- commercial       / demo12345     (Commercial Manager)
- organizer        / demo12345     (Event Organizers)
- itadmin          / demo12345     (IT Administrator)
- manager          / demo12345     (Account Manager - single owner for all contacts)
"""

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.core.management.base import BaseCommand

from contacts.models import Contact

User = get_user_model()


class Command(BaseCommand):
    help = "Seed demo users for all role groups."

    def handle(self, *args, **options):
        role_users = [
            ("commercial", "Commercial Manager", "Commercial", "Manager"),
            ("organizer", "Event Organizers", "Event", "Organizer"),
            ("itadmin", "IT Administrator", "IT", "Administrator"),
            ("manager", "Account Managers", "Account", "Manager"),
        ]

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

        # Idempotent consolidation of pre-consolidation logins. Contacts are
        # PROTECT-ed against owner deletion, so re-own before dropping the user.
        # am1..am6 and the older `accountmanager` all fold into `manager`;
        # the older `eventorganizer` folds into `organizer`.
        supersedes = {
            "manager": User.objects.filter(username__regex=r"^am[1-6]$")
            | User.objects.filter(username="accountmanager"),
            "organizer": User.objects.filter(username="eventorganizer"),
        }
        for target_username, stale_qs in supersedes.items():
            target = User.objects.get(username=target_username)
            for old_user in list(stale_qs.exclude(username=target_username)):
                moved = Contact.objects.filter(owner=old_user).update(owner=target)
                old_user.groups.clear()
                old_user.delete()
                self.stdout.write(
                    self.style.SUCCESS(
                        f"Consolidated {old_user.username} -> {target_username} "
                        f"({moved} contacts re-owned)"
                    )
                )

        self.stdout.write(self.style.SUCCESS("Demo users ready. Password for all: demo12345"))