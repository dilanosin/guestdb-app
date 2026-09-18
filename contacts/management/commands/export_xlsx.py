"""Export all contact records to a styled Excel (.xlsx) workbook.

Usage: manage.py export_xlsx [--output path/to/guest_database.xlsx] [--include-inactive]
"""

from django.core.management.base import BaseCommand
from django.utils import timezone

from contacts.models import Contact


class Command(BaseCommand):
    help = "Export contacts to a styled Excel workbook."

    def add_arguments(self, parser):
        parser.add_argument("--output", type=str, default="guest_database.xlsx")
        parser.add_argument(
            "--include-inactive",
            action="store_true",
            help="Include Inactive/Archived contacts as well as Active.",
        )

    def handle(self, *args, **options):
        from openpyxl import Workbook
        from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
        from openpyxl.utils import get_column_letter

        qs = Contact.objects.select_related("owner").order_by("last_name", "first_name")
        if not options["include_inactive"]:
            qs = qs.filter(status=Contact.Status.ACTIVE)

        wb = Workbook()
        ws = wb.active
        ws.title = "Contacts"

        # ---- Palette ----
        navy = "1E3A8A"
        slate = "475569"
        soft_gray = "F1F5F9"
        white = "FFFFFF"
        tier_a = "FDE8E8"   # light red
        tier_b = "FEF3C7"   # light amber
        tier_c = "DBEAFE"   # light blue
        good = "DCFCE7"     # light green
        warn = "FEF3C7"
        ao = "FEE2E2"

        thin = Side(style="thin", color="CBD5E1")
        border = Border(left=thin, right=thin, top=thin, bottom=thin)

        # ---- Title block ----
        ws.merge_cells("A1:J1")
        ws["A1"] = "MASTER GUEST & RELATIONSHIP DATABASE"
        ws["A1"].font = Font(name="Calibri", size=16, bold=True, color=navy)
        ws["A1"].alignment = Alignment(horizontal="left", vertical="center")
        ws.row_dimensions[1].height = 28

        ws.merge_cells("A2:J2")
        stamp = timezone.localtime(timezone.now())
        ws["A2"] = (
            f"Active Contact Directory  |  Exported {stamp:%d %b %Y %H:%M}"
        )
        ws["A2"].font = Font(name="Calibri", size=10, italic=True, color=slate)
        ws.row_dimensions[2].height = 18

        # ---- Header row ----
        headers = [
            "Name",
            "Position",
            "Company",
            "Email",
            "Telephone",
            "Relationship Owner",
            "Strategic Tier",
            "Last Validation Date",
            "Status",
            "Validation Overdue",
        ]
        header_row = 4
        for col, title in enumerate(headers, start=1):
            cell = ws.cell(row=header_row, column=col, value=title)
            cell.font = Font(bold=True, color=white, size=11)
            cell.fill = PatternFill("solid", start_color=navy)
            cell.alignment = Alignment(horizontal="left", vertical="center")
            cell.border = border
        ws.row_dimensions[header_row].height = 22

        # ---- Data rows ----
        start = header_row + 1
        for i, c in enumerate(qs):
            r = start + i
            overdue = c.is_validation_overdue
            row_vals = [
                c.full_name,
                c.position,
                c.company,
                c.email,
                c.telephone or "—",
                c.owner.get_full_name() or c.owner.username,
                f"Tier {c.tier}",
                c.last_validated_at.date().isoformat(),
                c.get_status_display(),
                "Yes" if overdue else "No",
            ]
            for col, val in enumerate(row_vals, start=1):
                cell = ws.cell(row=r, column=col, value=val)
                cell.font = Font(size=10, color="1E293B")
                cell.border = border
            if i % 2 == 1:
                for col in range(1, len(headers) + 1):
                    ws.cell(row=r, column=col).fill = PatternFill("solid", start_color=soft_gray)

            tier_cell = ws.cell(row=r, column=7)
            tier_fill = {"A": tier_a, "B": tier_b, "C": tier_c}[c.tier]
            tier_cell.fill = PatternFill("solid", start_color=tier_fill)
            tier_cell.font = Font(size=10, bold=True)

            status_cell = ws.cell(row=r, column=9)
            if c.status == Contact.Status.ACTIVE:
                status_cell.fill = PatternFill("solid", start_color=good)
            else:
                status_cell.fill = PatternFill("solid", start_color=warn)

            overdue_cell = ws.cell(row=r, column=10)
            if overdue:
                overdue_cell.fill = PatternFill("solid", start_color=ao)
                overdue_cell.font = Font(size=10, bold=True, color="B91C1C")

        # ---- Column widths + freeze + filter ----
        for col, w in enumerate([28, 30, 30, 32, 20, 30, 12, 20, 12, 18], start=1):
            ws.column_dimensions[get_column_letter(col)].width = w
        ws.auto_filter.ref = f"A{header_row}:J{start + len(qs) - 1}"
        ws.freeze_panes = "A5"

        wb.save(options["output"])
        self.stdout.write(
            self.style.SUCCESS(
                f"Exported {qs.count()} contacts to {options['output']} "
                f"(as of {stamp:%Y-%m-%d %H:%M})"
            )
        )