"""Test suite for the contacts app.

Covers the validation SLA logic, duplicate prevention, the overdue trend
aggregation, access control, the export endpoints, and the deployment
configuration helpers.
"""

import json
import os
from datetime import timedelta
from unittest import mock

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group, Permission
from django.core import mail
from django.test import RequestFactory, SimpleTestCase, TestCase
from django.urls import reverse
from django.utils import timezone

from config.settings import env_flag, env_list

from . import views
from .forms import ContactForm
from .models import Contact
from .views import (
    TREND_METRICS,
    TREND_RANGES,
    build_trend,
    find_duplicates,
    overdue_trend,
)

User = get_user_model()


def grant(user, *codenames):
    """Attach model permissions to a user, then reload to clear the perm cache."""
    perms = Permission.objects.filter(
        content_type__app_label="contacts", codename__in=codenames
    )
    user.user_permissions.add(*perms)
    return User.objects.get(pk=user.pk)


def make_contact(**kwargs):
    """Create a contact with sane defaults, owned by a throwaway user."""
    defaults = {
        "first_name": "Ada",
        "last_name": "Lovelace",
        "position": "Analyst",
        "company": "Analytical Engines",
        "email": "ada@example.com",
        "telephone": "",
        "owner": kwargs.pop("owner", None) or User.objects.create_user(
            username=f"owner_{Contact.objects.count()}_{kwargs.get('email', 'x')}",
            password="pw",
        ),
        "tier": Contact.Tier.TIER_A,
        "status": Contact.Status.ACTIVE,
        "last_validated_at": timezone.now(),
    }
    defaults.update(kwargs)
    return Contact.objects.create(**defaults)


class ContactModelTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(username="owner", password="pw")

    def test_full_name_and_str(self):
        c = make_contact(owner=self.owner)
        self.assertEqual(c.full_name, "Ada Lovelace")
        self.assertEqual(str(c), "Ada Lovelace")

    def test_default_ordering_is_last_then_first_name(self):
        make_contact(owner=self.owner, email="b@x.com", first_name="Zoe", last_name="Adams")
        make_contact(owner=self.owner, email="a@x.com", first_name="Amy", last_name="Adams")
        make_contact(owner=self.owner, email="c@x.com", first_name="Mia", last_name="Baker")
        names = list(Contact.objects.values_list("last_name", flat=True))
        self.assertEqual(names, ["Adams", "Adams", "Baker"])
        self.assertEqual(
            list(Contact.objects.values_list("first_name", flat=True)),
            ["Amy", "Zoe", "Mia"],
        )

    def test_tier_sla_thresholds(self):
        self.assertEqual(Contact.VALIDATION_DAYS[Contact.Tier.TIER_A], 90)
        self.assertEqual(Contact.VALIDATION_DAYS[Contact.Tier.TIER_B], 180)
        self.assertEqual(Contact.VALIDATION_DAYS[Contact.Tier.TIER_C], 365)

    def test_validation_due_date_respects_tier(self):
        validated = timezone.now() - timedelta(days=100)
        tier_a = make_contact(owner=self.owner, email="a@x.com", last_validated_at=validated)
        tier_c = make_contact(owner=self.owner, email="c@x.com", tier=Contact.Tier.TIER_C, last_validated_at=validated)
        self.assertEqual(tier_a.validation_threshold_days, 90)
        self.assertEqual(tier_c.validation_threshold_days, 365)
        self.assertLess(tier_a.validation_due_date, tier_c.validation_due_date)

    def test_overdue_only_for_active_contacts(self):
        old = timezone.now() - timedelta(days=200)
        active = make_contact(owner=self.owner, email="a@x.com", last_validated_at=old)
        inactive = make_contact(
            owner=self.owner,
            email="i@x.com",
            status=Contact.Status.INACTIVE,
            last_validated_at=old,
        )
        archived = make_contact(
            owner=self.owner,
            email="r@x.com",
            status=Contact.Status.ARCHIVED,
            last_validated_at=old,
        )
        self.assertTrue(active.is_validation_overdue)
        self.assertFalse(inactive.is_validation_overdue)
        self.assertFalse(archived.is_validation_overdue)

    def test_validation_status_transitions(self):
        owner = self.owner
        valid = make_contact(
            owner=owner,
            email="v@x.com",
            last_validated_at=timezone.now(),
        )
        due_soon = make_contact(
            owner=owner,
            email="d@x.com",
            # 80 days elapsed on a 90-day SLA leaves 10 days (<= 30% of 90).
            last_validated_at=timezone.now() - timedelta(days=80),
        )
        overdue = make_contact(
            owner=owner,
            email="o@x.com",
            last_validated_at=timezone.now() - timedelta(days=200),
        )
        inactive = make_contact(
            owner=owner,
            email="n@x.com",
            status=Contact.Status.INACTIVE,
            last_validated_at=timezone.now() - timedelta(days=200),
        )
        self.assertEqual(valid.validation_status, "valid")
        self.assertEqual(due_soon.validation_status, "due_soon")
        self.assertEqual(overdue.validation_status, "overdue")
        self.assertEqual(inactive.validation_status, "inactive")

    def test_days_until_overdue_counts_down(self):
        c = make_contact(
            owner=self.owner,
            email="x@x.com",
            last_validated_at=timezone.now() - timedelta(days=80),
        )
        self.assertLessEqual(c.days_until_overdue, 10)
        self.assertGreaterEqual(c.days_until_overdue, 9)

    def test_owner_cannot_be_deleted_while_contacts_exist(self):
        c = make_contact(owner=self.owner)
        with self.assertRaises(Exception):
            self.owner.delete()
        c.refresh_from_db()
        self.assertEqual(c.owner, self.owner)

    def test_email_is_unique(self):
        make_contact(owner=self.owner, email="dup@x.com")
        with self.assertRaises(Exception):
            make_contact(owner=self.owner, email="dup@x.com", first_name="Other")


