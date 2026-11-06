# Operatsion qöllanma: tezlik va kunlik DB backup

> Bu bölim avval `README.md` oxirida edi; mazmuni özgartirilmagan, faqat server yöllari,
> foydalanuvçi nomlari va Telegram chat ID `<user>`, `<app>`, `<BACKUP_CHAT_ID>` bilan almaştirildi.
> ⚠️ Backup'lar Telegram kanaliga bot orqali yuboriladi — bot tokenini faqat serverdagi `.env` da saqlang;
> token oşkor bölsa kanal tarixidagi backup'larni ham xavf ostida deb hisoblang.

## Sayt tezligini oşiriş: Redis, query, Nginx va Passenger

Staging testlari, har bosqiçdan keyin RPS/TTFB ölçovi, cPanel/Engintron
konfiguratsiya template'lari va rollback amallari: **[docs/performance.md](docs/performance.md)**.
Mavjud SQLite saqlanadi. Redis default öçiq; avval hostingda mavjudligini
tekşiring. Shaxsiy/CSRF sahifalar umumiy Nginx cache'ga qöyilmaydi.

```bash
python -m pip install -r requirements.txt
python manage.py check --settings=settings.production
python manage.py test accounts.test_performance config.test_public_cache blog.tests backup.tests telegram_bot.test_startup
python scripts/benchmark_http.py https://STAGING_DOMAIN/ --requests 50 --concurrency 5
```

Production profili uçun `.env`da `DEBUG=False` va amaldagi secret/host sozlamalari
kerak. Haqiqiy `.env` özgartirilmagan. Production deploy/yuklama testi bajarilmagan.

## Har kuni DB backup: Telegram (Ubuntu staging)

### cPanelda avtomatik yuborişni bir marta yoqiş

Fayllarni serverga yuklaş cron jadvalini özi örnatmaydi. Quyidagi installer
serverdagi loyiha egasining crontab'iga alohida boşqariladigan blok qöşadi:

```bash
source /home/<user>/virtualenv/<app>/3.10/bin/activate
cd /home/<user>/<app>
python manage.py test backup.test_scheduler --verbosity 2
python manage.py install_backup_cron --preview
python manage.py install_backup_cron --backup-dir /home/<user>/private_backups
crontab -l
```

`.env`dagi mavjud `TELEGRAM_BOT_TOKEN` va
`BACKUP_TELEGRAM_CHAT_ID=<BACKUP_CHAT_ID>` işlatiladi. Haqiqiy `.env` repositoryda
yaratilmaydi. Installer server timezone'iga boğliq bölmagan `* * * * *` jadval
qöyadi: kiçik `backup/scheduler.py` har daqiqada **Django yuklamasdan** vaqtni
tekşiradi. Faqat `settings.TIME_ZONE` böyiça 02:00 da `send_backup`ni çaqiradi.
Hozir loyiha timezone'i `Asia/Tashkent`. Server UTC bölsa ham şu vaqtda işlaydi.
02:00 dan keyin örnatilsa birinçi avtomatik backup ertasi kuni yuboriladi.
Cron xizmati işlaşi va hostingda Cron Jobs ruxsati bölişi şart. Server
02:00 da öçiq yoki cron töxtagan bölsa ötkazib yuborilgan iş avtomatik
qoplanmaydi. Installer faqat yozilgan jadvalni tekşiradi, cron daemonini yoqmaydi.

Installer boşqa cronlarni saqlaydi, öz blokini qayta örnatganda dublikat
yaratmaydi. Oldingi qölda yozilgan backup cron mavjud bölsa, dublikat
yaratmasdan töxtaydi: `crontab -e`da faqat eski backup qatorini olib taşlab,
installerni qayta işlating. Oldingi crontab private nusxasi installer çiqargan
yölda saqlanadi. Unda boşqa işlar sirlari bölişi mumkin, nusxani çatga yubormang.

Hozir scheduler orqali haqiqiy backup yuborişni tekşiriş:

```bash
python backup/scheduler.py --backup-dir /home/<user>/private_backups --timezone Asia/Tashkent --run-now
echo $?
```

