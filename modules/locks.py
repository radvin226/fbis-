# -*- coding: utf-8 -*-
"""سیستم قفل‌ها: تشخیص تخلف + دستورات مدیریت قفل."""

import re
import time
from collections import deque

import config
from core import ranks, utils
from core.db import db

# نام فارسی -> کلید داخلی
LOCKS = {
    "لینک": "link",
    "ایدی": "username",
    "آیدی": "username",
    "یوزرنیم": "username",
    "فوروارد": "forward",
    "هایپرلینک": "hyperlink",
    "عکس": "photo",
    "ویدیو": "video",
    "فیلم": "video",
    "گیف": "gif",
    "ویس": "voice",
    "موزیک": "music",
    "اهنگ": "music",
    "فایل": "file",
    "استیکر": "sticker",
    "نظرسنجی": "poll",
    "مخاطب": "contact",
    "موقعیت": "location",
    "متن": "text",
    "اسپم": "spam",
    "کلمه نامناسب": "badword",
    "فحش": "badword",
    "انگلیسی": "english",
    "شماره": "phone",
    "استوری": "story",
    "پست": "post",
    "مستهجن": "obscene",
    "ویرایش": "edit",
    "تگ": "mention",
    "حجم فایل": "file_size",
    "فایل حجیم": "file_size",
    "کد هنگ کننده": "hang_code",
    "کد هنگ‌کننده": "hang_code",
}

TITLES = {
    "link": "لینک", "username": "آیدی", "forward": "فوروارد", "hyperlink": "هایپرلینک",
    "photo": "عکس", "video": "ویدیو", "gif": "گیف", "voice": "ویس", "music": "موزیک",
    "file": "فایل", "sticker": "استیکر", "poll": "نظرسنجی", "contact": "مخاطب",
    "file_size": "حجم فایل", "hang_code": "کد هنگ‌کننده",
    "location": "موقعیت", "text": "متن", "spam": "اسپم", "badword": "کلمه نامناسب",
    "english": "انگلیسی", "phone": "شماره", "story": "استوری", "post": "پست",
    "obscene": "محتوای مستهجن", "edit": "ویرایش", "mention": "تگ",
}

BADWORDS_DEFAULT = ["کصافت", "کثافت", "حروم زاده", "بیشعور", "خفه شو"]

# ------------------------------------------------------------ ضدّ اسپم
_spam_cache = {}
_wave_cache = {}
_wave_notified = {}
_punish_notice_cache = {}
SPAM_RULES = {1: (10, 12), 2: (6, 8), 3: (4, 5)}


def _spam_hit(guid, author, text, level) -> bool:
    limit, window = SPAM_RULES.get(int(level or 2), SPAM_RULES[2])
    now = time.time()
    key = (str(guid), str(author))
    items = [i for i in _spam_cache.get(key, []) if now - i[0] < window]
    normalized = utils.normalize(text or '').lower()
    items.append((now, normalized))
    _spam_cache[key] = items[-30:]
    if len(items) >= limit:
        return True
    if len(items) >= 3 and normalized and all(i[1] == normalized for i in items[-3:]):
        return True
    return False


def register_abuse_wave(ctx, hit):
    """Group-level burst detector: several moderation hits in a short window."""
    if hit not in getattr(config, "SPAM_WAVE_TYPES", ()):
        return False
    now = time.time()
    key = str(ctx.guid)
    q = _wave_cache.setdefault(key, deque())
    q.append((now, str(hit), str(ctx.author or '')))
    window = max(10, int(getattr(config, 'SPAM_WAVE_WINDOW_SECONDS', 90)))
    while q and now - q[0][0] > window:
        q.popleft()
    threshold = max(2, int(getattr(config, 'SPAM_WAVE_THRESHOLD', 6)))
    if len(q) < threshold:
        return False
    # Do not trigger repeatedly while the same one-hour closure is active.
    g = ctx.g
    if float(g.get('closed_until') or 0) > now:
        return False
    last = float(_wave_notified.get(key, 0) or 0)
    if now - last < window:
        return False
    _wave_notified[key] = now
    q.clear()
    return True


