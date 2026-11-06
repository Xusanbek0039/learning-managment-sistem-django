# Licensing

> **Status (2026-11-06):** the `LICENSE` file (MIT) is a **draft awaiting the maintainer's confirmation**
> of the copyright holder and year. Until it is confirmed and published, do not treat the project as
> fully MIT-licensed.

## Project code

- License: MIT ([`LICENSE`](../LICENSE)).
- Copyright holder in the draft: **Husanbek Suyunov**, the only commit author in the git history
  (GitHub account `Xusanbek0039`). Year: **2026**, the year of the first commit (2026-01-15).
- Open questions for the maintainer:
  1. The README footer used to say "© 2024-2026 Suyunov Husan | IT Creative Zomin". Should the
     year range be 2024-2026, and should the holder be the person or the organisation (IT Creative)?
  2. If the code was written as an employee or contractor of the training center, the organisation
     may own the copyright. It must agree to the MIT release.

## Python dependencies (`requirements.txt`)

| Package | License (from package metadata) |
|---------|--------------------------------|
| Django, djangorestframework, python-dotenv, django-redis | BSD-3-Clause / BSD |
| reportlab | BSD |
| django-jazzmin, django-widget-tweaks, openpyxl, youtube-transcript-api, redis, argon2-cffi, whitenoise | MIT |
| pillow | MIT-CMU (HPND) |
| requests, openai | Apache-2.0 |
| psycopg2-binary | LGPL-3.0 with exceptions (dynamic use from Python is fine; not modified or vendored) |

All are compatible with distributing this project under MIT. Their licenses apply to the packages themselves.

## Vendored / bundled assets

| Asset | Origin | License | Action |
|-------|--------|---------|--------|
| `static/fonts/CertBold.ttf` | DejaVu Sans Bold (renamed) | Bitstream Vera / DejaVu license (free, redistributable; the font may not be sold on its own) | Keep. Add the DejaVu license text next to the font |
| `static/fonts/CertScript.ttf` | URW++ "Z003" (2014, base35 fonts) | AGPL-3.0 with font exception (URW base35 release) | Keep with license text, or replace with an OFL script font if you want to avoid AGPL text in the repo |
| `quiz/static/quiz/css/fonts/bootstrap-icons.*` | Bootstrap Icons | MIT | Keep |
| `staticfiles/vendor/*` (AdminLTE, Bootstrap, Bootswatch, Font Awesome Free, Select2) | Bundled by `django-jazzmin` via `collectstatic` | MIT / OFL-1.1 + CC BY 4.0 (Font Awesome Free) | Generated output. Should not be tracked (see roadmap) |
| CDN libraries in templates (Bootstrap, Bootstrap Icons, GSAP, Three.js, Chart.js, Quill, Lenis, marked, DOMPurify) | jsDelivr / cdnjs | MIT, BSD, Apache-2.0, and GSAP's standard "no charge" license | Not redistributed. GSAP is free to use but is **not** OSI open source |
| `docs/inPAY API v2 — hujjatlar.html` and `_files/` | Saved copy of inPAY's documentation | Third-party, all rights reserved | **Remove** from the repository and link to the official docs |
| `static/images/logo.png` and other branding | IT Creative | Trademark / brand of the training center | Exclude from the MIT grant (add a note) or ask forks to replace them |
| `media/` | User uploads | Personal data, not licensable | Remove from git (see SECURITY_AUDIT / roadmap) |
| `ilovalar/**/*.apk`, `*.aab`, `*.msi` | Build outputs | Same as source | Publish via GitHub Releases instead of git |

## Suggested next steps

1. The maintainer confirms the holder and year, then commits `LICENSE`.
2. Add `static/fonts/LICENSE-DejaVu.txt` and `static/fonts/LICENSE-URW-base35.txt`.
3. Add a short "Trademarks" note: the IT Creative name and logo are not covered by the MIT license.
4. Remove the copied inPAY HTML documentation.
