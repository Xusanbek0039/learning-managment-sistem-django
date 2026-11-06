# Roadmap

This roadmap is a plan, not a promise. Items are ordered by priority. Status as of 2026-11-06.

## 0. Security and repository hygiene (blocking open-source adoption)

- [ ] Rotate every credential that was ever committed, then stop tracking secrets, `db.sqlite3`,
      `media/`, `__pycache__/`, `staticfiles/` and build outputs (APK/AAB/MSI).
- [ ] Rewrite git history to purge those files (a one-off, maintainer-run operation with a force push).
- [ ] Move the production SQLite file **outside** the git working tree (`SQLITE_PATH` env variable,
      defaulting to the current path for backwards compatibility).
- [ ] Throttle `/api/v1/token/` like the other login endpoints.
- [ ] Harden or remove the unauthenticated `telegram_bot` `phone-link` upload endpoint.
- [ ] Enable GitHub private vulnerability reporting, Dependabot alerts and secret scanning push protection.

## 1. Maintenance

- [ ] Django 5.1 → **5.2 LTS** (5.1 is end-of-life), Pillow ≥ 13, DRF ≥ 3.18.2 (or the Django 5.2-compatible
      fixed release), requests ≥ 2.33, python-dotenv ≥ 1.2.2. Upgrade on a branch, run the full test suite,
      then deploy at a quiet time.
- [ ] Fix the 6 known stale tests ([DEVELOPMENT.md](DEVELOPMENT.md#known-failing-tests-good-first-issues)).
- [ ] Pin every dependency (`psycopg2-binary` is unpinned) and add a `requirements.lock`/hash file.

## 2. Make it installable by other training centers

These are the main barriers that stop another center from running the LMS today:

| Barrier | Why it blocks | Fix |
|---------|---------------|-----|
| Branding and domain are hard-coded (`itc-academy.uz`, `itcreative.uz` in `_DEFAULT_HOSTS`, logo, texts) | Every fork has to hunt through the code | `SiteConfig` model or settings for name, logo, domain, contacts |
| No demo data | New users see an empty system and cannot evaluate it | `seed_demo` command ([DEVELOPMENT.md](DEVELOPMENT.md#demo-data)) |
| Database fixed to SQLite at `BASE_DIR/db.sqlite3` | Larger centers need PostgreSQL; the DB inside the repo is risky | Read `DATABASE_URL` / `DB_*` env vars with SQLite as the default |
| Deployment documented only for cPanel + Passenger | Most developers use Docker or a VPS | `Dockerfile` + `docker-compose.yml` (Django + optional Redis), plus a gunicorn/nginx guide |
| Uzbek-only UI with a custom orthography | Harder for non-Uzbek contributors | Extend the existing `i18n` app; keep Uzbek as the default |
| Payment providers are Uzbekistan-specific | Fine locally, but must be optional | Feature flags so a center can disable Payme/Click/Tezcheck/inPAY cleanly |
| `accounts/views.py` ≈ 19,000 lines | Hard to review, merge conflicts | Split by feature into modules, gradually and with tests |
| No API schema | App developers must read code | `drf-spectacular` (OpenAPI) once dependencies are upgraded |

## 3. Features (from the original README plans, still open)

- [ ] Real-time chat via WebSockets (Django Channels)
- [ ] Direct video uploads to object storage (S3-compatible)
- [ ] Background tasks with a proper queue instead of in-process threads
- [x] Android apps (student, parent) and a Windows client (`ilovalar/`)
- [x] Optional Redis cache
- [x] Daily database backup to Telegram

---

## Open-source growth strategy

The goal is **genuine** adoption: developers who use, learn from and improve the code. No bought or
bot stars, no fake accounts, no artificial activity. These harm trust and break GitHub's terms.

### Who it is for

1. **Uzbek IT training centers** that want their own LMS.
2. **Students and junior developers in Uzbekistan** who want a real production Django codebase to learn
   from and contribute to (many of them are the center's own students).
3. **Django developers worldwide** interested in specific reusable parts: HTML sanitizer, rate limiting,
   payment integrations for Uzbekistan, Telegram OTP login, anti-cheat for browser games.

### What makes people star and fork (honestly)

- A repository that is **safe to clone**: no secrets, no personal data, and a `migrate` + `seed_demo`
  that works on the first try.
- A clear README in Uzbek and English, screenshots or a short demo video of every role, and a public
  demo instance with demo data only (optional, if hosting allows).
- Small, well-described issues that newcomers can finish in an evening.
- Fast, friendly reviews. Merging a first PR within days matters more than anything else.

### Reusable components to extract later

| Component | Source | Possible package |
|-----------|--------|------------------|
| Payme / Click / Tezcheck / inPAY integration | `accounts/payment_views.py`, `accounts/tezcheck_views.py` | `django-uz-payments` |
| Telegram OTP login and notifications | `telegram_bot/` | `django-telegram-otp` |
| Uzbek alphabet conversion | `accounts/alphabet.py` | `uz-alphabet` |
| HTML sanitizer + rate limit helpers | `accounts/security.py` | (docs or a gist first) |

Extract only after the code has tests and a stable API. Until then, documenting them is enough.

### 30-day plan

| Days | Focus | Concrete actions | Done when |
|------|-------|------------------|-----------|
| 1–3 | **Stop the leaks** | Rotate Telegram, OpenAI, reCAPTCHA, YouTube keys and Django `SECRET_KEY`; untrack secrets, DB and media with the safe server procedure | Secret scan on `main` is clean; the old tokens no longer work |
| 4–7 | **History cleanup** | `git filter-repo` on a mirror clone, force push, GitHub Support request for cached views; enable secret scanning push protection | `gitleaks detect` on the full history is clean |
| 8–10 | **First-run experience** | Implement `seed_demo`; make the DB path configurable; verify `docs/INSTALLATION.md` on a clean Windows and Linux machine | A newcomer runs the LMS in < 15 minutes |
| 11–14 | **Community setup** | Push the docs, templates and CI; create labels; open 10–15 issues (5+ `good first issue`: the 6 stale tests, the `security.txt` fix, the `.extra()` refactor, docs translations); pin a "Welcome contributors" discussion | Issues are visible with clear acceptance criteria |
| 15–18 | **Dependencies** | Django 5.2 LTS, Pillow 13, DRF upgrade on a branch; full test run; deploy | `pip-audit` reports 0 known vulnerabilities |
| 19–22 | **Show it** | Screenshots/GIFs per role in the README; a short Uzbek video walkthrough; a blog post on itc-academy.uz about open-sourcing | README shows the product in under 30 seconds of reading |
| 23–26 | **Reach Uzbek developers** | Share on Uzbek developer Telegram communities and local Python/Django groups, and present to the center's own students as a contribution workshop. Be honest: ask for feedback and contributors, not stars | First external issues or PRs arrive |
| 27–30 | **Review and plan** | Review every PR within 72 h; publish the first tagged release `v0.1.0` with a CHANGELOG; write the next 90-day roadmap based on real feedback | First release exists; contributors know what's next |

### Healthy metrics to watch (not to game)

- Time to first response on issues and PRs
- Number of external contributors with a merged PR
- Successful local installs (ask in a "How did installation go?" discussion)
- Forks that show real commits, not only star counts