def reset_wave(guid):
    _wave_cache.pop(str(guid), None)
    _wave_notified.pop(str(guid), None)


# ------------------------------------------------------------ تشخیص حجم فایل و کدهای هنگ‌کننده

def _to_int(value):
    try:
        return int(value)
    except Exception:
        try:
            return int(float(value))
        except Exception:
            return None


def _file_size_bytes(update):
    """حجم فایل را از شکل‌های رایج Update در نسخه‌های مختلف RubPy استخراج می‌کند."""
    seen = set()
    keys = ("file_size", "size", "file_size_bytes", "document_size", "bytes")

    def walk(obj, depth=0):
        if obj is None or depth > 5 or id(obj) in seen:
            return None
        seen.add(id(obj))
        if isinstance(obj, dict):
            for k in keys:
                if k in obj:
                    n = _to_int(obj[k])
                    if n is not None and n >= 0:
                        return n
            for k in ("file", "document", "media", "photo", "video", "music", "voice", "new_message", "message"):
                if k in obj:
                    n = walk(obj[k], depth + 1)
                    if n is not None:
                        return n
            return None
        for k in keys:
            try:
                v = getattr(obj, k, None)
            except Exception:
                v = None
            n = _to_int(v)
            if n is not None and n >= 0:
                return n
        for k in ("file", "document", "media", "photo", "video", "music", "voice", "new_message", "message"):
            try:
                v = getattr(obj, k, None)
            except Exception:
                v = None
            n = walk(v, depth + 1)
            if n is not None:
                return n
        return None

    return walk(update)


def _looks_like_hanging_code(text):
    """فقط الگوهای واضحِ حلقه بی‌نهایت/بازگشت بی‌پایان را تشخیص می‌دهد؛ کد اجرا نمی‌شود."""
    if not text or len(text) > 30000:
        return False
    t = utils.normalize(text).lower()
    patterns = (
        r"\bwhile\s+true\s*:",
        r"\bwhile\s*\(\s*true\s*\)",
        r"\bfor\s*\(\s*;\s*;\s*\)",
        r"\bwhile\s*\(\s*1\s*\)",
        r"\bwhile\s+1\s*:",
        r"\bloop\s*\{\s*loop\b",
    )
    if any(re.search(p, t) for p in patterns):
        return True
    # بازگشت مستقیمِ یک تابع با همان نام، فقط وقتی چند نشانه‌ی کد پایتون وجود دارد.
    if "def " in t and "return " in t:
        funcs = re.findall(r"\bdef\s+([a-z_]\w*)\s*\(", t)
        for fn in funcs:
            if len(re.findall(rf"\b{re.escape(fn)}\s*\(", t)) >= 2 and "sys.setrecursionlimit" in t:
                return True
    return False


def _is_code_update(update, text):
    if _looks_like_hanging_code(text):
        return True
    blob = str(getattr(update, "original_update", "") or "").lower()
    return any(ext in blob for ext in (".py", ".js", ".ts", ".java", ".cpp", ".c", ".cs", ".php")) and _looks_like_hanging_code(text)


