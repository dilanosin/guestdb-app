import json
from datetime import timedelta

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.contrib.auth.mixins import PermissionRequiredMixin
from django.core.paginator import Paginator
from django.db.models import Count, Q
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse_lazy
from django.utils import timezone
from django.views.generic import CreateView, DeleteView, ListView, UpdateView

from .forms import ContactForm
from .models import Contact


def permission_denied(request, exception=None):
    return render(request, "403.html", status=403)


def page_not_found(request, exception=None):
    return render(request, "404.html", status=404)


def tier_validation_breakdown():
    """Per-tier total/validated/overdue counts for active contacts.

    Each tier has a fixed SLA, so overdue is expressed as a plain cutoff
    timestamp (last_validated_at + N days) and evaluated in SQL. That keeps the
    dashboard to a handful of queries instead of one row fetch per contact.
    """
    now = timezone.now()
    breakdown = {}

    for tier in Contact.Tier.values:
        cutoff = now - timedelta(days=Contact.VALIDATION_DAYS[tier])
        qs = Contact.objects.filter(status=Contact.Status.ACTIVE, tier=tier)
        total = qs.count()
        overdue = qs.filter(last_validated_at__lt=cutoff).count()
        breakdown[tier] = {
            "total": total,
            "validated": total - overdue,
            "overdue": overdue,
        }

    return breakdown


def get_kpis():
    tier_validation = tier_validation_breakdown()
    total_active = sum(t["total"] for t in tier_validation.values())
    overdue_total = sum(t["overdue"] for t in tier_validation.values())
    active_valid = total_active - overdue_total

    tier_a = tier_validation[Contact.Tier.TIER_A]
    tier_a_total = tier_a["total"]
    tier_a_valid = tier_a["validated"]

    kpis = {
        "tier_a_total": tier_a_total,
        "tier_a_valid": tier_a_valid,
        "tier_a_pct": round((tier_a_valid / tier_a_total * 100) if tier_a_total else 0, 1),
        "overall_pct": round((active_valid / total_active * 100) if total_active else 0, 1),
        "total_active": total_active,
        "overdue_count": overdue_total,
        "overdue_pct": round((overdue_total / total_active * 100) if total_active else 0, 1),
        "overdue_tier_a": tier_a["overdue"],
        "unowned": Contact.objects.filter(owner__isnull=True).count(),
        "tier_counts": {t: tier_validation[t]["total"] for t in Contact.Tier.values},
        "tier_validation": tier_validation,
        # Every status is always present so templates never KeyError on an
        # empty status.
        "status_counts": {
            status: Contact.objects.filter(status=status).count()
            for status in Contact.Status.values
        },
        "unresolved_duplicates": len(find_duplicates()),
    }
    return kpis


TREND_RANGES = (6, 12, 24)


def _month_anchors(months):
    """Month-start points oldest first, with a final live point at `now`.

    Stepping back a month before seeding means the current month is only
    represented by the live point, so no label ever repeats.
    """
    now = timezone.localtime(timezone.now())
    cursor = (now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
              - timedelta(days=1)).replace(day=1)
    starts = []
    for _ in range(months - 1):
        starts.append(cursor)
        cursor = (cursor - timedelta(days=1)).replace(day=1)
    starts.reverse()
    return starts + [now]


def _due_dates(status=None):
    """(due_at, sla_window) pairs for validation maths."""
    qs = Contact.objects.all() if status is None else Contact.objects.filter(status=status)
    sla = Contact.VALIDATION_DAYS
    return [
        (last_validated_at + timedelta(days=sla[tier]), sla[tier])
        for last_validated_at, tier in qs.values_list("last_validated_at", "tier")
    ]


def _overdue_series(anchors):
    """Cumulative overdue count at each anchor, evaluated with today's status.

    A contact counts as overdue at time T when T is past its
    last_validated_at + tier SLA. Status is evaluated as of today, so a
    contact since archived or deactivated is excluded from every month.
    """
    due_dates = [due for due, _ in _due_dates(Contact.Status.ACTIVE)]
    return [sum(1 for due in due_dates if anchor > due) for anchor in anchors]


