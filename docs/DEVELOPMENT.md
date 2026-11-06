# Development guide

## Daily workflow

```bash
source .venv/bin/activate          # Windows: .venv\Scripts\activate
git switch -c fix/short-name
python manage.py runserver
# …edit…
python manage.py check
python manage.py test <app_or_module>
ruff check --select E9,F63,F7,F82 .
```

See [CONTRIBUTING.md](../CONTRIBUTING.md) for branch names, commit messages and PR rules.

## Project conventions

- **Language:** user-facing text and comments are Uzbek, written in the project's own Latin orthography
  (`ö`, `ğ`, `ş`, `ç`). `accounts/alphabet.py` and the `convert_alphabet` command handle conversion.
  Identifiers are English.
- **Where code lives:** most web views are in `accounts/views.py` (very large). Prefer new modules
  (as `accounts/certificates.py`, `accounts/course_bundles.py` and `accounts/public_courses.py` do)
  over growing that file.
- **Roles:** check permissions on the server (`user.role`, `user.is_admin_or_assistant`, assistant
  permissions via `accounts/permissions_registry.py`). Never rely on hidden buttons.
- **HTML safety:** templates auto-escape. Rich text uses `|safe_html` (whitelist sanitizer in
  `accounts/security.py`). Pass data to JS with `json_script`.
- **Rate limiting:** `accounts.security.check_rate_limit(request, key, identifier=..., ip_limit=..., id_limit=..., window=...)`.
- **Client IP:** always use `accounts.security.get_client_ip`. It honours `TRUSTED_PROXY_COUNT`.

## Tests

The suite has 1,400+ Django tests (`*/tests*.py`, `*/test_*.py`) and a few Node tests in `tests/`.

```bash
python manage.py test                        # everything (takes ~15 min on a laptop)
python manage.py test accounts.tests_security
node --test "tests/*.test.cjs"
```

`config/settings.py` sets `RUNNING_TESTS` when `test` is in `argv`. Telegram bot start-up, Redis and
the file cache are then disabled, and the test runner uses an in-memory SQLite database.

### Running tests fully offline (recommended)

If your `.env` contains any real token, use a throwaway settings module **outside the repository**,
so tests can never reach real services or your real files:

```python
# ~/lms_test_settings/offline_settings.py
import os, secrets, socket
for k in ("OPENAI_API_KEY", "TELEGRAM_BOT_TOKEN", "EMAIL_HOST_PASSWORD", "TEZCHECK_API_TOKEN",
          "INPAY_SECRET_KEY", "RECAPTCHA_SECRET_KEY", "GOOGLE_CLIENT_SECRET", "FACEBOOK_APP_SECRET",
          "GITHUB_CLIENT_SECRET", "YOUTUBE_API_KEY", "CLICK_SECRET_KEY"):
    os.environ[k] = ""
os.environ["TELEGRAM_BOT_TOKEN"] = "123456:FAKE_TOKEN_FOR_TESTS"   # some tests derive secrets from it
os.environ["DJANGO_SECRET_KEY"] = "test-" + secrets.token_urlsafe(32)

_real_connect = socket.socket.connect
def _local_only(self, addr):
    if isinstance(addr, tuple) and addr[0] not in ("127.0.0.1", "::1", "localhost"):
        raise OSError(f"network blocked in tests: {addr[0]}")
    return _real_connect(self, addr)
socket.socket.connect = _local_only

from config.settings import *  # noqa
MEDIA_ROOT = os.path.join(os.path.dirname(__file__), "media")
EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"
```

```bash
PYTHONPATH=~/lms_test_settings DJANGO_SETTINGS_MODULE=offline_settings python manage.py test
```

### Known failing tests (good first issues)

On 2026-11-06 the full suite gave 1429 tests, with 6 failures that come from tests lagging behind
recent code changes (not production bugs):

| Test | Cause |
|------|-------|
| `accounts.tests_pwa.PwaEndpointsTests.test_manifest` | Icon URLs now end with `?v=<hash>`; the test passes the query string to `finders.find` |
| `accounts.tests.CertificateTelegramNotificationTests` | Code now reads `certificate.course_name`; the test's `SimpleNamespace` lacks it |
| `accounts.tests_security.GameResultAccountStateTest` (2) | Anti-cheat now checks time since the launch token's `iat`; the test submits immediately |
| `ai_yordamchi.tests.AiCoinHistoryTests.test_missing_ai_history_is_backfilled_without_duplicates` | Migration `0007` writes the old-orthography reason (`yordamchi`), the test expects `yordamçi` |
| `games.tests.JumanjiViewTests.test_room_cell_coordinates_use_css_decimals` | The template now has `--jm-aspect` between `--jm-cell` and `padding-top`; the regex needs updating |

JavaScript (`node --test "tests/*.test.cjs"`): 8 tests, 3 failing: `coin_discounts`, `video_mobile` and
`video_timer`. They extract functions from templates whose markup has changed since the tests were written.

## Demo data

Never copy production data to a development machine. The planned approach is a
`seed_demo` management command (tracked in the roadmap) with these rules:

1. **Refuses to run** unless `DEBUG=True`, and only on an empty database or one that already
   contains nothing but demo users, unless `--force` is given.
2. Creates obviously fake accounts: `demo_admin`, `demo_assistant`, `demo_teacher1..2`,
   `demo_student1..10`, plus `demo_parent1..3` linked to students. Phone numbers come from a reserved
   range such as `+99890000xxxx`. Each run prints a random password.
3. Creates one or two courses (`Profession`) with sections, a video lesson (public YouTube URL), a
   homework and a short test, a `StudyGroup` with attendance for the current month, a few coin
   transactions, shop products and a blog post.
4. Generates no files in `media/` except placeholder images created with Pillow.
5. Is idempotent: running it twice does not duplicate data (`get_or_create` on usernames and slugs).

Until it exists, create users through `/admin/` and set their `role`.

## Migrations

- One migration per logical change. Make it backwards compatible (add nullable fields first, and
  backfill in a separate data migration).
- Test on a **copy** of a database, never on a live file: `cp db.sqlite3 /tmp/copy.sqlite3`, then use
  a settings module that points `DATABASES['default']['NAME']` at the copy.
- Mention every migration in the PR description; the deploy script runs `migrate --noinput`.

## Linting

CI runs `ruff check --select E9,F63,F7,F82` (syntax errors and undefined names only). Stricter rules
will be enabled gradually; please don't mix mass reformatting with functional changes.
