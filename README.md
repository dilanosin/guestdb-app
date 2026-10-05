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
.venv\Scripts\python manage.py seed_users
.venv\Scripts\python manage.py seed_mock_contacts --total=1000
.venv\Scripts\python manage.py createsuperuser
.venv\Scripts\python manage.py runserver
```

Open http://127.0.0.1:8000 and sign in.

Run the test suite before pushing:

```powershell
.venv\Scripts\python manage.py check
.venv\Scripts\python manage.py test
```

## Configuration

Every setting that differs between local development and a real deployment is
read from the environment, so no secret is stored in the repository.

| Variable | Default | Purpose |
| --- | --- | --- |
| `DJANGO_SECRET_KEY` | random per-process | Signing key. **Set this in production** |
| `DJANGO_DEBUG` | `True` | Set `False` to enable the hardening headers |
| `DJANGO_ALLOWED_HOSTS` | `localhost,127.0.0.1,[::1],.app.github.dev` | Comma-separated host allowlist |
| `DJANGO_CSRF_TRUSTED_ORIGINS` | wildcard `*.app.github.dev`, `*.trycloudflare.com` | Comma-separated origins |
| `DJANGO_SECURE_SSL_REDIRECT` | on when `DEBUG=False` | Force HTTPS |
| `DJANGO_SESSION_COOKIE_SECURE` / `DJANGO_CSRF_COOKIE_SECURE` | on when `DEBUG=False` | Secure cookies |
| `DJANGO_SECURE_HSTS_SECONDS` | `0` in dev, `31536000` otherwise | HSTS max-age |

With `DJANGO_DEBUG=False` the app automatically enables SSL redirect, secure
cookies, HSTS, `X-Content-Type-Options: nosniff`, `Referrer-Policy: same-origin`,
and `X-Frame-Options: DENY`.

## Review access (Codespaces)

The app runs live in a GitHub Codespace ready for review:

- **Review URL:** https://guestdb-r76gxr7p4p4xcwq9p-8000.app.github.dev
- **Demo credentials:**

  | Username | Password | Role |
  | --- | --- | --- |
  | `admin` | `admin12345` | Superuser / IT Administrator |
  | `commercial` | `demo12345` | Commercial Manager |
  | `organizer` | `demo12345` | Event Organizer |
  | `itadmin` | `demo12345` | IT Administrator |
  | `manager` | `demo12345` | Account Manager (owns all contacts) |

- The Codespace forwards port 8000 as a **private** port (sign-in required) and
  shuts down after 60 minutes of inactivity. Restart it with
  `gh api -X POST user/codespaces/guestdb-r76gxr7p4p4xcwq9p/start`, then pull the
  latest commit and relaunch the server.
- If a Codespace is deleted and recreated, it gets a new `*-8000.app.github.dev`
  hostname — update the Review URL above accordingly.

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

## Interface

- Layered design system in `contacts/static/contacts/style.css` (CSS custom
  properties, light/dark themes, reduced-motion and print styles)
- Responsive shell: sticky sidebar on desktop, off-canvas drawer with scrim on
  mobile, skip link, visible focus rings, keyboard-navigable menus
- Dashboard: 12-month overdue trend line (switchable to 6/24 months), tier
  validation bars, owner leaderboard
- Custom 403 and 404 pages
- CSV and XLSX exports, CSV event guest-list export