def _validation_series(anchors):
    """Active contacts split into valid / due soon / overdue at each anchor.

    Mirrors Contact.validation_status exactly: overdue wins first, then the
    due-soon window (the last 30% of the tier SLA), otherwise valid.
    """
    due_dates = _due_dates(Contact.Status.ACTIVE)
    buckets = {"valid": [], "due_soon": [], "overdue": []}

    for anchor in anchors:
        counts = {"valid": 0, "due_soon": 0, "overdue": 0}
        for due_at, window in due_dates:
            if anchor > due_at:
                counts["overdue"] += 1
            elif (due_at - anchor).days <= int(window * 0.3):
                counts["due_soon"] += 1
            else:
                counts["valid"] += 1
        for key, value in counts.items():
            buckets[key].append(value)

    return buckets


def _month_windows(months):
    """[(start, end_exclusive)] for the last `months` calendar months, oldest first."""
    now = timezone.localtime(timezone.now())
    cursor = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    windows = []
    for _ in range(months):
        nxt = (cursor + timedelta(days=32)).replace(day=1)
        windows.append((cursor, nxt))
        cursor = (cursor - timedelta(days=1)).replace(day=1)
    windows.reverse()
    return windows


def _status_series(windows):
    """Contacts on record by month, split by their current status.

    Status changes are not timestamped, so a true "status as of month X"
    history cannot be reconstructed. Instead this counts every contact
    validated on or before each month, grouped by the status it holds today.
    The series is cumulative, so the final point always equals the live
    status totals and no contact falls outside the window.
    """
    rows = list(Contact.objects.values_list("last_validated_at", "status"))
    keys = [status.lower() for status in Contact.Status.values]
    buckets = {key: [] for key in keys}

    for _, window_end in windows:
        counts = dict.fromkeys(keys, 0)
        for validated_at, status in rows:
            if validated_at < window_end:
                counts[status.lower()] += 1
        for key in keys:
            buckets[key].append(counts[key])

    return buckets


# Chart modes selectable from the dashboard. Each declares its series in draw
# order plus the copy shown above the canvas. `bucketed` marks the modes whose
# x-axis groups events by month rather than reading the state at a point in time.
TREND_METRICS = {
    "overdue": {
        "label": "Overdue",
        "title": "Overdue Trend",
        "sub": "Cumulative overdue active contacts",
        "unit": "overdue",
        "compute": "overdue",
        "bucketed": False,
        "show_target": True,
        "series": (
            {"key": "overdue", "label": "Overdue", "dot": "dot-red",
             "color": "#dc2626", "fill": "rgba(248, 113, 113, 0.15)"},
        ),
    },
    "status": {
        "label": "Status",
        "title": "Contact Status",
        "sub": "Contacts on record by month, split by current status",
        "unit": "contacts",
        "compute": "status",
        "bucketed": True,
        "show_target": False,
        "series": (
            {"key": "active", "label": "Active", "dot": "dot-green",
             "color": "#059669", "fill": "rgba(16, 185, 129, 0.12)"},
            {"key": "inactive", "label": "Inactive", "dot": "dot-slate",
             "color": "#64748b", "fill": "rgba(100, 116, 139, 0.12)"},
            {"key": "archived", "label": "Archived", "dot": "dot-violet",
             "color": "#8b5cf6", "fill": "rgba(139, 92, 246, 0.12)"},
        ),
    },
    "validation": {
        "label": "Validation",
        "title": "Validation Status",
        "sub": "Active contacts by validation standing",
        "unit": "contacts",
        "compute": "validation",
        "bucketed": False,
        "show_target": False,
        "series": (
            {"key": "valid", "label": "Valid", "dot": "dot-green",
             "color": "#059669", "fill": "rgba(16, 185, 129, 0.12)"},
            {"key": "due_soon", "label": "Due soon", "dot": "dot-amber",
             "color": "#d97706", "fill": "rgba(245, 158, 11, 0.12)"},
            {"key": "overdue", "label": "Overdue", "dot": "dot-red",
             "color": "#dc2626", "fill": "rgba(248, 113, 113, 0.15)"},
        ),
    },
}

DEFAULT_TREND_METRIC = "overdue"