# ------------------------------------------------------------ تشخیص تخلف
def detect(ctx):
    """کلید قفلی که پیام نقض کرده را برمی‌گرداند، یا None."""
    g = ctx.g
    locks = g.get("locks", {})
    up = ctx.update
    text = ctx.raw_text or ""

    def on(key):
        return bool(locks.get(key))

    # لیست مجاز: هر عبارتی که مجاز شده باشد از قفل عبور می‌کند
    allowed = g.get("allowed", [])
    clean = text
    for item in allowed:
        clean = clean.replace(item, " ")

    if on("forward") and getattr(up, "is_forward", False):
        return "forward"
    if on("link") and utils.has_link(clean):
        return "link"
    if on("username") and utils.has_username(clean):
        return "username"
    if on("phone") and utils.PHONE_RE.search(utils.en_digits(clean)):
        return "phone"
    if on("hyperlink") and _has_hyperlink(up):
        return "hyperlink"

    # قفل حجم فایل: فقط وقتی متادیتای حجم فایل در Update موجود باشد.
    if on("file_size"):
        size = _file_size_bytes(up)
        limit = max(1, int(g.get("max_file_mb") or 20)) * 1024 * 1024
        if size is not None and size > limit:
            return "file_size"

    # قفل کد هنگ‌کننده: کد اجرا نمی‌شود؛ فقط الگوهای واضح بررسی می‌شوند.
    if on("hang_code") and _is_code_update(up, text):
        return "hang_code"

    for key, attr in (("photo", "photo"), ("video", "video"), ("gif", "gif"),
                      ("voice", "voice"), ("music", "music"), ("file", "file"),
                      ("sticker", "sticker"), ("poll", "poll"),
                      ("contact", "contact"), ("location", "location")):
        if on(key) and getattr(up, attr, False):
            return key

    if on("text") and text and not any(
            getattr(up, a, False) for a in ("photo", "video", "gif", "voice",
                                            "music", "file", "sticker")):
        return "text"

    if on("english") and re.search(r"[A-Za-z]{4,}", clean):
        return "english"

    if on("badword"):
        words = g.get("filters") or BADWORDS_DEFAULT
        low = utils.normalize(clean).lower()
        for word in words:
            if utils.normalize(word).lower() in low:
                return "badword"

    if on("story") and _is_rubino(up, "story"):
        return "story"
    if on("post") and _is_rubino(up, "post"):
        return "post"

    if on("spam") and _spam_hit(ctx.guid, ctx.author, text, g.get("spam_level", 2)):
        return "spam"

    return None


def _has_hyperlink(update):
    meta = getattr(update, "metadata", None) or {}
    parts = []
    if isinstance(meta, dict):
        parts = meta.get("meta_data_parts", [])
    else:
        parts = getattr(meta, "meta_data_parts", []) or []
    for part in parts:
        kind = part.get("type") if isinstance(part, dict) else getattr(part, "type", "")
        if str(kind).lower() == "link":
            return True
    return False


def _is_rubino(update, kind):
    raw = getattr(update, "original_update", None) or {}
    blob = str(raw).lower()
    return f"rubino{kind}" in blob or f"{kind}_id" in blob


# ------------------------------------------------------------ اعمال تخلف
def _should_notify(guid, author, lock_key):
    now = time.time()
    key = (str(guid), str(author), str(lock_key))
    cooldown = max(2, int(getattr(config, "PUNISH_NOTICE_COOLDOWN_SECONDS", 15)))
    last = float(_punish_notice_cache.get(key, 0) or 0)
    if now - last < cooldown:
        return False
    _punish_notice_cache[key] = now
    return True


async def punish(ctx, lock_key):
    """پیام را حذف می‌کند و در صورت نیاز اخطار می‌دهد / کاربر را حذف می‌کند."""
    g = ctx.g
    title = TITLES.get(lock_key, lock_key)
    await ctx.api.delete(ctx.guid, [ctx.message_id])
    db.bump(ctx.guid, ctx.author, "deleted")

    warn_on = g.get("lock_warn", {}).get(lock_key, True)
    ban_on = g.get("lock_ban", {}).get(lock_key, True)
    if not warn_on:
        return

    warns = g.setdefault("warns", {})
    count = warns.get(ctx.author, 0) + 1
    warns[ctx.author] = count
    db.save("groups")

    name = await ctx.target_link(ctx.author)
    # سقف اخطار می‌تواند برای هر قفل جداگانه تنظیم شود؛ در غیر این صورت
    # سقف عمومی گروه استفاده می‌شود.
    maximum = int(g.get("lock_max_warn", {}).get(lock_key) or g.get("max_warn") or 3)

    if ban_on and count >= maximum:
        # وقتی کاربر به‌علت کیل/قفل حذف می‌شود، سابقه کیل او نباید باعث
        # حذف دوباره در ورود بعدی شود.
        try:
            if ctx.author in g.get("kill", []):
                g["kill"].remove(ctx.author)
        except Exception:
            pass
        warns.pop(ctx.author, None)
        db.save("groups")
        await ctx.api.ban(ctx.guid, ctx.author)
        await ctx.api.send(
            ctx.guid,
            f"🚫 کاربر {name}\nبه دلیل «{title}» به سقف اخطار رسید و از گروه حذف شد.")
        return

    if _should_notify(ctx.guid, ctx.author, lock_key):
        await ctx.api.send(
            ctx.guid,
            f"⚠️ {name} عزیز، ارسال «{title}» در این گروه ممنوع است.\n"
            f"اخطار: {utils.fa_digits(count)} از {utils.fa_digits(maximum)}")


