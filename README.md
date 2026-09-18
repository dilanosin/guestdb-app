# Master Guest & Relationship Database (Django)

Custom Django web application implementing the **Master Guest & Relationship
Database** PRD / Technical Design (v3.0). Replaces spreadsheet/nocode tooling
with a single source of truth for strategic relationships, stakeholders,
clients, and partners for SEOGS, VIP client networking, and corporate events.

## Stack

- Python 3.12 / Django 6.1 (SQLite for dev, PostgreSQL for prod)
- Django Templates + CSS (dashboard with Chart.js)
- `Faker` for synthetic data generation
- `openpyxl` for Excel exports

## Roles (Django Groups)

| Role | Scope |
| --- | --- |
| Commercial Manager | Custodian - full admin, duplicate resolution, KPI audit |
| Account Managers | Relationship Owners - maintain/validate contacts |
| Event Organizers | Full add/change/delete/view - generate guest lists & maintain contacts |
| IT Administrator | Superuser / platform administration |

## Quickstart

```powershell
# Windows PowerShell
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
.venv\Scripts\python manage.py migrate
.venv\Scripts\python manage.py setup_roles
.venv\Scripts\python manage.py seed_mock_contacts --total=1000
.venv\Scripts\python manage.py createsuperuser
.venv\Scripts\python manage.py runserver
```

Open http://127.0.0.1:8000 and sign in.

## Scheduled workflows

Run via cron / Task Scheduler / Celery Beat:

| Command | Frequency | Purpose |
| --- | --- | --- |
| `manage.py audit_overdue` | Daily | Flags overdue contacts, escalates Tier A stake SLO |
| `manage.py owner_digest` | Weekly | Consolidated validation digest email per owner |

Email uses the console backend by default; configure `EMAIL_BACKEND` for real
delivery.

## Data model highlights (`contacts.models.Contact`)

- `Tier`: A (Quarterly/90d), B (Semi-Annual/180d), C (Annual/365d)
- `Status`: Active / Inactive / Archived (archived records are preserved)
- `email` unique (strict de-duplication)
- `owner` FK to user (PROTECT; no contact without an owner)
- `is_validation_overdue` computed from tier threshold
- `validation_status`: valid / due soon / overdue

## KPIs tracked

- 100% Tier A validated
- 95% of active contacts validated
- 0 contacts without an owner
- 0 unresolved duplicates