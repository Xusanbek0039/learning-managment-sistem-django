# Changelog

All notable changes to this project are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/). The project has no versioned
releases or git tags yet; until it does, changes are listed under **Unreleased**.

## [Unreleased]

### Added
- `LICENSE` (MIT). Copyright holder and year still need the maintainer's confirmation.
- `README.en.md` (English README) and a restructured Uzbek `README.md` with corrected facts.
- Community files: `CONTRIBUTING.md`, `CODE_OF_CONDUCT.md`, `SECURITY.md`, `CHANGELOG.md`.
- Documentation: `docs/ARCHITECTURE.md`, `docs/INSTALLATION.md`, `docs/API.md`,
  `docs/DEVELOPMENT.md`, `docs/ROADMAP.md`, `docs/LICENSING.md`, `docs/BACKUP.md`.
- GitHub templates (bug report, feature request, documentation), pull request template, and CI
  workflows for Django tests, linting and secret scanning.
- Regression tests for the group attendance Excel export (`accounts/tests_attendance_export.py`).

### Fixed
- Group attendance Excel export (`attendance_report_excel`) raised `NameError`. The end of the
  function had been cut off since commit `98f84afa` (2026-07-18); it is restored from `ed8b33b2`.

### Security
- Removed a plaintext OpenAI API key from the `README.md` example environment block.
- `.env.example`: removed the placeholder `SECRET_KEY` value, the real backup chat ID and the server path.
- `.gitignore`: ignore local security audit reports and `*.tmp.py` scratch scripts.
