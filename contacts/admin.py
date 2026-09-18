from django.contrib import admin

from .models import Contact


@admin.register(Contact)
class ContactAdmin(admin.ModelAdmin):
    list_display = (
        "full_name",
        "position",
        "company",
        "email",
        "owner",
        "tier",
        "status",
        "last_validated_at",
        "is_validation_overdue",
    )
    list_filter = ("tier", "status", "owner")
    search_fields = ("first_name", "last_name", "company", "email")
    readonly_fields = ("created_at", "updated_at", "is_validation_overdue")

    @admin.display(boolean=True, description="Overdue")
    def is_validation_overdue(self, obj):
        return obj.is_validation_overdue