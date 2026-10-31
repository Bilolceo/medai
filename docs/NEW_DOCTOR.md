# Yangi shifokor uchun bot (qo'llanma)

Bitta shifokor = bitta o'z serveri (Railway loyihasi), o'z bazasi va o'z
Instagram ulanishi. Kod hammada bir xil; shifokorni boshqasidan ajratadigan
hamma narsa **bazadagi sozlamalar** (prompt, namunalar, qoidalar, manzil,
raqamlar) va u shablondan avtomatik tuziladi.

Taxminiy vaqt: texnik qismi 1–2 soat, shifokor bilan suhbat 45–60 daqiqa,
sinov 1 kun, kuzatuv 1 hafta.

## 1. Shifokordan nimalar kerak

Uchrashuvda shularni yozib oling (`data/doctor.example.json` ga mos):

**Majburiy**

| Savol | Maydon |
| --- | --- |
| To'liq ismi, egalik shaklida ("Aliyev Vali Salimovichning") | `DOCTOR_FULL_NAME_OF` |
| Qisqa nomi ("doktor Vali") — bot shunday aytadi | `DOCTOR_SHORT` |
| Mutaxassisligi kichik harflarda ("urolog", "urolog-androlog") | `SPECIALTY` |
| Qaysi shaharda qabul qiladi, "...ga" shaklida ("Toshkentga") | `CITY_TO` |
| Telegram nomi (chet eldagi bemorga beriladi) | `TELEGRAM` |
| Bemorga **aytiladigan** telefon raqam(lar)i, vergul bilan | `PHONES` |
| Qabul manzili | `ADDRESS` |
| Ish vaqti ("Dushanbadan shanbagacha 09:00–17:00, yakshanba dam olish") | `WORK_HOURS` |

**Ixtiyoriy** (bo'lmasa, tegishli qoida va namunalar tushib qoladi, bo'sh joy
qolmaydi, boshqa shifokorning narxi hech qachon chiqmaydi). Jadval maydonlari
berilmasa, qabul daftari standart jadvalda ishlaydi: dushanba–shanba,
09:00–17:00, 20 daqiqa. `WORK_HOURS` bemorga aytiladigan gap, `WORK_DAYS` /
`WORK_START` / `WORK_END` / `SLOT_MINUTES` esa qabul daftari hisoblaydigan
qiymatlar — ikkalasi bir-biriga mos bo'lsin.