def build_trend(months=12, metric=DEFAULT_TREND_METRIC):
    """Assemble everything the dashboard chart needs for one metric."""
    spec = TREND_METRICS.get(metric) or TREND_METRICS[DEFAULT_TREND_METRIC]
    months = max(2, min(int(months or 12), 36))

    # Point-in-time modes read the state at each anchor; bucketed modes group
    # events into calendar months instead.
    if spec["bucketed"]:
        anchors = _month_windows(months)
        labels = [start.strftime("%b %Y") for start, _ in anchors]
    else:
        anchors = _month_anchors(months)
        labels = [a.strftime("%b %Y") for a in anchors]

    calculators = {
        "overdue": _overdue_series,
        "validation": _validation_series,
        "status": _status_series,
    }
    computed = calculators[spec["compute"]](anchors)
    # The single-series calculators return a bare list; normalise to a mapping.
    data = {"overdue": computed} if spec["compute"] == "overdue" else computed

    series = []
    for entry in spec["series"]:
        series.append({**entry, "counts": data.get(entry["key"], [0] * months)})

    primary = series[0]["counts"] if series else [0] * months
    peak = max((max(s["counts"]) for s in series if s["counts"]), default=0)

    return {
        "metric": spec["label"],
        "metric_key": metric if metric in TREND_METRICS else DEFAULT_TREND_METRIC,
        "title": spec["title"],
        "sub": spec["sub"],
        "unit": spec["unit"],
        "show_target": spec["show_target"],
        "bucketed": spec["bucketed"],
        "labels_json": json.dumps(labels),
        "series": series,
        "datasets_json": json.dumps(
            [
                {
                    "label": s["label"],
                    "data": s["counts"],
                    "borderColor": s["color"],
                    "backgroundColor": s["fill"],
                }
                for s in series
            ]
        ),
        "months": months,
        "ranges": TREND_RANGES,
        "metrics": [
            {"key": key, "label": value["label"]}
            for key, value in TREND_METRICS.items()
        ],
        "peak": peak,
        "delta": (primary[-1] - primary[0]) if len(primary) > 1 else 0,
        "delta_abs": abs((primary[-1] - primary[0]) if len(primary) > 1 else 0),
        "total_now": sum(s["counts"][-1] for s in series) if series else 0,
    }


def overdue_trend(months=12):
    """Back-compatible wrapper: the original single-series overdue payload."""
    trend = build_trend(months, "overdue")
    counts = trend["series"][0]["counts"]
    return {
        "labels_json": trend["labels_json"],
        "counts_json": json.dumps(counts),
        "months": trend["months"],
        "ranges": TREND_RANGES,
        "peak": max(counts) if counts else 0,
        "delta": trend["delta"],
        "delta_abs": trend["delta_abs"],
    }


def find_duplicates():
    """Return list of dicts for records sharing the same name+company key."""
    groups = {}
    for c in Contact.objects.all():
        key = (c.first_name.strip().lower(), c.last_name.strip().lower(), c.company.strip().lower())
        groups.setdefault(key, []).append(c)
    return [
        {
            "key": f"{g[0].first_name} {g[0].last_name} — {g[0].company}",
            "names": [c.full_name for c in g],
            "ids": [c.id for c in g],
        }
        for g in groups.values()
        if len(g) > 1
    ]


def owner_breakdown():
    """Contact counts, completion %, overdue per relationship owner."""
    now = timezone.now()

    # One row per (owner, tier), then collapse in Python. Grouping by tier
    # lets overdue be a SQL cutoff instead of a per-contact property call.
    rows = []
    grouped = (
        Contact.objects.filter(status=Contact.Status.ACTIVE)
        .exclude(owner__isnull=True)
        .values("owner_id", "owner__first_name", "owner__last_name", "owner__email", "tier")
        .annotate(total=Count("id"))
        .order_by("owner__first_name", "tier")
    )

    owners = {}
    for item in grouped:
        entry = owners.setdefault(
            item["owner_id"],
            {
                "name": f"{item['owner__first_name']} {item['owner__last_name']}".strip(),
                "email": item["owner__email"],
                "total": 0,
                "overdue": 0,
            },
        )
        cutoff = now - timedelta(days=Contact.VALIDATION_DAYS[item["tier"]])
        bucket = Contact.objects.filter(
            owner_id=item["owner_id"],
            status=Contact.Status.ACTIVE,
            tier=item["tier"],
        )
        total = item["total"]
        overdue = bucket.filter(last_validated_at__lt=cutoff).count()
        entry["total"] += total
        entry["overdue"] += overdue

    for entry in owners.values():
        entry["completion"] = (
            round((entry["total"] - entry["overdue"]) / entry["total"] * 100)
            if entry["total"]
            else 0
        )
        rows.append(entry)

    rows.sort(key=lambda r: r["overdue"], reverse=True)
    return rows


