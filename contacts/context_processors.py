"""Template context processors shared across every page."""

from .models import Contact


def nav_badges(request):
    """Expose the unresolved-duplicate count to the sidebar on all pages.

    Only counts for authenticated users, and short-circuits when the request
    carries no session so anonymous pages never touch the database.
    """
    if not getattr(request, "user", None) or not request.user.is_authenticated:
        return {"unresolved_duplicates": 0}

    groups = {}
    for c in Contact.objects.only("first_name", "last_name", "company"):
        key = (
            c.first_name.strip().lower(),
            c.last_name.strip().lower(),
            c.company.strip().lower(),
        )
        groups[key] = groups.get(key, 0) + 1

    return {"unresolved_duplicates": sum(1 for n in groups.values() if n > 1)}
