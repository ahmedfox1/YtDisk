<div align="center">

# 💾 YTDISK

### خزّن أي ملف جوه فيديو يوتيوب 4K.. ورجّعه بايت ببايت

**Store any file inside a 4K YouTube video, and get it back byte for byte.**

[![Python](https://img.shields.io/badge/Python-3.9+-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![FFmpeg](https://img.shields.io/badge/FFmpeg-required-007808?logo=ffmpeg&logoColor=white)](https://ffmpeg.org/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![YouTube](https://img.shields.io/badge/شوف_الفيديو-FF0000?logo=youtube&logoColor=white)](https://www.youtube.com/@Ahmed_Sameh1)

</div>

---

> ⚠️ **المشروع ده تعليمي بالكامل.**
> الهدف إنك تفهم إزاي ضغط الفيديو والـError Correction بيشتغلوا، مش إنك تستخدم يوتيوب كتخزين.
> متخزّنش أي حاجة مهمة. يوتيوب ممكن يمسح الفيديو أو يعيد ضغطه في أي وقت.
> جرّب على قناة تجريبية وبفيديوهات **Unlisted**.

---

## 🎬 الفكرة في سطر

أي ملف هو مجرد أصفار وواحدات. كل bit بيتحوّل لمربع **أبيض (1)** أو **أسود (0)**، والمربعات بتترص في فريمات، والفريمات بتبقى فيديو 4K. ترفعه على يوتيوب، وبعدين تنزّله وتعكس العملية، فيرجع الملف زي ما هو بالظبط.

```mermaid
flowchart LR
    A[📄 ملف] --> B[🔐 تشفير AES-256]
    B --> C[🛡️ Reed-Solomon + Interleaving]
    C --> D[0️⃣1️⃣ bits]
    D --> E[⬛⬜ فريمات]
    E --> F[🎬 فيديو 4K]
    F --> G[(YouTube)]
    G --> H[⬇️ yt-dlp]
    H --> I[🔧 فك + تصليح]
    I --> J[✅ SHA-256 متطابق]
```

---

## ⚡ تشغيل سريع

### 1. التسطيب

```bash
git clone https://github.com/ahmedfox1/YtDisk
cd ytdisk
pip install -r requirements.txt
```

**ومحتاج FFmpeg** متسطّب وموجود في الـPATH:

| النظام | الأمر |
|---|---|
| Windows | `winget install --id Gyan.FFmpeg -e` (وبعدها اقفل الترمينال وافتحه تاني) |
| macOS | `brew install ffmpeg` |
| Linux | `sudo apt install ffmpeg` |

اتأكد إنه شغال: `ffmpeg -version`

### 2. ملف ← فيديو 4K

```bash
python ytdisk.py encode secret.zip -o upload.mp4 --password 1234
```

```
 file:  secret.zip  (1,000,000 bytes)
  with ECC: 1,143,675 bytes (+14%)
 video: upload.mp4  3840x2160  3.4s  3.9 MB
 sha256: 6aca80c6e03d6a20...
```

### 3. ارفع على يوتيوب

- ارفع `upload.mp4` من **YouTube Studio** كـ**Unlisted** (الـPrivate محتاج تسجيل دخول عشان تنزّله).
- **استنى لحد ما جودة 2160p تظهر.** يوتيوب بيجهّز الـ1080p الأول، والـ4K بتاخد وقت أطول.

### 4. نزّل الفيديو

```bash
# اتأكد إن الـ4K جاهزة (لازم تلاقي 3840x2160 في القايمة)
yt-dlp -F "https://youtu.be/XXXX"

# نزّل الفيديو بس، من غير صوت، بجودة 4K
yt-dlp -f "bestvideo[height>=2160]" -o "downloaded.%(ext)s" "https://youtu.be/XXXX"
```

### 5. فيديو ← ملف

```bash
python ytdisk.py decode downloaded.webm -o restored --password 1234
```

```
 header: secret.zip  1,000,000 bytes  block=4  frames=71  encrypted=True
 Reed-Solomon fixed 0 bytes
 expected: 6aca80c6e03d6a20...
 got:      6aca80c6e03d6a20...
 IDENTICAL, byte for byte!  → restored/secret.zip
```

---

## 🛠️ الأوامر

### `encode`: ملف ← فيديو

| الخيار | الافتراضي | الوصف |
|---|---|---|
| `file` | — | الملف اللي عايز تحوّله |
| `-o, --output` | `upload.mp4` | اسم الفيديو الناتج |
| `--block` | `4` | حجم البلوك لكل bit (على شبكة 1080p). أكبر = أمتن وأبطأ |
| `--nsym` | `32` | بايتات حماية Reed-Solomon لكل 255 بايت. `0` = من غير حماية |
| `--password` | — | يشفّر الملف بـAES-256-GCM |

### `decode`: فيديو ← ملف

| الخيار | الافتراضي | الوصف |
|---|---|---|
| `video` | — | الفيديو اللي نزّلته (mp4 أو webm أو أي دقة) |
| `-o, --output` | `restored` | الفولدر اللي الملف هيترجع فيه |
| `--password` | — | لازم لو الملف اتشفّر |

> الـdecoder بيعرف حجم البلوك والـnsym لوحده من الـheader، فمش محتاج تكتبهم تاني.

---

## 🧠 إزاي بتشتغل؟

الكود كله في ملف واحد، `ytdisk.py`، متقسّم أجزاء بالترتيب ده:

| # | الجزء | بيعمل إيه |
|---|---|---|
| 1 | **Config** | الإعدادات: الشبكة 1080p، الإخراج 4K، حجم البلوك، Reed-Solomon |
| 2 | **File ↔ bits** | `np.unpackbits` و`np.packbits` |
| 3 | **AES-256-GCM** | تشفير بباسورد (scrypt للمفتاح)، وبيكتشف لو الداتا اتلعب فيها |
| 4 | **Reed-Solomon** | كل 223 بايت + 32 بايت parity، وبيصلّح لحد 16 بايت بايظ في كل codeword |
| 5 | **bits ↔ frames** | 1 = مربع أبيض، 0 = مربع أسود. القراءة بتاخد متوسط نص البلوك بس |
| 6 | **Header** | اسم الملف والحجم والـSHA-256، ببلوكات 16×16 وحماية أقوى، ومكرر 30 فريم |
| 7 | **FFmpeg I/O** | pipe مباشر لـFFmpeg، وتكبير لـ4K بـnearest neighbor |
| 8 | **Encoder** | بيجمّع كل اللي فات |
| 9 | **Decoder** | العكس، وفي الآخر بيقارن الـhash |
| 10 | **CLI** | أوامر `encode` و`decode` |

### ليه Interleaving؟

ضغط الفيديو مش بيبوّظ بايتات متفرقة، بيبوّظ **حتة كاملة من الفريم**. لو كتبنا الـcodewords ورا بعض، codeword واحد هياخد الضربة كلها ويموت.

الحل: بنكتب البايت الأول من كل الـcodewords، وبعده التاني من كلهم... فالحتة البايظة تتوزّع، وكل codeword ياخد بايت أو اتنين بس. في الكود ده سطر واحد:

```python
return codewords.T.copy().tobytes()   # الـtranspose هو الـinterleaving
```

### ليه أبيض وأسود مش ألوان؟

الفيديو بيتخزن **إضاءة (Y)** بالدقة الكاملة، و**لون (U, V)** بربع الدقة (Chroma Subsampling 4:2:0). فالأبيض والأسود أمتن بكتير قدام الضغط.

### ليه 4K؟

يوتيوب بيدّي فيديوهات الـ4K **bitrate** أعلى بكتير. والفيديو مينفعش يشيل معلومات أكتر من الـbitrate بتاعه (حد Shannon). عشان كده بنرسم الداتا على شبكة 1080p ونكبّرها لـ4K: نفس الداتا، بس بتعيش.

---

## 🧪 نتايج التجارب

اتعملت على **محاكاة** لضغط يوتيوب (H.264 بـbitrate محدود: 4 Mbps لـ1080p و18 Mbps لـ4K). الأرقام تقريبية، والحكم النهائي برفع حقيقي.

| الدقة | البلوك | النوع | السعة | بايتات بايظة | النتيجة |
|---|---|---|---|---|---|
| 1080p | 1×1 | أبيض وأسود | 6641 KB/s | 97.8% | ❌ |
| 1080p | 4×4 | أبيض وأسود | 415 KB/s | 66.2% | ❌ |
| 1080p | 8×8 | أبيض وأسود | 104 KB/s | 0% | ✅ |
| 1080p | 4×4 | ألوان | 1245 KB/s | 95.3% | ❌ |
| **4K** | **4×4** | **أبيض وأسود** | **415 KB/s** | **0%** | **✅ الافتراضي** |
| 4K | 4×4 | 4 درجات رمادي | 830 KB/s | 27.9% | ❌ |
| 4K | 4×4 | ألوان | 1245 KB/s | 69.7% | ❌ |
| 4K | 8×8 | ألوان | 311 KB/s | 0% | ✅ |

**الخلاصة:** بالإعدادات الافتراضية، **1 جيجا محتاج حوالي 42 دقيقة فيديو**.

---

## ⚠️ ليه متستخدمهاش بجد؟

- **مفيش ضمان.** يوتيوب ممكن يمسح الفيديو أو يقفل القناة، أو يعيد ضغط الفيديوهات القديمة بكوديك جديد.
- **الكفاءة وحشة.** 1 جيجا = حوالي 42 دقيقة فيديو 4K، والرفع والتنزيل بطيئين.
- **ممكن يخالف شروط الاستخدام.** رفع فيديوهات هدفها التخزين مش المشاهدة ممكن يعرّض حسابك لمشاكل.
- **فيه بدائل أحسن.** Google Drive بيديك 15 جيجا مجاناً من غير أي صداع.


---

## 🐛 مشاكل شائعة

| المشكلة | الحل |
|---|---|
| `FileNotFoundError: [WinError 2]` | FFmpeg مش متسطّب أو مش في الـPATH. سطّبه واقفل الترمينال وافتحه تاني |
| `Reed-Solomon gave up` | الضغط بوّظ داتا كتير. اتأكد إنك نزّلت نسخة 2160p، أو اعمل encode بـ`--block 8` |
| `Requested format is not available` | يوتيوب لسه مجهّزش الـ4K. استنى شوية وجرّب تاني |
| `decryption failed` | الباسورد غلط |
| `Sign in to confirm you're not a bot` | `yt-dlp --cookies-from-browser firefox ...` |
| yt-dlp بيطلّع errors غريبة | `pip install -U yt-dlp` |

---

## 📚 اتعلم أكتر

- 🎥 **الفيديو الكامل:** [قناة بشمهندس أحمد سامح](https://www.youtube.com/@Ahmed_Sameh1)

الفكرة مستوحاة من مشاريع زي [Infinite Storage Glitch](https://github.com/DvorakDwarf/Infinite-Storage-Glitch)، والكود هنا مكتوب من الصفر بـPython عشان يتشرح خطوة بخطوة.

---

## 📄 License

[MIT](LICENSE). استخدمه واتعلم منه وطوّره براحتك.

<div align="center">

**لو المشروع عجبك، اديله ⭐**

</div>