Installer körsatgan `schedule.log`dan natijani öqing. Log fayli 1 MB x 3 bilan
çeklangan; muvaffaqiyat yoki exit code yoziladi. Telegramga batafsil xavfsiz
xabarni mavjud `send_backup` yuboradi. `--run-now` şu kun uçun avtomatik
muvaffaqiyat belgisini ham qöyadi; ertasi kuni yana 02:00 da yuboriladi.
Oddiy jadval bir kunda bir marta muvaffaqiyat belgisini yozadi, lock ikki
schedulerning birga işlaşiga yöl qöymaydi. Barça Telegram qismlari va yakuniy
xabar kelganini tekşiring. Şu daqiqada backup allaqaçon mavjud bölsa keyingi
daqiqada sinang. Xato bilan tugagan scheduled backup öşa kuni qayta urinmaydi;
sababini tuzatib `--run-now` işlating.

Öchirish/rollback: `crontab -e`da installerga tegişli `BEGIN LMS BACKUP ...`
va `END LMS BACKUP ...` orasidagi blokni şu markerlari bilan olib taşlang.
Butun eski crontabni qayta tiklaş boşqa yangi işlarni yöqotişi mumkin;
boşqa cronlarga tegmang. Ilova/Passenger restart talab qilinmaydi.

Tekşirilgan joylaşuvlar: `manage.py`, `config/settings.py`, `config/urls.py`,
`requirements.txt`, `passenger_wsgi.py`, `.htaccess`, asosiy `README.md`.
`config.settings` va Passenger `.env`ni mavjud `python-dotenv` bilan öqiydi;
qöşimça Python dependency kerak emas. Backup uçun HTTP URL/webhook yoki
Passenger/Nginx sozlamalarini özgartiriş kerak emas; cron alohida Python commandni işlatadi.
Serverga ulanilmadi, deploy yoki haqiqiy Telegram upload bajarilmadi.

