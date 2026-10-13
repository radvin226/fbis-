# -*- coding: utf-8 -*-
"""ابزارهای عمومی: نرمال‌سازی متن، اعداد فارسی، فونت، زمان و تاریخ."""

import re
import time
import random
import unicodedata

# ------------------------------------------------------------------ اعداد
FA_DIGITS = "۰۱۲۳۴۵۶۷۸۹"
AR_DIGITS = "٠١٢٣٤٥٦٧٨٩"
EN_DIGITS = "0123456789"

_D_MAP = {ord(f): ord(e) for f, e in zip(FA_DIGITS, EN_DIGITS)}
_D_MAP.update({ord(a): ord(e) for a, e in zip(AR_DIGITS, EN_DIGITS)})
_FA_MAP = {ord(e): ord(f) for e, f in zip(EN_DIGITS, FA_DIGITS)}


def en_digits(text: str) -> str:
    return (text or "").translate(_D_MAP)


def fa_digits(text) -> str:
    return str(text).translate(_FA_MAP)


def fa_num(n) -> str:
    """۱۲۳۴ -> ۱,۲۳۴ به فارسی"""
    try:
        return fa_digits(f"{int(n):,}")
    except Exception:
        return fa_digits(n)


# ------------------------------------------------------------------ متن
_ZWNJ = "\u200c\u200f\u200e\ufeff"
_AR_FIX = {"ي": "ی", "ك": "ک", "ۀ": "ه", "ؤ": "و", "إ": "ا", "أ": "ا", "آ": "آ"}


def compact(text: str) -> str:
    """Key for matching owner commands: spaces, ZWNJ and hamza variants do not matter."""
    t = normalize(text)
    t = t.replace("أ", "ا").replace("إ", "ا").replace("آ", "ا")
    return re.sub(r"\s+", "", t)


def normalize(text: str) -> str:
    """نرمال‌سازی متن فارسی برای تطبیق دستورات."""
    if not text:
        return ""
    text = unicodedata.normalize("NFKC", text)
    for a, b in _AR_FIX.items():
        text = text.replace(a, b)
    for ch in _ZWNJ:
        text = text.replace(ch, "")
    text = en_digits(text)
    text = re.sub(r"[ \t\u00a0]+", " ", text)
    return text.strip()


def clean_key(text: str) -> str:
    """نسخه‌ی فشرده‌شده برای مقایسه‌ی کلیدواژه (بدون فاصله)."""
    return normalize(text).replace(" ", "").lower()


def strip_emoji(text: str) -> str:
    return re.sub(r"[\U00010000-\U0010ffff\u2600-\u27bf\ufe0f]", "", text or "")


# ------------------------------------------------------------------ لینک و آیدی
LINK_RE = re.compile(
    r"(https?://\S+|www\.\S+|(?:splus|splusapp)\.ir/\S+|\b\S+\.(?:ir|com|net|org|me|co|xyz|site|info)\b/?\S*)",
    re.IGNORECASE,
)
USERNAME_RE = re.compile(r"@[A-Za-z][A-Za-z0-9_]{2,}")
SPLUS_ID_RE = re.compile(r"(?:^|\s)(?:-?\d{2,}|@[A-Za-z][A-Za-z0-9_]{2,})(?:$|\s)")
PHONE_RE = re.compile(r"(?:\+?98|0)?9\d{9}")


def has_link(text: str) -> bool:
    return bool(text) and bool(LINK_RE.search(text))


def has_username(text: str) -> bool:
    return bool(text) and bool(USERNAME_RE.search(text))


def extract_guid(text: str):
    """شناسه‌ی عددی Splus یا @username را از متن پیدا می‌کند."""
    text = normalize(text or "")
    m = re.search(r"(?<!\w)(-?\d{2,})(?!\w)", text)
    if m:
        return m.group(1)
    m = re.search(r"(?<!\w)(@[A-Za-z][A-Za-z0-9_]{2,})(?!\w)", text)
    return m.group(1) if m else None


def extract_username(text: str):
    text = normalize(text or "")
    m = re.search(r"(?<!\w)(@[A-Za-z][A-Za-z0-9_]{2,})(?!\w)", text)
    return m.group(1).lstrip("@") if m else None


def extract_join_links(text: str):
    """فقط لینک‌هایی که احتمال لینک دعوت/لینک چت Splus دارند."""
    out = []
    for raw in LINK_RE.findall(text or ""):
        link = raw.rstrip(".,!?)]}؛،")
        low = link.lower()
        if any(host in low for host in ("splus.ir", "splusapp.ir", "splusplus.ir")):
            if link not in out:
                out.append(link)
    return out


def is_duration(text: str) -> bool:
    return bool(re.fullmatch(r"\d+\s*[A-Za-zآ-ی]+?", normalize(text or "")) or re.fullmatch(r"\d+", normalize(text or "")))


