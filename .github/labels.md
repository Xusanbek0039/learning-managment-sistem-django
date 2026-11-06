# Issue labels

Reference for the maintainer. GitHub already creates most of these labels by default; the commands
below create or update them. **Run them manually**, nothing here runs automatically.

| Label | Colour | Use for |
|-------|--------|---------|
| `good first issue` | `7057ff` | Small, well-described task; a newcomer can finish it in an evening |
| `help wanted` | `008672` | Maintainers would welcome outside help |
| `documentation` | `0075ca` | Docs, README, translations |
| `enhancement` | `a2eeef` | New feature or improvement |
| `bug` | `d73a4a` | Something does not work |
| `security` | `b60205` | Hardening work that is safe to discuss publicly (never undisclosed vulnerabilities) |
| `tests` | `fbca04` | Tests and CI |

```bash
gh label create "good first issue" --color 7057ff --description "Good for newcomers" --force
gh label create "help wanted"      --color 008672 --description "Extra attention is needed" --force
gh label create "documentation"    --color 0075ca --description "Docs, README, translations" --force
gh label create "enhancement"      --color a2eeef --description "New feature or request" --force
gh label create "bug"              --color d73a4a --description "Something isn't working" --force
gh label create "security"         --color b60205 --description "Public hardening work" --force
gh label create "tests"            --color fbca04 --description "Tests and CI" --force
```

## Starter issues (ready to copy)

| Title | Labels |
|-------|--------|
| Fix `test_manifest`: strip `?v=` from icon URLs before `finders.find` | `good first issue`, `tests` |
| Fix `CertificateTelegramNotificationTests`: add `course_name` to the test certificate | `good first issue`, `tests` |
| Fix `GameResultAccountStateTest`: issue the launch token with an `iat` older than `min_time_seconds` | `good first issue`, `tests` |
| Fix `AiCoinHistoryTests` backfill expectation (old vs new orthography) | `good first issue`, `tests` |
| Update Jumanji CSS-decimal test regex for `--jm-aspect` | `good first issue`, `tests` |
| Update the 3 failing Node tests in `tests/` to match current templates | `help wanted`, `tests` |
| `security.txt`: fix `Canonical` URL and add a contact e-mail | `good first issue`, `security` |
| Replace `.extra(where=...)` in the study-group day filter with ORM bitwise expressions | `good first issue`, `enhancement` |
| Add `seed_demo` management command (spec in `docs/DEVELOPMENT.md#demo-data`) | `help wanted`, `enhancement` |
| Translate `docs/BACKUP.md` to English | `documentation`, `good first issue` |
| Add screenshots per role to the README (demo data only) | `documentation`, `help wanted` |