@login_required
def dashboard(request):
    requested = request.GET.get("range")
    trend_months = int(requested) if requested and requested.isdigit() else 12
    metric = request.GET.get("metric") or DEFAULT_TREND_METRIC
    if metric not in TREND_METRICS:
        metric = DEFAULT_TREND_METRIC
    kpis = get_kpis()
    tiers = []
    for label in ["A", "B", "C"]:
        tv = kpis["tier_validation"][label]
        tiers.append(
            {
                "label": label,
                "total": tv["total"],
                "validated": tv["validated"],
                "overdue": tv["overdue"],
                "pct": round((tv["validated"] / tv["total"] * 100) if tv["total"] else 0, 1),
            }
        )
    hour = timezone.localtime(timezone.now()).hour
    if hour < 12:
        greeting = "Good morning"
    elif hour < 18:
        greeting = "Good afternoon"
    else:
        greeting = "Good evening"
    return render(
        request,
        "contacts/dashboard.html",
        {
            "kpis": kpis,
            "tiers": tiers,
            "owners": owner_breakdown(),
            "trend": build_trend(trend_months, metric),
            "unresolved_duplicates": kpis["unresolved_duplicates"],
            "greeting": greeting,
            "today": timezone.localtime(timezone.now()),
        },
    )


class ContactListView(PermissionRequiredMixin, ListView):
    model = Contact
    template_name = "contacts/contact_list.html"
    context_object_name = "contacts"
    paginate_by = 25
    permission_required = "contacts.view_contact"

    def get_queryset(self):
        qs = super().get_queryset().select_related("owner")
        q = self.request.GET.get("q")
        tier = self.request.GET.get("tier")
        status = self.request.GET.get("status")
        if q:
            qs = qs.filter(
                Q(first_name__icontains=q)
                | Q(last_name__icontains=q)
                | Q(company__icontains=q)
                | Q(position__icontains=q)
                | Q(email__icontains=q)
                | Q(owner__username__icontains=q)
                | Q(owner__first_name__icontains=q)
                | Q(owner__last_name__icontains=q)
            )
        if tier:
            qs = qs.filter(tier=tier)
        if status:
            qs = qs.filter(status=status)
        return qs

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["tiers"] = Contact.Tier.choices
        ctx["statuses"] = Contact.Status.choices
        return ctx


class MyContactsView(PermissionRequiredMixin, ListView):
    model = Contact
    template_name = "contacts/contact_list.html"
    context_object_name = "contacts"
    paginate_by = 25
    permission_required = "contacts.view_contact"

    def get_queryset(self):
        qs = Contact.objects.filter(owner=self.request.user).select_related("owner")
        if self.request.GET.get("overdue") == "1":
            qs = qs.filter(status=Contact.Status.ACTIVE)
            ids = [c.id for c in qs.all() if c.is_validation_overdue]
            qs = Contact.objects.filter(id__in=ids)
        return qs

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["is_my_contacts"] = True
        return ctx


class ContactCreateView(PermissionRequiredMixin, CreateView):
    model = Contact
    form_class = ContactForm
    template_name = "contacts/contact_form.html"
    success_url = reverse_lazy("contact_list")
    permission_required = "contacts.add_contact"

    def get_initial(self):
        return {"owner": self.request.user}


class ContactUpdateView(PermissionRequiredMixin, UpdateView):
    model = Contact
    form_class = ContactForm
    template_name = "contacts/contact_form.html"
    success_url = reverse_lazy("contact_list")
    permission_required = "contacts.change_contact"


class ContactDeleteView(PermissionRequiredMixin, DeleteView):
    model = Contact
    success_url = reverse_lazy("contact_list")
    permission_required = "contacts.delete_contact"


