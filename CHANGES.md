# Version 2 - reliability, abuse and workflow fixes
- Durability: uploads copied into PostgreSQL, backup.py, pool_pre_ping, dashboard warning on SQLite/Render.
- Abuse: stale-order auto-cancel (also never-quoted orders), per-phone open-order cap, optional login-to-order,
  guest orders only attached with proof, per-phone login limits (CGNAT friendly).
- Payments: confirm needs proof, fee locked after payment, refund tracking, COD amount approval, bulk-save parity.
- Transporter: delivery code, decline order, report problem, forced password change, COD re-upload.
- Customer: cancel order, delivery code + SMS notifications, simpler password rule, gentler polling.
- Admin: stats, low stock, CSV export, pagination, multi-admin, audit log, new-order beep, reset-admin command.

# የተደረጉ ማስተካከያዎች (Changes)

## 2026-10-29 — AI toggle, checkout, login separation
- **Chat AI**: በነባሪ ጠፍቷል፤ Admin → Settings ላይ ON/OFF (ሲበራ ብቻ AI ይመልሳል)
- **Checkout**: "paste coordinates from Google Maps" ተወግዷል (GPS + ካርታ ፒን ብቻ)
- **Admin login**: ሁልጊዜ password ይጠይቃል፤ የተለየ field names (`admin_username` / `admin_password`)
- **Customer / Transporter login**: የተለየ ids + autocomplete — browser admin credentials አያስገባም
- **Qty**: native ↑↓ spinners ተደብቀዋል
- **Map CSP**: MapTiler / Mapbox / Esri tiles ተፈቅደዋል

## 2026-10-28 — የመለኪያ አሃድ (Unit of measure)
- **ምርት unit**: እያንዳንዱ ምርት `kg` / `L` / `pcs` / `pack` / `bag` ሊኖረው ይችላል
- **ዘይት** → ሊትር (1, 3, 5, 10, 20 L)
- **እህል** → ኪ.ግ (10, 15, 25, 50, 100 kg)
- **በቁጥር** → pcs / pack / bag
- Admin ቅጽ ላይ አሃድ ሲመረጥ መጠኖቹ በራስ-ሰር ይቀያየራሉ
- Cart, checkout, order detail ሁሉም አሃዱን ያሳያሉ (ኪ.ግ ብቻ አይደለም)
- `OrderItem.unit` snapshot — የድሮ ትዕዛዞች አሃዳቸው ይጠበቃል

## 2026-10-25 — የደህንነትና የተጠቃሚ ልምድ ማሻሻያዎች
- **Map tiles**: `MAP_TILES_URL` + `MAP_ATTRIBUTION` env (MapTiler/Mapbox). Esri only as local fallback.
- **Phone normalization**: 0911… / +251911… ወደ `+2519xxxxxxxx` ይቀየራል
- **Logout** `pending_orders` ያጸዳል (ጋራ መሣሪያ)
- **ሰዓት** Africa/Addis_Ababa (UTC+3)
- **Admin auto-refresh** ከ 3s → 30s (tab ታይቶ ብቻ)
- **Status transitions** ምክንያታዊ መንገድ ብቻ
- **Bank snapshot** በትዕዛዝ ሰዓት ይቀመጣል
- **Duplicate transaction ID** አይፈቀድም
- **Idempotency key** በ checkout (ሁለት ጊዜ ትዕዛዝ ይከላከላል)
- **GPS** ቁልፍ ሲጫኑ ብቻ
- **Stock** በጋሪ ገጽ ይፈተሻል
- **Password** ደንበኛ ቢያንስ 6 ቁምፊ (Amharic-friendly)
- **TRUST_PROXY** ማስጠንቀቂያ በ console
- **Referrer-Policy** `strict-origin-when-cross-origin`
- **Ethiopia bounding box** ለማድረሻ ቦታ
- **Order search** + Preparing/Cancelled ማጣሪያ
- **payment_note** በጋሪ/account ይታያል

# የተደረጉ ማስተካከያዎች (Changes)

## ሥራ የሚያስቆሙ ችግሮች
- **SECRET_KEY**፦ አሁን `instance/.secret_key` ውስጥ አንድ ጊዜ ይፈጠራል፤ ሁሉም gunicorn workers ተመሳሳይ ቁልፍ ይጠቀማሉ (login/session/CSRF አይቋረጥም)።
- **`init_db()`**፦ በፋይል lock ተጠብቋል፤ 4 workers በአንድ ጊዜ ቢነሱም የተደጋገሙ ምርቶች/admin አይፈጠሩም።
- **`order_number`**፦ ዓምዱ `String(40)` ሆኗል (PostgreSQL ላይ ትዕዛዝ አይወድቅም)።
- **Checkout ቁልፍ**፦ ሁሉም ማረጋገጫዎች ቁልፉን ከመዝጋት በፊት ይደረጋሉ፤ ተዘግቶ አይቀርም።
- **ካርታ (Leaflet)**፦ ካርታው ባይጫንም ትዕዛዝ ማስገባት ይቻላል (GPS ወይም coordinates መለጠፍ)።
- **የክፍያ ገደብ**፦ checkout ሲከፈት ዋጋ ከ server ይታደሳል፤ server "ክፍያ ያስፈልጋል" ካለ የባንክ ክፍል ይታያል።
- **Chat**፦ የ AI መልስ አይደገምም (`flush()` ተጨምሯል)፤ መልዕክት ሳይላክ ቢቀር ጽሑፉ አይጠፋም፤ ስህተት ይታያል።