# ------------------------------------------------------------ دستورات قفل
def _find_lock(text):
    """نام قفل را فقط به‌صورت عبارت مستقل تشخیص بده؛ substring ممنوع."""
    text = utils.normalize(text or "").strip()
    if not text:
        return None, None
    # Compare on the normalized form on both sides: ctx.text drops ZWNJ, the table keeps it.
    for fa, key in sorted(LOCKS.items(), key=lambda i: -len(utils.normalize(i[0]))):
        if text == utils.normalize(fa):
            return key, fa
    return None, None


async def handle(ctx) -> bool:
    text = ctx.text
    g = ctx.g

    # ---- لیست قفل
    if text in ("لیست قفل", "قفل ها", "قفلها"):
        if ctx.rank < ranks.ADMIN:
            return False
        lines = ["🔒 وضعیت قفل‌های گروه:\n"]
        for key, title in TITLES.items():
            state = "🔴 قفل" if g["locks"].get(key) else "🟢 باز"
            warn = "بله" if g.get("lock_warn", {}).get(key, True) else "خیر"
            ban = "بله" if g.get("lock_ban", {}).get(key, True) else "خیر"
            limit = g.get("lock_max_warn", {}).get(key) or g.get("max_warn", 3)
            lines.append(f"• {title}: {state} | اخطار: {warn} | بن: {ban} | سقف: {utils.fa_digits(limit)}")
        lines.append(f"\n⚠️ حداکثر اخطار: {utils.fa_digits(g.get('max_warn', 5))}")
        await ctx.reply("\n".join(lines))
        return True

    # ---- اخطار <قفل> روشن/خاموش   و   بن <قفل> روشن/خاموش
    for prefix, field in (("اخطار", "lock_warn"), ("بن", "lock_ban")):
        m = re.match(rf"^{prefix} (.+?) (روشن|خاموش)$", text)
        if m:
            key, fa = _find_lock(m.group(1))
            if not key:
                continue
            if ctx.rank < ranks.OWNER:
                return await _deny(ctx, "مالک گروه")
            g.setdefault(field, {})[key] = (m.group(2) == "روشن")
            db.save("groups")
            word = "اخطار دادن" if field == "lock_warn" else "حذف کاربر"
            await ctx.ok(f"{word} برای قفل «{TITLES[key]}» "
                         f"{'فعال' if m.group(2) == 'روشن' else 'غیرفعال'} شد.")
            return True

    # ---- تنظیم سقف اخطار اختصاصی هر قفل / همه قفل‌ها
    m = re.match(r"^تنظیم اخطار (همه|تمام|کل) (\d+)$", text)
    if m:
        if ctx.rank < ranks.OWNER:
            return await _deny(ctx, "مالک گروه")
        maximum = max(1, min(100, int(m.group(2))))
        g["max_warn"] = maximum
        # با تنظیم «همه»، همه قفل‌های فعلی و آینده از سقف عمومی استفاده می‌کنند.
        g["lock_max_warn"] = {}
        db.save("groups")
        await ctx.ok(f"سقف اخطار همه قفل‌ها روی {utils.fa_digits(maximum)} تنظیم شد.")
        return True

    m = re.match(r"^تنظیم اخطار (.+?) (\d+)$", text)
    if m:
        key, fa = _find_lock(m.group(1))
        if key:
            if ctx.rank < ranks.OWNER:
                return await _deny(ctx, "مالک گروه")
            maximum = max(1, min(100, int(m.group(2))))
            g.setdefault("lock_max_warn", {})[key] = maximum
            db.save("groups")
            await ctx.ok(f"سقف اخطار قفل «{TITLES[key]}» روی {utils.fa_digits(maximum)} تنظیم شد.")
            return True

    # ---- روشن/خاموش کردن اخطار یا بن برای همه قفل‌ها
    for prefix, field, label in (("اخطار", "lock_warn", "اخطار"), ("بن", "lock_ban", "بن")):
        if text in (f"{prefix} همه روشن", f"{prefix} تمام روشن", f"{prefix} کل روشن",
                    f"{prefix} همه خاموش", f"{prefix} تمام خاموش", f"{prefix} کل خاموش"):
            if ctx.rank < ranks.OWNER:
                return await _deny(ctx, "مالک گروه")
            enabled = text.endswith("روشن")
            for k in TITLES:
                g.setdefault(field, {})[k] = enabled
            db.save("groups")
            await ctx.ok(f"{label} برای همه قفل‌ها {'فعال' if enabled else 'غیرفعال'} شد.")
            return True

    # ---- تنظیم سطح قفل اسپم N
    m = re.match(r"^تنظیم سطح قفل اسپم (\d)$", text)
    if m:
        if ctx.rank < ranks.OWNER:
            return await _deny(ctx, "مالک گروه")
        level = max(1, min(3, int(m.group(1))))
        g["spam_level"] = level
        db.save("groups")
        await ctx.ok(f"سطح قفل اسپم روی {utils.fa_digits(level)} تنظیم شد.")
        return True

    # ---- تنظیم حداکثر حجم فایل
    m = re.match(r"^تنظیم (?:حداکثر )?(?:حجم فایل|حجم فایل مجاز)\s+(\d+)\s*(?:مگ|مگابایت|mb)?$", text, re.I)
    if m:
        if ctx.rank < ranks.OWNER:
            return await _deny(ctx, "مالک گروه")
        mb = max(1, min(2048, int(m.group(1))))
        g["max_file_mb"] = mb
        db.save("groups")
        await ctx.ok(f"حداکثر حجم فایل روی {utils.fa_digits(mb)} مگابایت تنظیم شد.")
        return True

    # ---- «قفل لینک روشن» / «لینک قفل» / «قفل لینک خاموش» / «لینک باز»
    state = None
    body = None
    if text.startswith("قفل "):
        rest = text[4:]
        if rest.endswith(" روشن"):
            state, body = True, rest[:-5]
        elif rest.endswith(" خاموش"):
            state, body = False, rest[:-6]
    if state is None:
        if text.endswith(" قفل"):
            state, body = True, text[:-4]
        elif text.endswith(" باز"):
            state, body = False, text[:-4]

    if state is not None and body:
        key, fa = _find_lock(body)
        if key:
            if ctx.rank < ranks.OWNER:
                return await _deny(ctx, "مالک گروه")
            previous = bool(g["locks"].get(key))
            had_status = key in (g.get("status_messages") or {})
            g["locks"][key] = state
            db.save("groups")
            status_text = f"🔒 {TITLES.get(key, key)} {'قفل شد' if state else 'باز شد'} {'✅' if state else '🔓'}"
            from core.api import LOCK_RIGHTS  # local import: keeps module loading independent
            if key in LOCK_RIGHTS:
                synced = await ctx.api.sync_group_permissions(ctx.guid)
                status_text += "\n🛡 محدودیت سروری: " + ("اعمال شد" if synced else "اعمال نشد؛ کنترل محلی ادامه دارد")
            if key in LOCK_RIGHTS:
                unsupported = ctx.api.unsupported_rights(LOCK_RIGHTS[key])
                if unsupported:
                    status_text += "\n⚠️ این نسخه SplusLib این دسترسی را روی سرور ندارد؛ این قفل فقط محلی اجرا می‌شود."
            if (not had_status) or previous != state:
                await ctx.status_update(key, status_text)
            if key == "badword" and state and not g.get("filters"):
                await ctx.reply("ℹ️ لیست فیلتر خالی است؛ با دستور «فیلتر [کلمه]» "
                                "کلمات دلخواه را اضافه کنید.")
            return True

    return False


async def _deny(ctx, who):
    await ctx.err(f"این دستور فقط توسط {who} قابل اجراست.")
    return True