@login_required
def validate_contact(request, pk):
    contact = get_object_or_404(Contact, pk=pk)
    if contact.owner != request.user and not request.user.is_superuser:
        from django.contrib.auth.models import Group

        if not request.user.groups.filter(name="Commercial Manager").exists():
            messages.error(request, "Only the owner or custodian may validate this contact.")
            return redirect("contact_list")
    contact.last_validated_at = timezone.now()
    contact.status = Contact.Status.ACTIVE
    contact.save()
    messages.success(request, f"{contact.full_name} validated.")
    return redirect(request.META.get("HTTP_REFERER", "contact_list"))


@login_required
def events(request):
    contacts = Contact.objects.filter(status=Contact.Status.ACTIVE).select_related("owner")
    q = request.GET.get("q")
    tier = request.GET.get("tier")
    company = request.GET.get("company")
    if q:
        contacts = contacts.filter(
            Q(first_name__icontains=q)
            | Q(last_name__icontains=q)
            | Q(company__icontains=q)
            | Q(email__icontains=q)
            | Q(owner__username__icontains=q)
            | Q(owner__first_name__icontains=q)
            | Q(owner__last_name__icontains=q)
        )
    if tier:
        contacts = contacts.filter(tier=tier)
    if company:
        contacts = contacts.filter(company__icontains=company)
    return render(
        request,
        "contacts/events.html",
        {
            "contacts": contacts,
            "tiers": Contact.Tier.choices,
            "picked_tier": tier,
            "picked_company": company,
            "q": q,
        },
    )


@login_required
def export_csv(request):
    contacts = Contact.objects.filter(status=Contact.Status.ACTIVE).select_related("owner")
    response = HttpResponse(content_type="text/csv")
    response["Content-Disposition"] = 'attachment; filename="guest_list.csv"'
    import csv

    writer = csv.writer(response)
    writer.writerow(
        [
            "Full Name",
            "Position",
            "Company",
            "Email",
            "Telephone",
            "Relationship Owner",
            "Strategic Tier",
            "Last Validation Date",
            "Status",
        ]
    )
    for c in contacts:
        writer.writerow(
            [
                c.full_name,
                c.position,
                c.company,
                c.email,
                c.telephone,
                c.owner.get_full_name() or c.owner.username,
                c.tier,
                c.last_validated_at.date().isoformat(),
                c.status,
            ]
        )
    return response


@login_required
def export_xlsx(request):
    from io import BytesIO

    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill
    from openpyxl.utils import get_column_letter

    contacts = Contact.objects.filter(status=Contact.Status.ACTIVE).select_related("owner")
    wb = Workbook()
    ws = wb.active
    ws.title = "Contacts"

    headers = [
        "Name", "Position", "Company", "Email", "Telephone",
        "Relationship Owner", "Strategic Tier", "Last Validation Date",
        "Status", "Validation Overdue",
    ]
    ws.append(headers)
    header_fill = PatternFill(start_color="1E3A8A", end_color="1E3A8A", fill_type="solid")
    header_font = Font(bold=True, color="FFFFFF")
    for col in range(1, len(headers) + 1):
        cell = ws.cell(row=1, column=col)
        cell.fill = header_fill
        cell.font = header_font

    for c in contacts:
        ws.append(
            [
                c.full_name, c.position, c.company, c.email, c.telephone,
                c.owner.get_full_name() or c.owner.username, f"Tier {c.tier}",
                c.last_validated_at.date().isoformat(), c.get_status_display(),
                "Yes" if c.is_validation_overdue else "No",
            ]
        )

    for i, w in enumerate([28, 30, 30, 32, 20, 30, 12, 20, 12, 18], start=1):
        ws.column_dimensions[get_column_letter(i)].width = w
    ws.auto_filter.ref = ws.dimensions
    ws.freeze_panes = "A2"

    buffer = BytesIO()
    wb.save(buffer)
    response = HttpResponse(
        buffer.getvalue(),
        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
    response["Content-Disposition"] = 'attachment; filename="guest_database.xlsx"'
    return response


@login_required
def duplicate_check(request):
    return render(request, "contacts/duplicates.html", {"duplicates": find_duplicates()})