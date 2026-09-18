from django.urls import path

from . import views

urlpatterns = [
    path("", views.dashboard, name="dashboard"),
    path("contacts/", views.ContactListView.as_view(), name="contact_list"),
    path("contacts/my/", views.MyContactsView.as_view(), name="my_contacts"),
    path("contacts/add/", views.ContactCreateView.as_view(), name="contact_add"),
    path("contacts/<int:pk>/edit/", views.ContactUpdateView.as_view(), name="contact_edit"),
    path("contacts/<int:pk>/delete/", views.ContactDeleteView.as_view(), name="contact_delete"),
    path("contacts/<int:pk>/validate/", views.validate_contact, name="contact_validate"),
    path("events/", views.events, name="events"),
    path("export/csv/", views.export_csv, name="export_csv"),
    path("export/xlsx/", views.export_xlsx, name="export_xlsx"),
    path("duplicates/", views.duplicate_check, name="duplicates"),
]