| Savol | Maydon |
| --- | --- |
| Mo'ljal | `LANDMARK` |
| Qabul kunlari, hafta kuni raqamlari (1 dushanba … 7 yakshanba): "1,2,3,4,5,6" | `WORK_DAYS` |
| Qabul boshlanishi va tugashi, "09:00" / "17:00" | `WORK_START`, `WORK_END` |
| Bitta qabul necha daqiqa, "20" | `SLOT_MINUTES` |
| Qabul (ko'rik) narxi, "300 000 so'm" | `PRICE_CONSULT` |
| Tez bo'shanish operatsiyasi, "taxminan 1000 $ atrofida" | `PRICE_FAST` |
| Implant, "taxminan 30 mln so'm atrofida" | `PRICE_IMPLANT` |
| Narxi so'ralganda botning tanlov savoli ("qabulmi, operatsiyami yoki implantmi") | `SERVICES_CHOICE` |
| A'zo o'lchami bo'yicha amaliyotlar | `SIZE_PROCEDURES` |
| Dashboarddagi nom | `BRAND_TITLE` |

Savollar, shifokordan so'rash kerak:

1. Qaysi xizmatlarni bajaradi va qaysi birining narxini bemorga **aytishga**
   ruxsat beradi? (Narx faqat shu ro'yxatdan chiqadi.)
2. Qaysi raqamni bemorga berish mumkin? Shaxsiy raqami berilmasligi kerakmi?
3. Ohang: oddiy operatordek yoki rasmiyroq? "Siz" deb murojaat qilinadimi?
4. Nima haqida **gapirmaslik** kerak (dori, tashxis, boshqa shifokorlar)?
5. Qabulga yozilish tartibi: bot qo'ng'iroq uchun ism, raqam va qulay vaqtni
   yig'adi — shu yetarlimi yoki shifokor boshqacha xohlaydimi?
6. Eng ko'p so'raladigan 15–20 savolga o'zining javobi (bilim bazasiga).
7. Dam olish kunlari, ta'tillar.
8. Telegramga xabarnoma (yangi raqam, javob kutayotgan suhbat) qaysi chatga?

## 2. Instagramni ulash

1. Shifokor akkaunti **Professional** (Business yoki Creator) bo'lishi kerak.
2. Meta ilovasida shifokor akkauntini **Instagram Tester** qilib qo'shing;
   shifokor taklifni Instagram sozlamalarida qabul qiladi. (Ko'p mijozda
   Meta "App Review" kerak bo'ladi, uni oldindan boshlang.)
3. Meta ilovasida webhook: `https://<server>/webhook`, tasdiqlash tokeni —
   `WEBHOOK_VERIFY_TOKEN`; `messages` va `comments` ga obuna.
4. Akkaunt ID: webhookda kelgan `entry.id` (u `/me` qaytargan ID emas!).
   Xato bo'lsa, logda `webhook_unknown_ig_account` ko'rinadi.
5. Tokenni chatga yoki git'ga yozmang. `scripts/set_channel_credentials.py`
   uni muhit o'zgaruvchisidan olib shifrlab saqlaydi.

## 3. Serverni ko'tarish

[DEPLOY.md](DEPLOY.md) bo'yicha: har shifokorga **alohida Railway loyihasi**
(web, worker, postgres, redis), hammasi shu bitta repoga ulanadi;
`scripts/generate_secrets.py` bilan kalitlar. Yangi shifokor `main` branchni
kuzatadi; birinchi klinika `stable` ni — `main` dagi o'zgarish 1–2 kun
ishlagach `stable` ga o'tkaziladi. Muhit o'zgaruvchilari:

* `PRODUCT_NAME` — kirish sahifasidagi nom (standart `medai`; shifokor o'z
  nomini xohlasa, masalan `Dr. Vali`);
* `DOCTOR_NAME`, `DOCTOR_SPECIALTY` — bot kimning nomidan gapiradi; ikkinchisi
  dashboard sarlavhasi ostidagi qator ham (`brand_subtitle` sozlamasi bo'lmasa);
* `PROVISION_TENANT_NAME`, `PROVISION_IG_ACCOUNT_ID`, `PROVISION_OPERATOR_USERNAME`,
  `PROVISION_OPERATOR_PASSWORD` — birinchi yozuvlar server o'zi yaratadi.

## 4. Botni shablondan tuzish

```bash
# 1) Shifokor ma'lumotlari: data/doctor.example.json ni nusxalab to'ldiring.
cp data/doctor.example.json data/dr-ISM.json

# 2) Avval faqat ko'rib chiqing (hech narsa yozilmaydi):
DRY_RUN=1 python scripts/apply_template.py data/dr-ISM.json

# 3) Serverda qo'llash (baza faqat Railway ichida ochiq):
sh scripts/apply_remote.sh data/dr-ISM.json
```

`apply_template.py`:

* kerakli maydon yetishmasa yoki imlo xatosi bo'lsa, nimaligini aytib to'xtaydi;
* sozlamalar tenant ga yoziladi, avvalgisi `BACKUP_BEGIN/END` orasida chop
  etiladi;
* **bot o'chiq** holda qoladi (`BOT_ENABLED` aniq `true` bo'lmaguncha);
* tenantda prompt allaqachon bo'lsa, `FORCE=1` siz hech narsani almashtirmaydi.

## 5. Sinov (botni yoqishdan oldin)

1. Dashboardda botni yoqmasdan, sozlamalarni o'qib chiqing: prompt, qoidalar,
   raqamlar. Shifokorga ham ko'rsating.
2. `scripts/reply_suite.py` / `dry_run_reply.py` bilan 30–50 ta test savol:
   salom, narx, manzil, ovozli xabar, reels, lotin/kirill/rus, "botmisiz?",
   shoshilinch holat, dori so'rash.
3. Tekshiring: faqat `PHONES` dagi raqamlar chiqadimi, narxlar to'g'rimi,
   hech narsa to'qilmayaptimi.
4. Faqat shundan keyin botni yoqing.

## 5a. Haftalik hisobot va doktor boti

Doktorning Telegram boti (`DOCTOR_TELEGRAM_BOT_TOKEN`, `DOCTOR_TELEGRAM_USERNAMES`)
har dushanba 09:00 da (Toshkent vaqti) o'tgan haftaning hisobotini yuboradi:
bot tomoni (kim yozdi, kim javob oldi, tezlik, mavzular) va operatorlar tomoni
(javob tezligi, javob kutayotgan chatlar, klinikaniki bo'lmagan raqamlar,
"yangi" bo'lib turgan qo'ng'iroq raqamlari). Bot menyusidagi **📊 Hisobot**
tugmasi oxirgi 7 kunni darhol beradi.

Hisobot faqat doktor botni bir marta ochib (`/start`) chat saqlangandan keyin
keladi; u kelgunga qadar o'tkazib yuborilgan hafta, doktor botni ochganda, bir
soat ichida yetib boradi. Username'lar bo'sh bo'lsa, bot hech kimga ruxsat
bermaydi: ulashda shuni tekshiring.

## 6. Birinchi hafta

* Har kuni bot javoblarini o'qing: tashxis, to'qima narx, noto'g'ri raqam,
  noto'g'ri til/yozuv.
* Xato takrorlansa, shablonni (`app/doctor_templates/<nom>/`) tuzating,
  sinovdan o'tkazing va hamma shifokorga bir yo'la chiqaring. Bitta
  shifokorning bazasidagi sozlamani qo'lda tuzatish faqat shu shifokorga
  tegishli bo'lsin.
* Haftalik hisobot: nechta odam yozdi, nechta raqam qoldirdi, qabullar.

## Shablonlar

`app/doctor_templates/<nom>/` — `template.json` (modullar ro'yxati),
`prompt.txt`, `examples/*.txt`, `rules/*.txt`. `<<MAYDON>>` joylariga
shifokor ma'lumoti qo'yiladi. Yangi mutaxassislik (stomatolog va h.k.) uchun
yangi papka yarating; `urolog` ni namuna qilib oling. Maydon `needs` bo'lsa,
u berilmaganda modul tushib qoladi.
