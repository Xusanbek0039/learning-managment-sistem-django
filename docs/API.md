# REST API

Generated from `api/urls.py` and the included URL modules (Nov 2026). There is no OpenAPI schema
yet (see the roadmap). Response formats below are a summary; read the view code for exact fields.

## Basics

| Item | Value |
|------|-------|
| Base path | `/api/v1/` |
| Authentication | `Authorization: Token <key>` (DRF `TokenAuthentication`) or a browser session |
| Default permission | `IsAuthenticated` (`REST_FRAMEWORK` in `config/settings.py`) |
| Pagination | `PageNumberPagination`, 20 items per page (`?page=N`) |
| Renderer | JSON only |
| Throttling | `coin_transfer`: 10/min. App login endpoints use `accounts.security.check_rate_limit` |

Session-authenticated requests also go through `SessionTimeoutMiddleware` and `DeviceLimitMiddleware`.

### Getting a token

```http
POST /api/v1/token/
Content-Type: application/json

{"username": "student_demo", "password": "********"}
```

```json
{"token": "<40-character key>"}
```

The mobile apps use `/api/v1/app/login/` instead. It is rate-limited and records the device.

## Core endpoints (`api/urls.py`)

| Method | Path | Notes |
|--------|------|-------|
| POST | `token/` | DRF `obtain_auth_token` |
| GET | `status/` | API status (authenticated, not blocked) |
| POST | `app/login/` | App login (rate-limited) |
| POST | `app/auth/start/`, `app/auth/poll/` | Start and poll a confirmed app sign-in (rate-limited) |
| POST | `app/location/` | Location recorded with user consent after app login (authenticated) |
| POST | `app/web-login/` | One-time link so the app can open the website without logging in again |
| GET/PUT | `profile/`, `profile/complete/` | Current user profile |
| GET | `courses/`, `courses/<id>/`, `courses/<id>/sections/`, `courses/<id>/lessons/` | Courses |
| GET | `my/enrollments/`, `my/test-results/`, `my/certificates/` | Current student's data |
| GET | `admin/students/` | Admin only |

## Student app (`api/app_urls.py`, prefix `app/`)

`courses/<pk>/lessons/`, `lessons/<pk>/`, `lessons/<pk>/video/beat/`, `lessons/<pk>/video/complete/`,
`lessons/<pk>/speaking/`, `lessons/<pk>/test/start|submit|result/`, `video-quiz/submit/`,
`lessons/<pk>/homework/submit|edit|history|messages|messages/send/`, `homework/`, `schedule/`,
`messages/`, `messages/<pk>/read/`, `chat/conversations|sidebar|unread/`, `chat/users/search/`,
`chat/group/<profession_pk>/messages|send/`, `chat/direct/<user_pk>/messages|send/`.

## Parent app (`parents/app_urls.py`, prefix `parent-app/`)

Header: `Authorization: Parent <token>`. Tokens are stored hashed.

`login/`, `logout/`, `password-reset/`, `me/`, `me/password/`, `children/<id>/`,
`children/<id>/lessons|homework|tests|attendance|payments|pay|achievements|books/`,
`messages/`, `notifications/`.

## Modules

| Prefix | Endpoints |
|--------|-----------|
| `coin/` | `summary/`, `market/stats/`, `products/`, `products/<pk>/`, `products/<pk>/comments/`, `products/<pk>/comments/add/`, `products/<pk>/like/`, `products/<pk>/purchase/`, `my-purchases/`, `purchases/<pk>/cancel/`, `transactions/`, `activities/`, `premium/plans/`, `premium/request/`, `transfer/search/`, `transfer/initiate/`, `transfer/confirm/` |
| `reyting/` | `` (leaderboard) |
| `startup/` | `top/`, `me/`, `<slug>/`, `<slug>/progress|tasks|team|metrics|finance|pitch/` |
| `library/` | `stats/`, `books/`, `books/check/`, `books/<pk>/`, `books/<pk>/request/`, `my/requests/`, `my/requests/<pk>/cancel/`, `my/rentals/`, `admin/books/…`, `admin/requests/…/approve|reject/`, `admin/rentals/…/return/` |
| `assistant/` | Assistant-admin API: `dashboard/`, `permissions/`, `roles/`, `action-logs/`, `users/…`, `homeworks/`, `homework-submissions/…`, `test-results/…`, `lessons-analytics/`, `attendance/…`, `certificates/`, `parents/…`, `parent-messages/…`, `parent-password-resets/…`, `enrollment-requests/…`, `unenroll-requests/…`, `premium-requests/…`, `messages/…`, `blog/posts|comments/…`, `books/…`, `book-rentals/…`, `help-requests/…`, `applications/…`, `surveys/…`, `survey-questions/…`, `survey-responses/`. Each endpoint checks the assistant's `AssistantRole` permissions |
| `quiz/` | DRF router: `questions/`, `quizzes/`, `attempts/` |

## External games (`games/external_api.py`, prefix `games/external/`)

For third-party HTML5 games embedded in the LMS. These endpoints are CSRF-exempt and use short-lived
launch tokens and HMAC-signed results instead of sessions.

| Method | Path |
|--------|------|
| POST | `verify/` (body: `{"token": "<launch_token>", "game": "<slug>"}`) |
| POST | `result/` |
| POST/GET | `mp/find/`, `mp/state/`, `mp/ping/`, `mp/move/`, `mp/resign/`, `mp/room/` |
| GET/POST | `gx/profile/`, `gx/complete/`, `gx/leaderboard/` |

## Webhooks (not under `/api/v1/`)

Payment providers (Payme, Click, Tezcheck, inPAY) and Telegram call dedicated CSRF-exempt endpoints
that verify signatures or secret tokens. See `accounts/payment_views.py`, `accounts/tezcheck_views.py`,
`telegram_bot/views.py`, `docs/TEZCHECK_SETUP.md` and `docs/INPAY_SETUP.md`.