**Faol settings SQLite işlatadi va backup uni qöllaydi.** Bazani almaştiriş
kerak emas. SQLite online backup API orqali vaqtinçalik izçil nusxa olinadi
(WAL'dagi commit qilingan yozuvlar ham kiradi), undan SQL dump va `.sql.gz`
yaratiladi. Işlayotgan `db.sqlite3` fayli oddiy copy qilinmaydi.
100 MB va undan katta baza ham qöllanadi: limit asl DBga emas, yuboriladigan
siqilgan fayl/qismlarga tegişli. Siqiliş nisbati ma’lumotga boğliq;
49 000 000 baytdan katta gzip avtomatik qismlanadi.
README'dagi DB env misollari öz-özidan `DATABASES`ni özgartirmaydi.
Command PostgreSQL va MySQL/MariaDB backendlarini ham aniqlaydi.
`ENGINE`, `NAME`, `USER`, `PASSWORD`, `HOST`, `PORT`, qöllanadigan `OPTIONS`
Django sozlamasidan olinadi. Connection string örniga alohida maydonlardan foydalaning.
PostgreSQL uçun `sslmode`, `sslcert`, `sslkey`, `sslrootcert`, `connect_timeout`
uzatiladi. MySQL `charset`, `unix_socket`ni uzatadi; `init_command` va
`isolation_level` Django sessioniga tegişli. Boşqa OPTIONS, MySQL SSL yoki
PostgreSQL service mapping kerak bölsa command xavfsiz töxtaydi: avval mappingni
stagingda moslaştiring. MySQL dump InnoDB uçun `--single-transaction` işlatadi;
MyISAM va parallel DDL paytida izçillik uçun maintenance/DB lock rejasini tuzing.
DB userga dump qilinadigan barça obyektlar, routines/events uçun zarur ruxsatlar kerak.

1. Staging Ubuntu'da şu loyiha virtualenv'ini işlating. SQLite uçun Pythonning
   standart `sqlite3` moduli yetarli, taşqi dump binary kerak emas.
   PostgreSQL/MySQL işlatilsa DBga mos client
   (`postgresql-client`, yoki `mysql-client`/`mariadb-client`) örnating.
   `pg_dump` major versiyasi DB serveridan eski bölmasin.
   MySQL backend uçun serverdagi Django DB driver ham örnatilgan bölişi kerak.
   Mavjud `.env`ga `.env.example`dagi `BACKUP_*` qiymatlarini administrator kiritsin;
   token faqat serverdagi himoyalangan `.env`da saqlansin (`chmod 600 .env`).
   Mavjud `TELEGRAM_BOT_TOKEN` avtomatik işlatiladi. Uni takrorlaş şart emas:
   `BACKUP_TELEGRAM_BOT_TOKEN`ni böş qoldiring yoki umuman kiritmang.
   Alohida backup bot kerak bölsagina şu override qiymatini töldiring.
   Test uçun alohida staging kanal/token işlating; production kanal ID namunasi
   `<BACKUP_CHAT_ID>`, bot admin bölib hujjat va xabar yubora olişi kerak.

2. Backup papkasini web document root, `media` va `static`dan taşqarida,
   cron işlaydigan foydalanuvçi nomidan yarating:

   ```bash
   install -d -m 700 /home/<user>/private_backups
   df -h /home/<user>/private_backups
   df -i /home/<user>/private_backups
   quota -s
   du -sh /home/<user>/private_backups
   ```

   `/`dagi 3.5 TB umumiy va 1.4 TB böş joy hosting hisobining quota'si emas.
   Tarifdagi 3 GB çegarani cPanel Disk Usage va hosting administratoridan aynan
   backup papkasi joylaşgan filesystem, user/project quota va inode böyiça
   tasdiqlang. `quota -s` işlamasa yoki quota körinmasa uni çeksiz deb hisoblamang.
   377 GiB RAM/233 GiB available ham disk quota'sini oşirmaydi.
   `BACKUP_MAX_DISK_MB=1024` faqat backup katalogi uçun namunaviy limit;
   hisobning qolgan fayllari, DB va loglarini ham 3 GB içida hisoblang.
   `BACKUP_MIN_FREE_MB=100` filesystem böş joy zaxirasi, user quota zaxirasi emas.
   Haqiqiy quota buzilsa yoziş xatosi commandni töxtatadi.

3. Sozlamalar (MB önlik: 1 MB = 1 000 000 bayt):

   | Qiymat | Vazifasi |
   |---|---|
   | `BACKUP_DIR` | Mutlaq, symlink bölmagan, `700` huquqli yopiq katalog |
   | `BACKUP_RETENTION_DAYS` | Saqlaş kunlari, musbat son; default 7 |
   | `BACKUP_MAX_DISK_MB` | Şu papkadagi barça fayllar hajmi çegarasi; default 1024 |
   | `BACKUP_MIN_FREE_MB` | Filesystemda böş qolişi kerak bölgan joy; default 100 |
   | `BACKUP_DUMP_TIMEOUT` | Dump uçun timeout sekundda; default 1800 |
   | `BACKUP_HTTP_TIMEOUT` | HTTP socket amallari timeout'i sekundda; default 120 |
   | `BACKUP_PG_DUMP`, `BACKUP_MYSQLDUMP` | Binary nomi yoki mutlaq yöli; argumentlar yozmang |

   SQL avval vaqtinçalik disk fayliga yoziladi, keyin gzip qilinadi. Şu sabab
   raw SQL + gzip + bitta 49 MB qismga joy hisoblang. SQLite uçun izçil snapshot
   + raw SQL bir vaqtda ham joy egallaydi; SQL hajmi asl DBdan kattaroq bölişi
   mumkin (ayniqsa BLOB ma’lumotlarda). Har blok yozilişidan oldin
   limit tekşiriladi; zaruratda eng eski boşqariladigan backup öçiriladi,
   joy baribir yetmasa command xato bilan töxtaydi. Eski backup faqat aniq
   `backup_YYYY_MM_DD_HH_MM.sql.gz` nomi va command yaratgan `.owned` belgisi
   mavjud bölsa tozalanadi. Begona fayllar öçirilmaydi, lekin limitda hisoblanadi.
   Katalog faqat bitta işonçli foydalanuvçiga tegişli bölsin.
   Eski nusxalar yangi dump muvaffaqiyatsiz bölişidan oldin ham tozalanişi mumkin.
   Lock faylini öçirmang: Linux `flock` jarayon tugaganda avtomatik böşaydi.
   SIGKILL/quvvat uzilişidan qolgan `.send-backup-*` kataloglarini avtomatik
   öçirmaydi; faol jarayon yöqligini tekşirgaç administrator tozalaydi.

4. Staging tekşiruvlari (yöllarni staging joylaşuviga moslang):

   ```bash
   /path/to/venv/bin/python -m pip install -r requirements.txt
   /path/to/venv/bin/python -m unittest backup.tests -v
   /path/to/venv/bin/python manage.py check
   /path/to/venv/bin/python manage.py send_backup
   echo $?
   ```

   Oxirgi buyruq **haqiqiy DB dump va Telegram upload** qiladi. Kanalda barça
   qismlar va yakuniy muvaffaqiyat xabari bölişini tekşiring; exit code 0
   bölişi kerak. Alohida stagingda notöğri binary/token, HTTP timeout,
   kiçik disk limiti va parallel ikkita commandni tekşiring (notöğri binary
   testi faqat PostgreSQL/MySQLga tegişli): xato uçun exit 1,
   qisman dump yuborilmasligi va vaqtinçalik fayllar tozalanişi kerak.
   HTTP/Telegram xatosida allaqaçon yuborilgan qismlar kanalda qolişi mumkin;
   muvaffaqiyat xabarisiz töplamni tayyor backup deb hisoblamang.
   Unit testlar tarmoqsiz siqiş, bölish/birlashtirish, retention, xatolar va
   Linuxda lockni tekşiradi; real DB/Telegram staging sinovi alohida zarur.
   Django settings yuklanişidan oldingi xatolarda command hali işga tuşmaydi;
   cron stderr monitoringini ham saqlang.

5. Cron: Django `TIME_ZONE=Asia/Tashkent`; nomdagi vaqt haqiqiy mahalliy
   vaqt (`backup_2026_10_20_02_00.sql.gz`), qölda işga tuşganda ham 02:00
   deb qotirilmagan. Şu daqiqada mavjud backup ustiga yozilmaydi.
   Ubuntu cron **server timezone** böyiça işlaydi:

   ```bash
   timedatectl
   date
   crontab -e
   ```

   Server timezone Asia/Tashkent bölsa, loyiha egasining crontab'iga:

   ```cron
   0 2 * * * cd /home/<user>/<app> && /home/<user>/<app>/.venv/bin/python manage.py send_backup >> /home/<user>/backup-cron.log 2>&1
   ```

   Server UTC bölsa Tashkent 02:00 uçun `0 21 * * *` işlating (oldingi UTC kun).
   `TZ` env qiymatini qöyiş Ubuntu cron jadvalini avtomatik özgartiradi deb
   hisoblamang. Cron uçun Passenger `.htaccess`dagi interpreter va loyiha yölini
   tekşiring. Log fayliga `600` huquq va logrotate qöying. Engintron/Nginx va
   Passenger 6.2.0 içida scheduler yoki request orqali backup işga tuşirilmaydi.

### Stagingda tiklaşni tekşiriş

Telegram `sendDocument` rasmiy limit 50 MB:
https://core.telegram.org/bots/api#senddocument . Command 49 000 000 baytlik
qismlardan foydalanadi; har qism alohida hujjat va caption'da `qism N/T` bilan
yuboriladi. RAMga töliq arxiv yuklanmaydi. Qismlar mustaqil gzip emas: avval
nomdagi raqamlar tartibida birlaştiring. Caption'dagi SHA256 töliq gzipga tegişli.

```bash
# Bitta backup to‘plamining barcha qismlarini alohida katalogga yuklab oling.
cat backup_2026_10_20_02_00.sql.gz.part*-of-* > restored.sql.gz
sha256sum restored.sql.gz
gzip -t restored.sql.gz
gzip -dc restored.sql.gz > restored.sql
# Bitta hujjat bo‘lsa cat kerak emas: o‘sha .sql.gz uchun sha256sum va gzip -t.
```

SHA256 caption bilan bir xil va barça qismlar mavjudligini tekşiring. Söng
faqat izolyatsiyalangan staging DBda, parolni command argumentiga yozmasdan:

```bash
# SQLite: faqat yangi, alohida staging fayliga tiklang, production db.sqlite3ga emas.
sqlite3 staging_restore.sqlite3 < restored.sql
sqlite3 staging_restore.sqlite3 'PRAGMA integrity_check;'
# PostgreSQL: bo‘sh, alohida staging_restore bazasi oldindan yaratiladi.
psql -W -h localhost -U staging_user -d staging_restore -v ON_ERROR_STOP=1 -f restored.sql
# MySQL/MariaDB: dump CREATE DATABASE/USE asl nomni saqlaydi!
# Shu sabab faqat alohida disposable MySQL instance ichida ishga tushiring.
mysql -h localhost -u staging_user -p < restored.sql
```

Tiklangan jadvallar va asosiy yozuvlar sonini tekşiring. Dump fayllarida şaxsiy
ma’lumotlar bölişi mumkin; kanal va server backup katalogiga kirişni çeklang.
Token, DB paroli, töliq connection string yoki xom HTTP/subprocess xatolari
command logiga çiqarilmaydi. Haqiqiy `.env` bu özgarişlarda yaratilmaydi.
