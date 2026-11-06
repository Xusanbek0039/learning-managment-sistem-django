# Contributing / Hissa qöşiş

🇬🇧 [English](#english) · 🇺🇿 [Özbekça](#özbekça)

---

## English

Thank you for helping improve IT Creative Academy LMS. This system runs a real training center
(<https://itc-academy.uz>), so we value **small, well-tested, reviewable changes**.

### Before you start

1. Read the [Code of Conduct](CODE_OF_CONDUCT.md).
2. Security problems go to [SECURITY.md](SECURITY.md), **never** to a public issue.
3. Look for issues labelled [`good first issue`](../../labels/good%20first%20issue),
   [`help wanted`](../../labels/help%20wanted) or [`documentation`](../../labels/documentation).
   Comment on the issue before starting larger work so nobody duplicates it.

### Local setup

Follow [docs/INSTALLATION.md](docs/INSTALLATION.md). In short:

```bash
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt -r requirements-dev.txt
cp .env.example .env    # DEBUG=True, random DJANGO_SECRET_KEY, no real tokens
python manage.py migrate
python manage.py runserver
```

### Workflow

1. Fork the repository and create a branch from `main`:
   `feat/<short-name>`, `fix/<short-name>`, `docs/<short-name>`.
2. Make a focused change. One topic per pull request.
3. Add or update tests (`accounts/tests_*.py` and similar). Bug fixes should include a regression test.
4. Run locally:
   ```bash
   python manage.py check
   python manage.py test <app_or_module>
   ruff check --select E9,F63,F7,F82 .
   ```
5. Use [Conventional Commits](https://www.conventionalcommits.org/) style messages, e.g.
   `fix(attendance): restore Excel export rows`.
6. Open a pull request and fill in the template.

### Rules that protect production

- **Never commit** `.env`, `db.sqlite3`, anything in `media/`, backups, logs, APK/MSI builds or `__pycache__`.
- **Never use real user data** in tests, fixtures, screenshots or issues. Use made-up names and
  phone numbers such as `+998900000001`.
- Migrations: keep them small and backwards compatible. Never edit an already-merged migration.
  Mention every new migration in the PR description.
- Tests must not call external services (Telegram, OpenAI, payment providers, SMTP). Mock them,
  e.g. `@mock.patch('telegram_bot.utils.notify_user', lambda *a, **k: None)`.
- Don't add new third-party services or paid APIs without discussing them in an issue first.
- Keep the existing Uzbek orthography in user-facing strings and comments (`ö`, `ğ`, `ş`, `ç`;
  see `accounts/alphabet.py`). Code identifiers stay in English.

### Code style

- Follow the style of the surrounding code; avoid unrelated reformatting in the same PR.
- Views that change state must use POST with CSRF protection. Check roles and permissions on the server.
- Escape output. Rich text must go through `accounts.security.sanitize_html` / the `safe_html` filter.
- Use the ORM. Raw SQL needs a very good reason and parameterised queries.

---

## Özbekça

IT Creative Academy LMS ni yaxşilaşga yordam berayotganingiz uçun rahmat! Tizim real öquv markazida
işlaydi, şuning uçun **kiçik, testlangan va tekşiriş oson** özgarişlarni qadrlaymiz.

### Boşlaşdan oldin

1. [Xulq-atvor kodeksi](CODE_OF_CONDUCT.md) bilan tanişing.
2. Xavfsizlik muammolari — faqat [SECURITY.md](SECURITY.md) orqali, ommaviy issue'da **emas**.
3. [`good first issue`](../../labels/good%20first%20issue), [`help wanted`](../../labels/help%20wanted),
   [`documentation`](../../labels/documentation) belgili vazifalardan boşlang. Katta iş oldidan issue'ga yozing.

### Ish tartibi

1. Fork → `main` dan branch: `feat/...`, `fix/...`, `docs/...`.
2. Bitta PR — bitta mavzu.
3. Test qöşing yoki yangilang; bug-fix uçun regressiya testi şart.
4. Lokal tekşiruv: `python manage.py check`, `python manage.py test <app>`, `ruff check --select E9,F63,F7,F82 .`
5. Commit xabari: Conventional Commits (`fix(attendance): ...`).
6. PR oçing va şablonni töldiring.

### Production'ni himoyalaydigan qoidalar

- `.env`, `db.sqlite3`, `media/`, backup, log, APK/MSI va `__pycache__` ni **commit qilmang**.
- Test, fixture, skrinşot va issue'larda **real foydalanuvçi ma'lumotini işlatmang** (soxta ism, `+998900000001` kabi raqam).
- Migratsiyalar kiçik va orqaga mos bölsin; merge bölgan migratsiyani tahrirlamang.
- Testlar Telegram, OpenAI, tölov tizimlari va SMTP ga sörov yubormasin — mock qiling.
- Yangi taşqi yoki pullik xizmatni avval issue'da muhokama qiling.
- Foydalanuvçiga körinadigan matnlarda loyiha yozuvini saqlang (`ö`, `ğ`, `ş`, `ç`).
