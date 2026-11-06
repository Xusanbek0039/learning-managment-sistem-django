# Security Policy / Xavfsizlik siyosati

🇬🇧 [English](#english) · 🇺🇿 [Özbekça](#özbekça)

## English

IT Creative Academy LMS runs a real training center with student, parent and staff accounts.
Please help us keep their data safe.

### Supported versions

There are no versioned releases yet. Only the latest `main` branch, as deployed to
<https://itc-academy.uz>, receives security fixes.

### Reporting a vulnerability

**Do not open a public issue, pull request or discussion for security problems.**

1. Preferred: GitHub **private vulnerability reporting** on this repository
   (*Security → Report a vulnerability*), if it is enabled.
2. Otherwise, message the maintainer privately on Telegram: <https://t.me/husanbek_coder>.

Please include:

- the affected URL, endpoint or file and line;
- steps to reproduce, and the impact you expect;
- whether any real user data was accessed. Please access as little as possible and delete it afterwards.

We aim to acknowledge reports within **7 days** and to agree on a disclosure date with you.
Please give us up to **90 days** before public disclosure (as stated in `security.txt`).

### Rules for testing

- Test against **your own local installation**, not against itc-academy.uz.
- No denial-of-service, spam, social engineering, physical attacks, or automated scanning of production.
- Never read, change or delete other users' data; never pay with real payment methods for testing.
- Leaked credentials you find in the git history must be reported, not used.

### For maintainers and contributors

- Secrets live only in the server's `.env` (`chmod 600`). `.env.example` contains key names only.
- Never commit databases, `media/` uploads, backups or logs.
- CI runs a secret scan (`.github/workflows/secret-scan.yml`). If it fires, **rotate the secret first**,
  then remove it from history in coordination with the maintainer.

## Özbekça

### Zaiflik haqida xabar beriş

**Xavfsizlik muammosi uçun ommaviy issue / PR oçmang.**

1. Afzal: GitHub *Security → Report a vulnerability* (private reporting yoqilgan bölsa).
2. Yoki maintainer'ga Telegram orqali maxfiy yozing: <https://t.me/husanbek_coder>.

Xabarda: manzil/fayl, qayta takrorlaş qadamlari, ta'sir, real ma'lumotga kiriş bölgan-bölmagani.
7 kun içida javob berişga harakat qilamiz; ommaga oşkor qilişdan oldin 90 kungaça vaqt bering.

### Sinov qoidalari

- Faqat **öz lokal nusxangizda** sinang, itc-academy.uz da emas.
- DoS, spam, ijtimoiy muhandislik, production'ni avtomatik skanerlaş taqiqlanadi.
- Boşqa foydalanuvçilar ma'lumotini öqimang, özgartirmang, öçirmang.
- Git tarixida topilgan kalitlarni işlatmang, xabar bering.