def command_hit(text: str, word: str) -> bool:
    text = normalize(text or "").strip()
    word = normalize(word or "").strip()
    if not text or not word:
        return False
    if text == word:
        return True
    prefix = word + " "
    if not text.startswith(prefix):
        return False
    rest = text[len(prefix):].strip()
    return bool(rest) and bool(extract_guid(rest))


def mute_command_hit(text: str, word: str):
    text = normalize(text or "").strip()
    word = normalize(word or "").strip()
    if text == word:
        return True, ""
    prefix = word + " "
    if not text.startswith(prefix):
        return False, ""
    rest = text[len(prefix):].strip()
    if not rest:
        return False, ""
    guid = extract_guid(rest)
    without_guid = rest.replace(guid, "", 1).strip() if guid else rest
    if guid:
        if not without_guid:
            return True, ""
        if is_duration(without_guid):
            return True, without_guid
        return False, ""
    if is_duration(rest):
        return True, rest
    return False, ""


# ------------------------------------------------------------------ زمان
_UNITS = {
    "ثانیه": 1, "دقیقه": 60, "ساعت": 3600, "روز": 86400,
    "هفته": 604800, "ماه": 2592000, "سال": 31536000,
    "s": 1, "m": 60, "h": 3600, "d": 86400,
}


def parse_mute_minutes(text: str):
    """مدت سکوت را برحسب دقیقه برمی‌گرداند. عدد تنها = دقیقه."""
    text = normalize(text or "")
    if not text:
        return None
    m = re.fullmatch(r"(\d+)\s*([آ-یA-Za-z]+)?", text)
    if not m:
        return None
    value = int(m.group(1))
    if value < 1:
        return None
    unit = (m.group(2) or "دقیقه").strip().lower()
    if unit.startswith("دقیق") or unit in {"m", "min", "minute", "minutes"}:
        return value
    if unit.startswith("ساعت") or unit in {"h", "hour", "hours"}:
        return value * 60
    if unit.startswith("روز") or unit in {"d", "day", "days"}:
        return value * 1440
    if unit.startswith("هفته") or unit in {"w", "week", "weeks"}:
        return value * 10080
    if unit.startswith("ماه") or unit in {"mo", "month", "months"}:
        return value * 43200  # ۳۰ روز
    if unit.startswith("سال") or unit in {"y", "year", "years"}:
        return value * 525600  # ۳۶۵ روز
    # ثانیه در سیستم سروری SplusLib قابل اعمال با دقت ثانیه نیست؛
    # برای واحد صریح ثانیه، رو به بالا به یک دقیقه تبدیل می‌کنیم.
    if unit.startswith("ثانی") or unit in {"s", "sec", "second", "seconds"}:
        return max(1, (value + 59) // 60)
    return None


def format_mute_duration(minutes):
    """نمایش خوانا برای زمان سکوت؛ ۱ روز/ماه/سال و دقیقه‌ای."""
    try:
        m = int(minutes)
    except Exception:
        return "مدت نامعتبر"
    if m <= 0:
        return "دائمی"
    if m % (365 * 24 * 60) == 0:
        return f"{fa_num(m // (365 * 24 * 60))} سال"
    if m % (30 * 24 * 60) == 0:
        return f"{fa_num(m // (30 * 24 * 60))} ماه"
    if m % (24 * 60) == 0:
        return f"{fa_num(m // (24 * 60))} روز"
    if m % 60 == 0:
        return f"{fa_num(m // 60)} ساعت"
    return f"{fa_num(m)} دقیقه"


def parse_duration(text: str):
    """«۱۵ روز» یا «30 دقیقه» یا «10» -> ثانیه (عدد تنها = ثانیه)."""
    text = normalize(text)
    if not text:
        return None
    m = re.search(r"(\d+)\s*([^\d\s]+)?", text)
    if not m:
        return None
    value = int(m.group(1))
    unit = (m.group(2) or "").strip()
    if not unit:
        return value
    for key, mul in _UNITS.items():
        if unit.startswith(key):
            return value * mul
    return value


def human_duration(seconds: int) -> str:
    seconds = int(seconds)
    for name, size in (("روز", 86400), ("ساعت", 3600), ("دقیقه", 60)):
        if seconds >= size:
            return f"{fa_digits(seconds // size)} {name}"
    return f"{fa_digits(seconds)} ثانیه"


_MONTHS = ["فروردین", "اردیبهشت", "خرداد", "تیر", "مرداد", "شهریور",
           "مهر", "آبان", "آذر", "دی", "بهمن", "اسفند"]
_WEEKDAYS = ["شنبه", "یکشنبه", "دوشنبه", "سه‌شنبه", "چهارشنبه", "پنجشنبه", "جمعه"]


def jalali_now():
    """(سال, ماه, روز, نام‌روزهفته) — اگر jdatetime نصب باشد از آن استفاده می‌کند."""
    try:
        import jdatetime
        d = jdatetime.datetime.now()
        return d.year, d.month, d.day, _WEEKDAYS[d.weekday()]
    except Exception:
        # تبدیل دستی میلادی -> شمسی
        import datetime
        g = datetime.date.today()
        jy, jm, jd = _g2j(g.year, g.month, g.day)
        wd = (g.weekday() + 2) % 7  # دوشنبه=0 میلادی -> شنبه=0 شمسی
        return jy, jm, jd, _WEEKDAYS[wd]


def _g2j(gy, gm, gd):
    g_d_m = [0, 31, 59, 90, 120, 151, 181, 212, 243, 273, 304, 334]
    gy2 = gy - 1600
    gm2 = gm - 1
    gd2 = gd - 1
    g_day_no = 365 * gy2 + (gy2 + 3) // 4 - (gy2 + 99) // 100 + (gy2 + 399) // 400
    g_day_no += g_d_m[gm2] + gd2
    if gm > 2 and ((gy % 4 == 0 and gy % 100 != 0) or gy % 400 == 0):
        g_day_no += 1
    j_day_no = g_day_no - 79
    j_np = j_day_no // 12053
    j_day_no %= 12053
    jy = 979 + 33 * j_np + 4 * (j_day_no // 1461)
    j_day_no %= 1461
    if j_day_no >= 366:
        jy += (j_day_no - 1) // 365
        j_day_no = (j_day_no - 1) % 365
    for i in range(11):
        days = 31 if i < 6 else 30
        if j_day_no < days:
            return jy, i + 1, j_day_no + 1
        j_day_no -= days
    return jy, 12, j_day_no + 1


def date_text() -> str:
    y, m, d, wd = jalali_now()
    return f"📅 {wd} {fa_digits(d)} {_MONTHS[m - 1]} {fa_digits(y)}"


def clock_text() -> str:
    return "🕐 ساعت " + fa_digits(time.strftime("%H:%M:%S"))


def stamp(ts=None) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts or time.time()))