## ደህንነት
- **CSRF** በሁሉም POST ላይ ተግባራዊ ሆኗል (ፎርሞች + `fetch` ራስጌ)።
- **Login ገደብ**፦ 5 ያልተሳኩ ሙከራዎች → 15 ደቂቃ እገዳ (admin, customer, transporter)፤ chat/help/register/checkout በ IP ይገደባሉ።
- **ደረሰኞች ግላዊ ናቸው**፦ `instance/private_uploads/` ውስጥ ይቀመጣሉ፤ admin፣ የትዕዛዙ ባለቤት ወይም የተመደበው አጓጓዥ ብቻ ያያቸዋል። የድሮ ደረሰኞች በራስ-ሰር ይዛወራሉ።
- **ምስሎች**፦ እውነተኛው ይዘት (magic bytes + Pillow) ይረጋገጣል፤ Pillow ግዴታ ሆኗል።
- **Admin password**፦ ቋሚ `admin123` የለም፤ ካልተሰጠ በዘፈቀደ ይፈጠራል (አንድ ጊዜ ይታያል)።
- **የናሙና ባንክ አካውንት** ንቁ አይደለም (ደንበኞች ወደ ሐሰተኛ አካውንት እንዳይከፍሉ)።
- **Logout** በ POST ብቻ፤ session ከመግባት በኋላ ይታደሳል፤ open-redirect ተዘግቷል፤ የደህንነት ራስጌዎች ተጨምረዋል።
- ባልተጠበቁ `int()`/`float()` ግብዓቶች የሚመጣ 500 ስህተት ተወግዷል፤ ቀለም/ፎንት ይረጋገጣሉ።

## የሎጂክ ስህተቶች
- **Stock**፦ ሰርዝ → መልስ → ሰርዝ ደጋግሞ አይጨምርም (`stock_released`)፤ ትዕዛዝ እንደገና ሲከፈት stock ይቀነሳል፤ ሲፈጠር ከመጠን በላይ አይሸጥም (atomic update)።
- **አጓጓዥ መመደብ**፦ ሁኔታውን በግድ `out_for_delivery` አያደርግም፤ የተዘጋ ትዕዛዝ ላይ አይፈቀድም።
- **አጓጓዥ**፦ የተሰረዘ/የተዘጋ ትዕዛዝ ወይም ያልተረጋገጠ ክፍያ ላይ `delivered` ማድረግ አይችልም፤ የተሰበሰበው መጠን ይረጋገጣል።
- **የማድረሻ ሰዓት**፦ የእያንዳንዱ ምርት ሰዓት ተግባራዊ ይሆናል።
- **ባንክ/አጓጓዥ መሰረዝ**፦ በትዕዛዞች ላይ ከተመዘገበ ይሰናከላል እንጂ አይሰረዝም።
- **አዲስ አጓጓዥ** የይለፍ ቃል ባይሰጥ የተፈጠረው ጊዜያዊ የይለፍ ቃል አንድ ጊዜ ይታያል።

## ሌሎች
- Amharic ትርጉም (`translations/am`)፣ የኢሜይል ማረጋገጫ፣ `db.get_or_404`/`utcnow()` (deprecated ኮድ ተወግዷል)።
- `innerHTML` ላይ `escapeHtml`፤ የጋሪ ዋጋዎች ይቀመጣሉ፤ API ስህተት ጋሪውን አያጠፋም።
- ከ 100,000 ብር በላይ (COD) እና ከ 100 በታች ክፍያ አለመጠየቅ **ሳይቀየር** ቀርቷል (የንግድ ውሳኔ ነው) — `PREPAY_MIN`/`PREPAY_MAX` በ `app.py` አናት።

## ተጨማሪ ማስተካከያ
- **Checkout rate limit**፦ የሚቆጠሩት የተሳኩ ትዕዛዞች ብቻ ናቸው (የማረጋገጫ ስህተቶች ኮታውን አይበሉም)።
- **CSRF ቶክን**፦ login ሲደረግ ቶክኑ አይቀየርም፤ በሌሎች ትሮች ተከፍተው የነበሩ ገጾች መስራታቸውን ይቀጥላሉ።
- **ማሳሰቢያ**፦ ከ nginx ጀርባ ሲሰራ `TRUST_PROXY=1` ግዴታ ነው፤ ያለዚያ ሁሉም ደንበኞች አንድ IP ይጋራሉ።
