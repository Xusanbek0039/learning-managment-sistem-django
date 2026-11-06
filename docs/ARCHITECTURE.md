# Architecture

This page describes the code as it is in the repository (Nov 2026). When the code and this page
disagree, the code wins. Please open a `documentation` issue if you notice that.

## Overview

- One Django project, `config/` (settings, root URLs, WSGI/ASGI, PWA, public views such as
  `robots.txt`, `security.txt` and `/media/` serving).
- 14 local apps. `accounts` is by far the largest: ~4,200 lines of models and ~19,000 lines of views.
- Server-rendered HTML (Django templates + Bootstrap + vanilla JS) for the web UI, plus a DRF API
  under `/api/v1/` for the Android/Windows apps and some dynamic pages.
- SQLite database, local-memory/file cache by default (Redis is optional).
- Deployed on cPanel with Phusion Passenger (`passenger_wsgi.py`) behind Nginx (Engintron).

```
Browser ──► Nginx ──► Passenger ──► Django (config.wsgi via passenger_wsgi.py)
Android/Windows apps ──► /api/v1/ (DRF)          │
Telegram ──► /telegrambotlms/ (webhook)           ├─► SQLite (db.sqlite3)
                                                  ├─► media/ (uploads, served by config.views.serve_media)
                                                  └─► OpenAI · Telegram Bot API · Payme · Click · Tezcheck · inPAY · SMTP
```

## Apps

| App | URL prefix | Responsibility |
|-----|-----------|----------------|
| `accounts` | `/` | Users (`CustomUser`), courses (`Profession`), sections, lessons, tests, homework, certificates, payments (Payme/Click/Tezcheck/inPAY), balances, chat, attendance, study groups, surveys, library, security events, middleware, OAuth logins |
| `teacher` | `/teacher/` | Teacher panel: certificates, salary, teacher attendance, study groups |
| `parents` | `/parent/`, `/dashboard/parents/`, `/api/v1/parent-app/` | Parent portal (separate model and login), admin views of parents, parent Android app API |
| `api` | `/api/v1/` | DRF API: profile, courses, enrollments, coin, leaderboard, startup, assistant admin, library, app auth |
| `ai_yordamchi` | `/ai/` | OpenAI chat sessions, AI knowledge base |
| `coin` | `/` | Coin economy, shop, transfers, promo codes |
| `games` | `/games/`, `/api/v1/games/external/` | Typing, word game, board games (chess/checkers), competitions, external games API with HMAC checks |
| `blog` | `/blog/` | Posts, likes, comments |
| `telegram_bot` | `/telegrambotlms/` | Webhook, OTP login, notifications, broadcasts |
| `quiz` | `/quiz/`, `/api/v1/quiz/` | Quiz module (DRF router: questions, quizzes, attempts) |
| `video_quiz` | `/video-quiz/` | Questions shown inside video lessons |
| `kurslarim` | `/kurslarim/` | "My courses" pages |
| `startup` | `/startup/`, `/dashboard/startups/` | Student startup projects and roadmap |
| `backup` | — | `send_backup` command: consistent SQLite snapshot → gzip → Telegram |

## Roles and authorisation

- `accounts.CustomUser.role` ∈ `admin`, `assistant_admin`, `teacher`, `student` (`AUTH_USER_MODEL = 'accounts.CustomUser'`).
- Assistant admins get granular permissions through `AssistantRole` (`accounts/permissions_registry.py`);
  their actions are recorded in `AssistantActionLog`.
- Parents are **not** Django users. They have their own model and session/token handling in `parents/`.
  The parent app uses `Authorization: Parent <token>`, and tokens are stored hashed (`ParentAppToken`).
- Views check roles server-side (e.g. `user.is_admin_or_assistant`). DRF defaults to `IsAuthenticated`.

## Request pipeline (middleware, in order)

From `config/settings.py` → `MIDDLEWARE`:

1. `SecurityMiddleware` (Django)
2. `accounts.middleware.RequestSizeLimitMiddleware`: rejects oversized requests
3. `accounts.middleware.AdminLoginRateLimitMiddleware`: admin login brute-force limit
4. `accounts.middleware.SuspiciousRequestMiddleware`: scans requests for SQLi/XSS/path traversal/SSTI/command-injection patterns and records `SecurityEvent`s
5. `whitenoise.middleware.WhiteNoiseMiddleware`
6. `SessionMiddleware`, `CommonMiddleware`, `CsrfViewMiddleware`, `AuthenticationMiddleware`,
   `MessageMiddleware`, `XFrameOptionsMiddleware` (Django)
7. `SecurityHeadersMiddleware`: CSP and other security headers
8. `MaintenanceMiddleware`, `BlockedUserMiddleware`, `SessionTimeoutMiddleware`, `DeviceLimitMiddleware`,
   `WiFiAutoLogoutMiddleware`, `ProfileCompletionMiddleware`, `SectionBlockMiddleware`,
   `UpdateLastActivityMiddleware`, `PaymentCheckMiddleware`, `BirthdayCheckMiddleware`, `PremiumExpiryMiddleware`

## Background work

There is no Celery. Two mechanisms exist:

- **In-process threads** started in `accounts/apps.py` (`RUN_INPROCESS_WORKERS=true`): AI homework
  checking and the email notification loop. `telegram_bot/apps.py` sets the webhook when the web
  process starts. Both are skipped for `manage.py` commands and tests.
- **Cron** management commands, e.g. `send_backup` (via `install_backup_cron`), `send_email_notifications`,
  `send_payment_reminders`, `check_premium_expiry`, `competition_reminder`. See `docs/performance.md`.

## Data and files

| Path | Content | In git? |
|------|---------|---------|
| `db.sqlite3` | Database | Currently tracked; **must be removed** (see roadmap) |
| `media/` | User uploads (served through `config.views.serve_media`, which forces download for unsafe types) | Should not be; some files are still tracked |
| `static/` | Source static assets | Yes |
| `staticfiles/` | `collectstatic` output, served by Nginx | Currently tracked |
| `logs/` | Rotating logs incl. `security.log` | Ignored |
| `ilovalar/` | Android/Windows app sources and builds | Sources yes; builds should move to GitHub Releases |

## Security building blocks

`accounts/security.py` (rate limiting, `get_client_ip` with trusted proxy count, `sanitize_html`),
`accounts/middleware.py`, `SecurityEvent` logging, Argon2 password hashing, HMAC-signed game results,
signature checks on payment webhooks and a secret-token check on the Telegram webhook.