# ------------------------------------------------------------------ فونت‌ها
_EN_SRC = "abcdefghijklmnopqrstuvwxyz"

FONTS_EN = {
    "لش": dict(zip(_EN_SRC, "ᴀʙᴄᴅᴇғɢʜɪᴊᴋʟᴍɴᴏᴘǫʀsᴛᴜᴠᴡxʏᴢ")),
    "شکسته": dict(zip(_EN_SRC, "𝔞𝔟𝔠𝔡𝔢𝔣𝔤𝔥𝔦𝔧𝔨𝔩𝔪𝔫𝔬𝔭𝔮𝔯𝔰𝔱𝔲𝔳𝔴𝔵𝔶𝔷")),
    "کشیده": dict(zip(_EN_SRC, "𝓪𝓫𝓬𝓭𝓮𝓯𝓰𝓱𝓲𝓳𝓴𝓵𝓶𝓷𝓸𝓹𝓺𝓻𝓼𝓽𝓾𝓿𝔀𝔁𝔂𝔃")),
}

_FA_LETTERS = "ابپتثجچحخدذرزژسشصضطظعغفقکگلمنوهی"


def apply_font(text: str, name: str) -> str:
    """فونت انگلیسی نگاشتی + فونت فارسی تزئینی."""
    name = normalize(name)
    table = FONTS_EN.get(name)
    out = []
    for ch in text:
        low = ch.lower()
        if table and low in table:
            out.append(table[low])
        else:
            out.append(ch)
    res = "".join(out)
    if name == "کشیده":
        res = "ـ".join(list(res))
    elif name == "شکسته":
        res = " ".join(list(res))
    elif name == "لش":
        res = res.replace(" ", " ˓ ")
    return res


FA_FONT_STYLES = [
    lambda t: "ـ".join(t),
    lambda t: " ".join(t),
    lambda t: "•".join(t),
    lambda t: f"『{t}』",
    lambda t: f"╰┈➤ {t}",
    lambda t: f"✦ {t} ✦",
    lambda t: f"➤ {t} ⚡",
    lambda t: f"❰ {t} ❱",
    lambda t: f"⌯ {t} ⌯",
    lambda t: f"↫ {t} ↬",
    lambda t: f"‹ {t} ›",
    lambda t: f"𓆩 {t} 𓆪",
    lambda t: f"⟅ {t} ⟆",
    lambda t: f"☆ {t} ☆",
    lambda t: f"❪ {t} ❫",
]


def font_showcase(text: str) -> str:
    lines = [f"🖌 نمونه فونت‌های «{text}»", ""]
    for i, fn in enumerate(FA_FONT_STYLES, 1):
        lines.append(f"{fa_digits(i)}. {fn(text)}")
    for name in FONTS_EN:
        lines.append(f"• {name}: {apply_font(text, name)}")
    return "\n".join(lines)


# ------------------------------------------------------------------ صفحه‌بندی
PAGE_SIZE = 20


def paginate(items, page: int = 1):
    page = max(1, int(page or 1))
    total = max(1, (len(items) + PAGE_SIZE - 1) // PAGE_SIZE)
    page = min(page, total)
    start = (page - 1) * PAGE_SIZE
    return items[start:start + PAGE_SIZE], page, total


def rand(seq):
    return random.choice(seq) if seq else ""
