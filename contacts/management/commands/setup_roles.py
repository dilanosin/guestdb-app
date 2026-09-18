"""Ensure the role groups defined in the PRD exist.

Groups:
- Commercial Manager (Database Custodian)
- Account Managers (Relationship Owners & Data Stewards)
- Event Organizers (audience + exports + contact maintenance)
- IT Administrator (platform administration)
"""

from django.core.management.base import BaseCommand
from django.contrib.auth.models import Group, Permission
from django.contrib.contenttypes.models import ContentType
from django.db.models import Q

from contacts.models import Contact


class Command(BaseCommand):
    help = "Create/refresh the role groups and their permissions."

    def handle(self, *args, **options):
        ct = ContentType.objects.get_for_model(Contact)
        model_perms = Permission.objects.filter(content_type=ct)

        add = model_perms.get(codename="add_contact")
        change = model_perms.get(codename="change_contact")
        delete = model_perms.get(codename="delete_contact")
        view = model_perms.get(codename="view_contact")

        groups = {
            "Commercial Manager": [add, change, delete, view],
            "Account Managers": [add, change, view],
            "Event Organizers": [add, change, delete, view],
            "IT Administrator": [add, change, delete, view],
        }

        for name, perms in groups.items():
            group, created = Group.objects.get_or_create(name=name)
            group.permissions.set([p.id for p in perms])
            self.stdout.write(self.style.SUCCESS(f"{'Created' if created else 'Updated'} group: {name}"))

        self.stdout.write(self.style.SUCCESS("Role groups ready."))