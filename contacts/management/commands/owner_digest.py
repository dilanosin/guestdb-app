"""Weekly Relationship Owner digest workflow (spec: Flow 2).

Groups overdue contacts by Relationship Owner and sends a single consolidated
email to each owner with a link to their 'My Contacts' overdue view.
"""

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.core.mail import send_mail
from django.core.management.base import BaseCommand
from django.urls import reverse

from contacts.models import Contact

User = get_user_model()


class Command(BaseCommand):
    help = "Send weekly consolidated validation reminder digest to owners."

    def handle(self, *args, **options):
        owners = User.objects.filter(groups__name="Account Managers")
        sent = 0
        for owner in owners:
            overdue = [
                c
                for c in Contact.objects.filter(
                    owner=owner, status=Contact.Status.ACTIVE
                )
                if c.is_validation_overdue
            ]
            if not overdue or not owner.email:
                continue
            lines = [
                "- {full} | {company} | {pos} | {email} | validated {date}".format(
                    full=c.full_name,
                    company=c.company,
                    pos=c.position,
                    email=c.email,
                    date=c.last_validated_at.date(),
                )
                for c in overdue
            ]
            link = "Browse overdue contacts in the app: /contacts/my/?overdue=1"
            body = (
                f"You have {len(overdue)} contact(s) due for validation.\n\n"
                + "\n".join(lines)
                + "\n\n"
                + link
            )
            send_mail(
                subject=f"[Guest DB] {len(overdue)} contact(s) to validate",
                message=body,
                from_email=None,
                recipient_list=[owner.email],
            )
            sent += 1
            self.stdout.write(f"Digest sent to {owner.email}: {len(overdue)} overdue")
        self.stdout.write(self.style.SUCCESS(f"Owner digests sent: {sent}"))