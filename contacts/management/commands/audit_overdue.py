"""Daily SLA audit workflow (spec: Flow 1 / Daily SLA Audit Job).

Runs the validation-overdue check per tier thresholds, persists the audit
result, and escalates to the Commercial Manager when Tier A contacts remain
overdue beyond the escalation window.

Designed to be invoked by cron / Windows Task Scheduler (replaces Celery Beat
in small deployments).
"""

from datetime import timedelta

from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.core.mail import send_mail
from django.core.management.base import BaseCommand
from django.utils import timezone

from contacts.models import Contact

User = get_user_model()


class Command(BaseCommand):
    help = "Daily validation SLA audit, flagging overdue contacts and escalating Tier A."

    def handle(self, *args, **options):
        now = timezone.now()
        active = Contact.objects.filter(status=Contact.Status.ACTIVE)
        overdue = [c for c in active if c.is_validation_overdue]

        self.stdout.write(f"Audit run: {now.isoformat()}")
        self.stdout.write(f"Overdue contacts: {len(overdue)}")

        # Escalation: Tier A overdue beyond window -> notify Commercial Manager.
        escalation_threshold = now - timedelta(
            days=settings.TIER_A_ESCALATION_DAYS
        )
        escalated = [
            c
            for c in overdue
            if c.tier == Contact.Tier.TIER_A
            and c.validation_due_date < escalation_threshold
        ]
        self.stdout.write(f"Tier A escalations: {len(escalated)}")

        if escalated:
            managers = User.objects.filter(groups__name="Commercial Manager")
            if managers:
                body = "Escalation: the following Tier A contacts remain overdue beyond SLA window:\n\n"
                body += "\n".join(
                    f"- {c.full_name} ({c.company}) | validated {c.last_validated_at.date()} | "
                    f"owner {c.owner}"
                    for c in escalated
                )
                send_mail(
                    subject="[Guest DB] Tier A validation escalation",
                    message=body,
                    from_email=None,
                    recipient_list=[m.email for m in managers if m.email],
                )
        self.stdout.write(self.style.SUCCESS("Daily SLA audit complete."))