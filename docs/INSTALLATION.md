# Installation

🇬🇧 English below · 🇺🇿 [Özbekça qisqaça](#özbekça-qisqaça)

## Requirements

| Tool | Version | Notes |
|------|---------|-------|
| Python | 3.10+ | Production uses 3.10; 3.12 also works locally |
| pip, venv | recent | |
| Git | any | |
| Node.js | 22+ | Optional, only for the JavaScript unit tests in `tests/` |
| SQLite | bundled with Python | The only database backend the current settings use |

No external service is needed to run the LMS locally. OpenAI, Telegram, SMTP, OAuth and
payment providers are all optional. Leave their variables empty.

## 1. Get the code

```bash
git clone https://github.com/Xusanbek0039/learning-managment-sistem-django.git
cd learning-managment-sistem-django
```

## 2. Virtual environment and dependencies

```bash
python -m venv .venv
# Windows (PowerShell): .venv\Scripts\Activate.ps1
# Windows (cmd):        .venv\Scripts\activate.bat
# Linux / macOS:        source .venv/bin/activate
pip install -r requirements.txt -r requirements-dev.txt
```

`psycopg2-binary` and `redis` are installed but not used unless you enable them.

## 3. Configure `.env`

```bash
cp .env.example .env
python -c "from django.core.management.utils import get_random_secret_key as g; print(g())"
```

Put the generated value into `.env`, together with these local settings:

```env
DEBUG=True
DJANGO_SECRET_KEY=<generated value>
ALLOWED_HOSTS=127.0.0.1,localhost
SITE_URL=http://127.0.0.1:8000
SECURE_SSL_REDIRECT=False
SECURE_HSTS_SECONDS=0
RUN_INPROCESS_WORKERS=false      # no background AI/email threads while developing
```

Keep `TELEGRAM_BOT_TOKEN` **empty** locally. If it is set, `runserver` starts the bot in polling mode
and talks to the real Telegram API.

## 4. Database

The repository still contains a historical `db.sqlite3`. **Do not use it.** In a **fresh clone**
(never on a server or in a deployed folder), start from a clean database:

```bash
# move the tracked file out of the way (do not commit this change)
mv db.sqlite3 db.sqlite3.repo-copy      # Windows: ren db.sqlite3 db.sqlite3.repo-copy
python manage.py migrate
python manage.py createsuperuser
```

Do not run `makemigrations` unless you changed models; all migrations are committed.

## 5. Static files and run

```bash
python manage.py collectstatic --noinput   # optional with DEBUG=True
python manage.py runserver
```

- Site: <http://127.0.0.1:8000>
- Admin: `http://127.0.0.1:8000/<ADMIN_URL>` (`admin/` by default)

To try the different roles, create users in the admin and set `role` to `student`, `teacher`,
`assistant_admin` or `admin`. Parents are created in the parents dashboard (`/dashboard/parents/`).
See [DEVELOPMENT.md](DEVELOPMENT.md#demo-data) for the planned demo-data command.

## 6. Verify

```bash
python manage.py check
python manage.py test accounts.tests_security
```

## Production

The live site runs on cPanel with Phusion Passenger and Nginx (Engintron). Key points:

1. `.env` on the server only, `chmod 600`, never in git. Required: `DEBUG=False`, strong
   `DJANGO_SECRET_KEY`, explicit `ALLOWED_HOSTS` and `CSRF_TRUSTED_ORIGINS`, `SECURE_SSL_REDIRECT=True`,
   `SECURE_HSTS_SECONDS` > 0, and `USE_X_FORWARDED_FOR=True` with the right `TRUSTED_PROXY_COUNT`
   behind a proxy.
2. `passenger_wsgi.py` loads `.env` and serves `config.wsgi`. `.htaccess` sets `PassengerAppRoot`/`PassengerPython`.
   Adjust both paths for your account.
3. Deploy with [`deploy.sh`](../deploy.sh): DB backup → `git pull --ff-only` → `pip install` (if
   requirements changed) → `migrate` → `collectstatic` → Passenger restart.
4. Run `python manage.py check --deploy` after every configuration change.
5. Daily DB backup to Telegram: [BACKUP.md](BACKUP.md). Performance and cache: [performance.md](performance.md).
6. Telegram webhook: set `SITE_URL` and `TELEGRAM_WEBHOOK_SECRET`, then run `python manage.py setup_webhook`.

> ⚠️ The production SQLite file lives inside the git working tree. Read the "untracking" plan in the
> roadmap before removing `db.sqlite3` from git, or a `git pull` may delete the live database.

## Özbekça qisqaça

```bash
git clone https://github.com/Xusanbek0039/learning-managment-sistem-django.git
cd learning-managment-sistem-django
python -m venv .venv && source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements.txt -r requirements-dev.txt
cp .env.example .env        # DEBUG=True, yangi DJANGO_SECRET_KEY, TELEGRAM_BOT_TOKEN böş
mv db.sqlite3 db.sqlite3.repo-copy                      # repodagi bazani işlatmang
python manage.py migrate
python manage.py createsuperuser
python manage.py runserver
```

Taşqi xizmatlar (OpenAI, Telegram, SMTP, tölovlar) lokal işlaş uçun şart emas, ularning kalitlarini böş qoldiring.