class ContactFormTests(TestCase):
    def setUp(self):
        self.group = Group.objects.create(name="Account Managers")
        self.owner = User.objects.create_user(
            username="manager", password="pw", email="manager@example.com"
        )
        self.owner.groups.add(self.group)
        self.custodian = User.objects.create_user(
            username="commercial", password="pw", email="commercial@example.com"
        )
        custodian_group = Group.objects.create(name="Commercial Manager")
        self.custodian.groups.add(custodian_group)
        mail.outbox = []

    def valid_payload(self, **overrides):
        data = {
            "first_name": "Grace",
            "last_name": "Hopper",
            "position": "Rear Admiral",
            "company": "US Navy",
            "email": "grace@example.com",
            "telephone": "",
            "owner": self.owner.pk,
            "tier": Contact.Tier.TIER_A,
            "status": Contact.Status.ACTIVE,
            "last_validated_at": timezone.now().strftime("%Y-%m-%dT%H:%M"),
        }
        data.update(overrides)
        return data

    def test_owner_queryset_prefers_role_groups(self):
        stranger = User.objects.create_user(username="stranger", password="pw")
        form = ContactForm()
        owners = form.fields["owner"].queryset
        self.assertIn(self.owner, owners)
        self.assertIn(self.custodian, owners)
        self.assertNotIn(stranger, owners)

    def test_email_is_normalised_to_lowercase_and_trimmed(self):
        form = ContactForm(data=self.valid_payload(email="  Grace@EXAMPLE.com "))
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.cleaned_data["email"], "grace@example.com")

    def test_duplicate_name_and_company_is_rejected(self):
        make_contact(
            owner=self.owner,
            first_name="Grace",
            last_name="Hopper",
            company="US Navy",
            email="existing@example.com",
        )
        form = ContactForm(data=self.valid_payload())
        self.assertFalse(form.is_valid())
        self.assertIn("already exists", str(form.errors))

    def test_duplicate_detection_is_case_insensitive(self):
        make_contact(
            owner=self.owner,
            first_name="Grace",
            last_name="Hopper",
            company="US Navy",
            email="existing@example.com",
        )
        form = ContactForm(
            data=self.valid_payload(first_name="GRACE", last_name="hopper", company="us navy")
        )
        self.assertFalse(form.is_valid())

    def test_blocked_duplicate_emails_the_custodian(self):
        make_contact(
            owner=self.owner,
            first_name="Grace",
            last_name="Hopper",
            company="US Navy",
            email="existing@example.com",
        )
        form = ContactForm(data=self.valid_payload())
        self.assertFalse(form.is_valid())
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn("commercial@example.com", mail.outbox[0].to)

    def test_editing_a_contact_does_not_flag_itself(self):
        existing = make_contact(
            owner=self.owner,
            first_name="Grace",
            last_name="Hopper",
            company="US Navy",
            email="grace@example.com",
        )
        form = ContactForm(
            instance=existing,
            data=self.valid_payload(position="Rear Admiral, NAVY"),
        )
        self.assertTrue(form.is_valid(), form.errors)


class OverdueTrendTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(username="manager", password="pw")

    def test_returns_requested_number_of_points_oldest_first(self):
        trend = overdue_trend(12)
        self.assertEqual(len(trend["counts_json"].split(",")), 12)
        labels = trend["labels_json"].strip("[]").replace('"', "").split(", ")
        self.assertEqual(len(labels), 12)
        # Oldest label must precede the newest chronologically.
        self.assertNotEqual(labels[0], labels[-1])
        self.assertLess(labels[0], labels[-1])

    def test_labels_are_unique(self):
        labels = overdue_trend(12)["labels_json"].strip("[]").replace('"', "").split(", ")
        self.assertEqual(len(labels), len(set(labels)))

    def test_final_point_matches_live_overdue_kpi(self):
        old = timezone.now() - timedelta(days=400)
        for i in range(5):
            make_contact(owner=self.owner, email=f"old{i}@x.com", last_validated_at=old)
        for i in range(3):
            make_contact(owner=self.owner, email=f"new{i}@x.com")

        trend = overdue_trend(12)
        final = int(trend["counts_json"].strip("[]").split(", ")[-1])
        overdue_now = sum(1 for c in Contact.objects.all() if c.is_validation_overdue)
        self.assertEqual(final, overdue_now)
        self.assertEqual(final, 5)

    def test_counts_are_non_decreasing_over_time(self):
        old = timezone.now() - timedelta(days=400)
        for i in range(4):
            make_contact(owner=self.owner, email=f"old{i}@x.com", last_validated_at=old)
        counts = [int(x) for x in overdue_trend(12)["counts_json"].strip("[]").split(", ")]
        # Once a contact passes its SLA it stays overdue, so the series can
        # only hold steady or rise.
        self.assertEqual(counts, sorted(counts))

    def test_ignores_non_active_contacts(self):
        old = timezone.now() - timedelta(days=400)
        make_contact(
            owner=self.owner,
            email="a@x.com",
            status=Contact.Status.ARCHIVED,
            last_validated_at=old,
        )
        counts = [int(x) for x in overdue_trend(6)["counts_json"].strip("[]").split(", ")]
        self.assertEqual(counts[-1], 0)

    def test_reported_ranges_are_offered(self):
        self.assertEqual(list(TREND_RANGES), [6, 12, 24])
        for r in TREND_RANGES:
            self.assertEqual(
                len(overdue_trend(r)["counts_json"].strip("[]").split(",")), r
            )

    def test_out_of_range_months_are_clamped(self):
        # 1 is clamped up to the 2-month floor; 999 clamps down to 36.
        self.assertEqual(
            len(overdue_trend(1)["counts_json"].strip("[]").split(",")), 2
        )
        self.assertEqual(
            len(overdue_trend(999)["counts_json"].strip("[]").split(",")), 36
        )

    def test_peak_and_delta_metadata(self):
        old = timezone.now() - timedelta(days=400)
        for i in range(2):
            make_contact(owner=self.owner, email=f"old{i}@x.com", last_validated_at=old)
        trend = overdue_trend(12)
        counts = [int(x) for x in trend["counts_json"].strip("[]").split(", ")]
        self.assertEqual(trend["peak"], max(counts))
        self.assertEqual(trend["delta"], counts[-1] - counts[0])
        self.assertEqual(trend["delta_abs"], abs(counts[-1] - counts[0]))


class DuplicateDetectionTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(username="manager", password="pw")

    def test_no_duplicates_on_empty_database(self):
        self.assertEqual(find_duplicates(), [])

    def test_groups_shared_name_and_company(self):
        make_contact(
            owner=self.owner, email="a@x.com", first_name="Ada", last_name="Byron",
            company="Acme",
        )
        make_contact(
            owner=self.owner, email="b@x.com", first_name="Ada", last_name="Byron",
            company="Acme",
        )
        make_contact(
            owner=self.owner, email="c@x.com", first_name="Grace", last_name="Hopper",
            company="Acme",
        )
        found = find_duplicates()
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["key"], "Ada Byron — Acme")
        self.assertEqual(len(found[0]["ids"]), 2)

    def test_same_name_different_company_is_not_a_duplicate(self):
        make_contact(owner=self.owner, email="a@x.com", company="Acme")
        make_contact(owner=self.owner, email="b@x.com", company="Globex")
        self.assertEqual(find_duplicates(), [])

    def test_whitespace_and_case_are_normalised(self):
        make_contact(owner=self.owner, email="a@x.com", first_name="Ada", last_name="Byron", company="Acme")
        make_contact(owner=self.owner, email="b@x.com", first_name=" ada ", last_name="BYRON", company=" acme ")
        self.assertEqual(len(find_duplicates()), 1)


class NavBadgeContextProcessorTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(username="manager", password="pw")
        self.user = User.objects.create_user(username="viewer", password="pw")

    def test_anonymous_request_reports_zero_without_touching_db(self):
        from .context_processors import nav_badges

        class FakeRequest:
            user = None

        self.assertEqual(nav_badges(FakeRequest())["unresolved_duplicates"], 0)

    def test_counts_groups_not_rows(self):
        from .context_processors import nav_badges

        class FakeRequest:
            user = self.user

        for i in range(2):
            make_contact(
                owner=self.owner,
                email=f"d{i}@x.com",
                first_name="Ada",
                last_name="Byron",
                company="Acme",
            )
        make_contact(owner=self.owner, email="solo@x.com", first_name="Solo", company="Acme")

        request = FakeRequest()
        request.user = self.user
        self.assertEqual(nav_badges(request)["unresolved_duplicates"], 1)


class AccessControlTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="manager", password="pw")

    def test_dashboard_requires_login(self):
        response = self.client.get(reverse("dashboard"))
        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse("login"), response["Location"])

    def test_anonymous_is_redirected_away_from_every_protected_page(self):
        for name in ("dashboard", "contact_list", "my_contacts", "events", "duplicates"):
            with self.subTest(page=name):
                response = self.client.get(reverse(name))
                self.assertEqual(response.status_code, 302)

    def test_login_page_is_public(self):
        self.assertEqual(self.client.get(reverse("login")).status_code, 200)

    def test_authenticated_user_reaches_the_dashboard(self):
        self.client.force_login(self.user)
        self.assertEqual(self.client.get(reverse("dashboard")).status_code, 200)

    def test_add_contact_requires_the_permission(self):
        self.client.force_login(self.user)
        self.assertEqual(self.client.get(reverse("contact_add")).status_code, 403)

        perm = Permission.objects.get(
            content_type__app_label="contacts", codename="add_contact"
        )
        self.user.user_permissions.add(perm)
        self.user = User.objects.get(pk=self.user.pk)  # drop perm cache
        self.client.force_login(self.user)
        self.assertEqual(self.client.get(reverse("contact_add")).status_code, 200)


class DashboardTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="manager", password="pw")
        self.client.force_login(self.user)

    def test_kpis_are_exposed(self):
        make_contact(owner=self.user, email="a@x.com")
        ctx = self.client.get(reverse("dashboard")).context
        self.assertEqual(ctx["kpis"]["total_active"], 1)
        self.assertEqual(ctx["kpis"]["unowned"], 0)

    def test_default_range_is_twelve_months(self):
        ctx = self.client.get(reverse("dashboard")).context
        self.assertEqual(ctx["trend"]["months"], 12)

    def test_range_query_parameter_is_honoured(self):
        for months in TREND_RANGES:
            with self.subTest(months=months):
                ctx = self.client.get(reverse("dashboard"), {"range": months}).context
                self.assertEqual(ctx["trend"]["months"], months)

    def test_non_numeric_range_falls_back_to_default(self):
        ctx = self.client.get(reverse("dashboard"), {"range": "bogus"}).context
        self.assertEqual(ctx["trend"]["months"], 12)

    def test_out_of_range_parameter_is_clamped(self):
        ctx = self.client.get(reverse("dashboard"), {"range": 999}).context
        self.assertEqual(ctx["trend"]["months"], 36)

    def test_greeting_varies_by_hour(self):
        ctx = self.client.get(reverse("dashboard")).context
        self.assertIn(ctx["greeting"], {"Good morning", "Good afternoon", "Good evening"})

    def test_tier_rows_are_always_present(self):
        ctx = self.client.get(reverse("dashboard")).context
        self.assertEqual([t["label"] for t in ctx["tiers"]], ["A", "B", "C"])


class ContactListTests(TestCase):
    def setUp(self):
        # The list view is permission-gated, so the test user needs view_contact.
        self.user = grant(
            User.objects.create_user(username="manager", password="pw"),
            "view_contact",
        )
        self.client.force_login(self.user)
        make_contact(owner=self.user, email="ada@x.com", first_name="Ada", company="Acme")
        make_contact(
            owner=self.user, email="grace@x.com", first_name="Grace", company="Globex",
            tier=Contact.Tier.TIER_C, status=Contact.Status.INACTIVE,
        )

    def test_lists_contacts(self):
        ctx = self.client.get(reverse("contact_list")).context
        self.assertEqual(len(ctx["contacts"]), 2)

    def test_search_filters_across_fields(self):
        for term in ("ada", "Globex", "grace@x.com"):
            with self.subTest(term=term):
                ctx = self.client.get(reverse("contact_list"), {"q": term}).context
                self.assertEqual(len(ctx["contacts"]), 1)

    def test_tier_and_status_filters_combine(self):
        ctx = self.client.get(
            reverse("contact_list"), {"tier": "C", "status": "INACTIVE"}
        ).context
        self.assertEqual(len(ctx["contacts"]), 1)
        self.assertEqual(ctx["contacts"][0].email, "grace@x.com")

    def test_filters_return_empty_set_cleanly(self):
        response = self.client.get(reverse("contact_list"), {"q": "nobodyhere"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.context["contacts"]), 0)

    def test_pagination_is_configured(self):
        response = self.client.get(reverse("contact_list"))
        self.assertEqual(response.context["paginator"].per_page, 25)

    def test_overdue_filter_on_my_contacts(self):
        old = timezone.now() - timedelta(days=400)
        make_contact(
            owner=self.user, email="stale@x.com", first_name="Stale",
            last_validated_at=old,
        )
        ctx = self.client.get(reverse("my_contacts"), {"overdue": "1"}).context
        self.assertEqual([c.email for c in ctx["contacts"]], ["stale@x.com"])


class ValidateContactTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(username="manager", password="pw")
        self.perms = Permission.objects.filter(
            content_type__app_label="contacts",
            codename__in=["change_contact", "view_contact"],
        )
        self.owner.user_permissions.add(*self.perms)
        self.owner = User.objects.get(pk=self.owner.pk)
        self.client.force_login(self.owner)
        self.contact = make_contact(
            owner=self.owner,
            email="stale@x.com",
            status=Contact.Status.INACTIVE,
            last_validated_at=timezone.now() - timedelta(days=400),
        )

    def test_validate_resets_the_sla_clock_and_reactivates(self):
        self.client.post(reverse("contact_validate", args=[self.contact.pk]))
        self.contact.refresh_from_db()
        self.assertEqual(self.contact.status, Contact.Status.ACTIVE)
        self.assertFalse(self.contact.is_validation_overdue)

    def test_owner_can_validate_their_own_contact(self):
        response = self.client.post(reverse("contact_validate", args=[self.contact.pk]))
        self.assertEqual(response.status_code, 302)

    def test_third_party_cannot_validate(self):
        other = User.objects.create_user(username="other", password="pw")
        other.user_permissions.add(*self.perms)
        other = User.objects.get(pk=other.pk)
        self.client.force_login(other)
        self.client.post(reverse("contact_validate", args=[self.contact.pk]))
        self.contact.refresh_from_db()
        self.assertEqual(self.contact.status, Contact.Status.INACTIVE)

    def test_custodian_may_validate_any_contact(self):
        custodian = User.objects.create_user(username="commercial", password="pw")
        custodian.groups.add(Group.objects.create(name="Commercial Manager"))
        custodian.user_permissions.add(*self.perms)
        custodian = User.objects.get(pk=custodian.pk)
        self.client.force_login(custodian)
        self.client.post(reverse("contact_validate", args=[self.contact.pk]))
        self.contact.refresh_from_db()
        self.assertEqual(self.contact.status, Contact.Status.ACTIVE)


class ExportTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="manager", password="pw")
        self.client.force_login(self.user)
        make_contact(owner=self.user, email="ada@x.com")

    def test_csv_export(self):
        response = self.client.get(reverse("export_csv"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "text/csv")
        self.assertIn("guest_list.csv", response["Content-Disposition"])
        self.assertIn(b"ada@x.com", response.content)

    def test_xlsx_export_is_a_valid_zip_container(self):
        response = self.client.get(reverse("export_xlsx"))
        self.assertEqual(response.status_code, 200)
        self.assertIn("spreadsheetml", response["Content-Type"])
        self.assertIn("guest_database.xlsx", response["Content-Disposition"])
        # PK header is the zip magic number.
        self.assertTrue(response.content.startswith(b"PK"))

    def test_exports_exclude_inactive_contacts(self):
        make_contact(
            owner=self.user, email="gone@x.com", status=Contact.Status.INACTIVE
        )
        response = self.client.get(reverse("export_csv"))
        self.assertNotIn(b"gone@x.com", response.content)


class EventBuilderTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="manager", password="pw")
        self.client.force_login(self.user)
        make_contact(owner=self.user, email="ada@x.com", company="Acme")
        make_contact(owner=self.user, email="bob@x.com", company="Globex")

    def test_lists_only_active_contacts(self):
        make_contact(
            owner=self.user, email="old@x.com", status=Contact.Status.ARCHIVED
        )
        ctx = self.client.get(reverse("events")).context
        self.assertEqual(ctx["contacts"].count(), 2)

    def test_company_filter(self):
        ctx = self.client.get(reverse("events"), {"company": "acme"}).context
        self.assertEqual([c.email for c in ctx["contacts"]], ["ada@x.com"])

    def test_search_and_tier_filters(self):
        ctx = self.client.get(reverse("events"), {"q": "bob"}).context
        self.assertEqual([c.email for c in ctx["contacts"]], ["bob@x.com"])
        ctx = self.client.get(reverse("events"), {"tier": "B"}).context
        self.assertEqual(ctx["contacts"].count(), 0)


class DuplicatePageTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="manager", password="pw")
        self.client.force_login(self.user)

    def test_page_renders_with_no_duplicates(self):
        response = self.client.get(reverse("duplicates"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["duplicates"], [])

    def test_duplicate_groups_are_listed(self):
        for i in range(2):
            make_contact(
                owner=self.user, email=f"d{i}@x.com",
                first_name="Ada", last_name="Byron", company="Acme",
            )
        ctx = self.client.get(reverse("duplicates")).context
        self.assertEqual(len(ctx["duplicates"]), 1)


class SettingsTests(TestCase):
    def test_mail_backend_is_console_or_test_local(self):
        from django.conf import settings

        # Django's test runner swaps in the locmem backend automatically.
        self.assertIn(
            settings.MAILERS["default"]["BACKEND"],
            {
                "django.core.mail.backends.console.EmailBackend",
                "django.core.mail.backends.locmem.EmailBackend",
            },
        )

    def test_secret_key_is_not_the_checked_in_default(self):
        from django.conf import settings

        self.assertTrue(settings.SECRET_KEY)
        self.assertNotIn("django-insecure", settings.SECRET_KEY)

    def test_password_validators_are_enabled(self):
        from django.conf import settings

        self.assertGreaterEqual(len(settings.AUTH_PASSWORD_VALIDATORS), 4)

    def test_security_middleware_is_installed(self):
        from django.conf import settings

        self.assertIn(
            "django.middleware.security.SecurityMiddleware", settings.MIDDLEWARE
        )
        self.assertIn(
            "django.middleware.clickjacking.XFrameOptionsMiddleware", settings.MIDDLEWARE
        )

    def test_allowed_hosts_is_not_a_wildcard(self):
        from django.conf import settings

        self.assertNotIn("*", settings.ALLOWED_HOSTS)

    def test_security_headers_are_enabled(self):
        from django.conf import settings

        self.assertEqual(settings.SECURE_CONTENT_TYPE_NOSNIFF, True)
        self.assertEqual(settings.SECURE_REFERRER_POLICY, "same-origin")
        self.assertEqual(settings.X_FRAME_OPTIONS, "DENY")

    def test_debug_mode_relaxes_the_transport_settings(self):
        """Dev defaults must not force HTTPS, or the local server breaks."""
        from django.conf import settings

        # NOTE: Django's test runner forces settings.DEBUG=False at runtime, so
        # these assert the values the module computed when DEBUG defaulted on.
        self.assertFalse(settings.SECURE_SSL_REDIRECT)
        self.assertFalse(settings.SESSION_COOKIE_SECURE)
        self.assertFalse(settings.CSRF_COOKIE_SECURE)
        self.assertEqual(settings.SECURE_HSTS_SECONDS, 0)

    def test_error_handlers_are_wired_up(self):
        from django.conf import settings

        self.assertEqual(settings.HANDLER403, "contacts.views.permission_denied")
        self.assertEqual(settings.HANDLER404, "contacts.views.page_not_found")


class ErrorPageTests(TestCase):
    def test_403_renders_the_branded_page(self):
        response = views.permission_denied(RequestFactory().get("/contacts/"))

        self.assertEqual(response.status_code, 403)
        self.assertIn("403", response.content.decode())

    def test_404_renders_the_branded_page(self):
        response = views.page_not_found(RequestFactory().get("/nope/"))

        self.assertEqual(response.status_code, 404)
        body = response.content.decode()
        self.assertIn("Page not found", body)
        self.assertIn("Back to dashboard", body)


class DashboardMetricParityTests(TestCase):
    """The dashboard math moved from Python loops to SQL cutoffs.

    These lock the SQL version to the same answers the per-row property used to
    produce, so a future refactor cannot quietly change a reported KPI.
    """

    def setUp(self):
        self.owner = User.objects.create_user(username="manager", password="pw")
        self.today = timezone.now()

        make_contact(
            owner=self.owner, email="fresh-a@example.com", tier=Contact.Tier.TIER_A,
            last_validated_at=self.today, status=Contact.Status.ACTIVE,
        )
        make_contact(
            owner=self.owner, email="stale-a@example.com", tier=Contact.Tier.TIER_A,
            last_validated_at=self.today - timedelta(days=200),
            status=Contact.Status.ACTIVE,
        )
        make_contact(
            owner=self.owner, email="mid-b@example.com", tier=Contact.Tier.TIER_B,
            last_validated_at=self.today - timedelta(days=100),
            status=Contact.Status.ACTIVE,
        )
        make_contact(
            owner=self.owner, email="archived-c@example.com", tier=Contact.Tier.TIER_C,
            last_validated_at=self.today - timedelta(days=10),
            status=Contact.Status.ARCHIVED,
        )

    def reference_counts(self):
        """Brute-force truth using the model property."""
        out = {}
        for tier in Contact.Tier.values:
            rows = [
                c
                for c in Contact.objects.filter(
                    status=Contact.Status.ACTIVE, tier=tier
                )
            ]
            overdue = sum(1 for c in rows if c.is_validation_overdue)
            out[tier] = {
                "total": len(rows),
                "validated": len(rows) - overdue,
                "overdue": overdue,
            }
        return out

    def test_breakdown_matches_the_model_property(self):
        self.assertEqual(views.tier_validation_breakdown(), self.reference_counts())

    def test_kpi_aggregates_match_the_breakdown(self):
        kpis = views.get_kpis()
        breakdown = self.reference_counts()

        self.assertEqual(kpis["total_active"], sum(t["total"] for t in breakdown.values()))
        self.assertEqual(
            kpis["overdue_count"], sum(t["overdue"] for t in breakdown.values())
        )
        self.assertEqual(
            kpis["overdue_tier_a"], breakdown[Contact.Tier.TIER_A]["overdue"]
        )

    def test_archived_contacts_are_excluded_everywhere(self):
        breakdown = views.tier_validation_breakdown()

        self.assertEqual(breakdown[Contact.Tier.TIER_C]["total"], 0)
        self.assertEqual(views.get_kpis()["total_active"], 3)

    def test_owner_breakdown_totals_match_kpis(self):
        breakdown = views.owner_breakdown()
        kpis = views.get_kpis()

        self.assertEqual(len(breakdown), 1)
        self.assertEqual(breakdown[0]["total"], kpis["total_active"])
        self.assertEqual(breakdown[0]["overdue"], kpis["overdue_count"])

    def test_completion_percentage_is_bounded(self):
        for row in views.owner_breakdown():
            self.assertGreaterEqual(row["completion"], 0)
            self.assertLessEqual(row["completion"], 100)


class TrendMetricTests(TestCase):
    """The dashboard chart offers overdue / validation / status modes."""

    def setUp(self):
        self.owner = User.objects.create_user(username="manager", password="pw")
        self.today = timezone.now()

        make_contact(
            owner=self.owner, email="valid@example.com", tier=Contact.Tier.TIER_A,
            last_validated_at=self.today, status=Contact.Status.ACTIVE,
        )
        make_contact(
            owner=self.owner, email="overdue@example.com", tier=Contact.Tier.TIER_A,
            last_validated_at=self.today - timedelta(days=200),
            status=Contact.Status.ACTIVE,
        )
        make_contact(
            owner=self.owner, email="dormant@example.com", tier=Contact.Tier.TIER_C,
            last_validated_at=self.today - timedelta(days=900),
            status=Contact.Status.INACTIVE,
        )

    def series_map(self, trend):
        return {s["key"]: s["counts"] for s in trend["series"]}

    def test_every_advertised_metric_builds(self):
        for key in TREND_METRICS:
            with self.subTest(metric=key):
                trend = build_trend(12, key)
                self.assertTrue(trend["title"])
                self.assertTrue(trend["series"])
                for entry in trend["series"]:
                    self.assertEqual(len(entry["counts"]), 12, entry["key"])

    def test_unknown_metric_falls_back_to_overdue(self):
        self.assertEqual(build_trend(12, "does-not-exist")["metric_key"], "overdue")

    def test_overdue_mode_matches_the_live_kpi(self):
        trend = build_trend(12, "overdue")
        self.assertEqual(
            trend["series"][0]["counts"][-1], views.get_kpis()["overdue_count"]
        )

    def test_overdue_mode_ignores_inactive_contacts(self):
        # The dormant contact is 900 days past a 365-day SLA but is inactive.
        self.assertEqual(build_trend(12, "overdue")["series"][0]["counts"][-1], 1)

    def test_validation_mode_final_point_matches_the_model(self):
        trend = build_trend(12, "validation")
        counts = self.series_map(trend)
        expected = {"valid": 0, "due_soon": 0, "overdue": 0}
        for contact in Contact.objects.filter(status=Contact.Status.ACTIVE):
            expected[contact.validation_status] += 1

        self.assertEqual(
            {key: values[-1] for key, values in counts.items()}, expected
        )

    def test_validation_mode_excludes_inactive_from_every_bucket(self):
        counts = self.series_map(build_trend(12, "validation"))
        for key, values in counts.items():
            with self.subTest(series=key):
                self.assertEqual(len(values), 12)
        # Only the two active contacts are ever counted; the inactive one is
        # absent from every bucket, including the live point.
        self.assertEqual(sum(v[-1] for v in counts.values()), 2)

    def test_status_mode_final_point_matches_the_database(self):
        counts = self.series_map(build_trend(12, "status"))
        for status in Contact.Status.values:
            with self.subTest(status=status):
                self.assertEqual(
                    counts[status.lower()][-1],
                    Contact.objects.filter(status=status).count(),
                )

    def test_status_mode_is_cumulative_and_loses_nobody(self):
        counts = self.series_map(build_trend(12, "status"))
        for key, values in counts.items():
            with self.subTest(series=key):
                self.assertEqual(values, sorted(values), "not non-decreasing")
        # The 900-day-old contact predates the window, so the final totals must
        # still account for every contact.
        self.assertEqual(sum(v[-1] for v in counts.values()), Contact.objects.count())

    def test_labels_line_up_with_the_series_length(self):
        for key in TREND_METRICS:
            with self.subTest(metric=key):
                trend = build_trend(24, key)
                labels = json.loads(trend["labels_json"])
                self.assertEqual(len(labels), 24)
                self.assertEqual(len(set(labels)), 24, "duplicate month label")

    def test_month_range_is_clamped_for_every_metric(self):
        for key in TREND_METRICS:
            with self.subTest(metric=key):
                self.assertEqual(build_trend(999, key)["months"], 36)
                self.assertEqual(build_trend(1, key)["months"], 2)

    def test_datasets_json_is_shaped_for_chartjs(self):
        trend = build_trend(12, "validation")
        datasets = json.loads(trend["datasets_json"])

        self.assertEqual(len(datasets), len(trend["series"]))
        for dataset, entry in zip(datasets, trend["series"]):
            self.assertEqual(dataset["label"], entry["label"])
            self.assertEqual(dataset["data"], entry["counts"])
            self.assertTrue(dataset["borderColor"].startswith("#"))
            self.assertTrue(dataset["backgroundColor"].startswith("rgba"))

    def test_only_overdue_mode_shows_the_target_line(self):
        self.assertTrue(build_trend(12, "overdue")["show_target"])
        for key in ("validation", "status"):
            self.assertFalse(build_trend(12, key)["show_target"])

    def test_legacy_overdue_trend_wrapper_still_works(self):
        legacy = overdue_trend(12)
        current = build_trend(12, "overdue")

        self.assertEqual(json.loads(legacy["counts_json"]), current["series"][0]["counts"])
        self.assertEqual(legacy["labels_json"], current["labels_json"])
        self.assertEqual(legacy["months"], 12)


class DashboardMetricSwitchTests(TestCase):
    def setUp(self):
        self.user = grant(
            User.objects.create_superuser(username="root", email="r@x.com"),
            "view_contact",
        )
        self.client.force_login(self.user)
        make_contact(owner=self.user)

    def test_dashboard_defaults_to_the_overdue_metric(self):
        response = self.client.get(reverse("dashboard"))
        trend = response.context["trend"]

        self.assertEqual(response.status_code, 200)
        self.assertEqual(trend["metric_key"], "overdue")

    def test_metric_query_parameter_switches_the_chart(self):
        for key in TREND_METRICS:
            with self.subTest(metric=key):
                response = self.client.get(reverse("dashboard"), {"metric": key})
                self.assertEqual(response.context["trend"]["metric_key"], key)
                self.assertContains(response, "overdueChart")

    def test_unknown_metric_parameter_is_ignored(self):
        response = self.client.get(reverse("dashboard"), {"metric": "bogus"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["trend"]["metric_key"], "overdue")

    def test_metric_and_range_combine(self):
        response = self.client.get(
            reverse("dashboard"), {"metric": "status", "range": "6"}
        )
        trend = response.context["trend"]

        self.assertEqual(trend["metric_key"], "status")
        self.assertEqual(trend["months"], 6)
        self.assertContains(response, "metric=overdue&amp;range=6")
        self.assertContains(response, "metric=status&amp;range=6")

    def test_every_metric_advertises_its_own_switcher_options(self):
        response = self.client.get(reverse("dashboard"), {"metric": "validation"})
        for key in TREND_METRICS:
            self.assertContains(response, f"?metric={key}")


class EnvHelperTests(SimpleTestCase):
    """The env parsers in config/settings.py decide how a deploy is configured."""

    def setUp(self):
        patcher = mock.patch.dict(os.environ, {}, clear=False)
        patcher.start()
        self.addCleanup(patcher.stop)
        for key in ("FLAG", "ITEMS"):
            os.environ.pop(key, None)

    def test_env_flag_defaults_when_unset(self):
        self.assertTrue(env_flag("FLAG", default=True))
        self.assertFalse(env_flag("FLAG"))

    def test_env_flag_accepts_common_truthy_spellings(self):
        for raw in ("1", "true", "TRUE", "Yes", " on "):
            with self.subTest(raw=raw):
                os.environ["FLAG"] = raw
                self.assertTrue(env_flag("FLAG"))

    def test_env_flag_rejects_other_values(self):
        for raw in ("0", "false", "no", "off", "", "maybe"):
            with self.subTest(raw=raw):
                os.environ["FLAG"] = raw
                self.assertFalse(env_flag("FLAG"))

    def test_env_list_splits_and_trims(self):
        os.environ["ITEMS"] = " a.com , b.com ,, c.com "
        self.assertEqual(env_list("ITEMS", ["fallback"]), ["a.com", "b.com", "c.com"])

    def test_env_list_falls_back_when_unset_or_blank(self):
        self.assertEqual(env_list("ITEMS", ["fallback"]), ["fallback"])
        os.environ["ITEMS"] = "  ,  "
        self.assertEqual(env_list("ITEMS", ["fallback"]), ["fallback"])

    def test_env_list_never_mutates_the_default(self):
        default = ["fallback"]
        env_list("ITEMS", default)
        self.assertEqual(default, ["fallback"])
