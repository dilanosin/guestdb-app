from django import forms
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.core.mail import send_mail
from django.utils import timezone

from .models import Contact

User = get_user_model()


class ContactForm(forms.ModelForm):
    class Meta:
        model = Contact
        fields = [
            "first_name",
            "last_name",
            "position",
            "company",
            "email",
            "telephone",
            "owner",
            "tier",
            "status",
            "last_validated_at",
        ]
        widgets = {
            "last_validated_at": forms.DateTimeInput(attrs={"type": "datetime-local"}),
            "telephone": forms.TextInput(attrs={"placeholder": "Optional"}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Relationship Owners: every active user in a role group (incl. Commercial
        # Manager / IT), prioritised to Account Managers.
        role_groups = User.objects.filter(
            groups__name__in=["Account Managers", "Commercial Manager", "IT Administrator"]
        )
        if not role_groups.exists():
            role_groups = User.objects.all()
        self.fields["owner"].queryset = role_groups

    def clean_email(self):
        email = self.cleaned_data.get("email")
        if email:
            email = email.strip().lower()
        return email

    def clean(self):
        cleaned = super().clean()
        first = (cleaned.get("first_name") or "").strip().lower()
        last = (cleaned.get("last_name") or "").strip().lower()
        company = (cleaned.get("company") or "").strip().lower()

        if first and last and company:
            qs = Contact.objects.filter(
                first_name__iexact=first,
                last_name__iexact=last,
                company__iexact=company,
            )
            if self.instance.pk:
                qs = qs.exclude(pk=self.instance.pk)
            if qs.exists():
                self._alert_duplicate(qs.first())
                raise forms.ValidationError(
                    "A contact with this name and company already exists. "
                    "Duplicate records are prohibited — search for the existing record instead."
                )
        return cleaned

    def _alert_duplicate(self, existing):
        managers = User.objects.filter(groups__name="Commercial Manager")
        recipients = [m.email for m in managers if m.email]
        if not recipients:
            return
        send_mail(
            subject="[Guest DB] Duplicate contact attempt",
            message=(
                f"{timezone.now():%Y-%m-%d %H:%M} — a duplicate contact was blocked.\n\n"
                f"Existing record: {existing.full_name} ({existing.company}), "
                f"owner {existing.owner}, tier {existing.tier}.\n\n"
                "Please review."
            ),
            from_email=None,
            recipient_list=recipients,
        )