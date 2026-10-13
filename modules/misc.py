# -*- coding: utf-8 -*-
"""لقب و اصل، خوشامدگویی، قوانین، یادگیری/سخنگو، فونت، قیمت‌ها و راهنما."""

import re
import random
import os

import config
from core import ranks, utils
from core.db import db
from modules import sassy_ai

PRICE_ENDPOINTS = {
    "currency": "https://api.tgju.org/v1/market/indicator/summary-table-data/price_dollar_rl",
    "crypto": "https://api.coingecko.com/api/v3/simple/price?ids=bitcoin,ethereum,tether,binancecoin,solana,dogecoin&vs_currencies=usd&include_24hr_change=true",
}

# شاخص‌های TGJU برای نمایش مرتب طلا و سکه. TGJU قیمت‌های خود را در API به ریال می‌دهد.
GOLD_COIN_ITEMS = [
    ("طلای ۱۸ عیار", "geram18"),
    ("طلای ۲۴ عیار", "geram24"),
    ("مثقال طلا", "mesghal"),
    ("سکه امامی", "coin_emami"),
    ("سکه بهار آزادی", "coin_bahar"),
    ("نیم سکه", "coin_nim"),
    ("ربع سکه", "coin_rob"),
    ("سکه گرمی", "coin_gerami"),
]

def _clean_num(value):
    """تبدیل مقدار API به عدد قابل نمایش؛ HTML و جداکننده‌ها را هم پاک می‌کند."""
    if value is None:
        return None
    raw = re.sub(r"<[^>]+>", "", str(value)).replace(",", "").replace("٬", "").strip()
    raw = re.sub(r"[^0-9.\-]", "", raw)
    try:
        return float(raw)
    except Exception:
        return None

def _fmt_price_rial(value):
    n = _clean_num(value)
    if n is None:
        return "—"
    # قیمت بازار در پیام ربات به تومان خواناتر است.
    toman = n / 10
    return f"{toman:,.0f} تومان"

def _extract_tgju_price(data):
    """چند شکل پاسخ TGJU را پشتیبانی می‌کند تا با تغییر جزئی JSON خراب نشود."""
    if not isinstance(data, dict):
        return None
    candidates = [data.get("price"), data.get("p"), data.get("value"), data.get("last")]
    obj = data.get("data")
    if isinstance(obj, dict):
        candidates += [obj.get("price"), obj.get("p"), obj.get("value"), obj.get("last")]
    if isinstance(obj, list) and obj:
        row = obj[0]
        if isinstance(row, (list, tuple)):
            candidates += list(row)
        elif isinstance(row, dict):
            candidates += [row.get("price"), row.get("p"), row.get("value"), row.get("last")]
    for value in candidates:
        n = _clean_num(value)
        if n is not None:
            return n
    return None


# راهنمای قابل‌کپی: هر دستور به‌صورت مستقل داخل code-span نمایش داده می‌شود.
# در SplusLib API عمومی فعلی clipboard-copy native مستند نشده؛ بنابراین
# از قابلیت مطمئنِ code formatting استفاده می‌کنیم و هر دستور را جدا می‌فرستیم.
HELP_SECTIONS = [
    ("👑 مقام و فعال‌سازی", [
        "فعال", "غیرفعال", "تنظیم ادمین [ریپلای/آیدی/@username]", "تنظیم ویژه [ریپلای/آیدی/@username]",
        "تنظیم مالک [ریپلای/آیدی]", "انتقال مالکیت [ریپلای/آیدی]", "برکناری [ریپلای/آیدی]",
        "لیست ادمین", "لیست ویژه", "مقامم", "/admin",
    ]),
    ("🔇 مدیریت کاربران", [
        "سکوت [مدت] [ریپلای/آیدی]", "حذف سکوت [ریپلای/آیدی]", "پاکسازی پیام‌های سکوت [ریپلای/آیدی]",
        "بن", "بن همه", "حذف بن همه", "اخراج", "حذف بن", "آن بن", "کیل", "سیک", "حذف کیل", "آن سیک",
        "بلاک", "حذف بلاک", "معاف", "حذف معاف", "لیست بن", "لیست کیل", "لیست بلاک", "لیست معاف",
        "اخطار", "حذف اخطار", "کاهش اخطار", "پاکسازی اخطار", "اخطارم", "حذف تمام اخطارها",
        "حذف [تعداد]", "پین", "حذف پین", "ری اکت 🫠 [ریپلای]", "ری‌اکت 🫠 [ریپلای]", "حذف ری اکت 🫠 [ریپلای]", "ریپ سگتم [روی پیام کاربر]", "ریپپ سگتم [روی پیام کاربر]", "ریپ متن => پاسخ", "حذف ریپ متن", "حذف ریپ سلام", "لیست ریپ", "پاسخ سریع نام => متن", "پاسخ سریع نام", "حذف پاسخ سریع نام", "لیست پاسخ سریع",
    ]),
    ("🔒 قفل و گروه", [
        "لینک قفل", "عکس قفل", "ویس قفل", "اسپم قفل", "قفل حجم فایل روشن", "قفل حجم فایل خاموش",
        "تنظیم حجم فایل [مگابایت]", "قفل کد هنگ‌کننده روشن", "قفل کد هنگ‌کننده خاموش", "ضدتکرار روشن/خاموش", "ضدفلود روشن/خاموش", "تنظیم ضدفلود [تعداد] [ثانیه]", "اسلومود [ثانیه]", "قفل ویرایش روشن/خاموش", "هشدار ویرایش روشن/خاموش", "آمار پیشرفته",
        "گروه بسته", "گروه باز", "قفل متادیتا", "باز کردن متادیتا", "وضعیت متادیتا", "تنظیم نام گروه [متن]", "تنظیم بیو گروه [متن]", "لینک", "اپدیت گروه",
        "تعداد درخواست ها", "لیست درخواست ها", "قبول کردن درخواست [تعداد]", "رد کردن درخواست [تعداد]",
    ]),
    ("🎙 ویس‌کال و موزیک", [
        "کال", "قطع کال", "قطعکال", "بستن مایک [ریپلای/آیدی]", "باز کردن مایک [ریپلای/آیدی]",
        "پخش", "پخش <نام آهنگ>", "پخش <شماره>", "پخش با ریپلای", "پخش #لینک", "پخش از اول",
        "بعدی", "قبلی", "پلی لیست", "لیست آهنگ", "توقف", "قطع پخش", "قطع موزیک", "دانلود با ریپلای",
        "فایل [نام یا شماره]", "تنظیم صدا [0-100]", "کم", "زیاد", "بیصدا", "با صدا",
        "تنظیم سرعت [0.25 تا 3]", "جلو [ثانیه]", "عقب [ثانیه]", "تارگت @Channel",
        "تکرار فعال", "تکرار غیرفعال",
    ]),
    ("📋 لیست‌ها", [
        "لیست بن", "لیست کیل", "لیست بلاک", "لیست معاف", "لیست سکوت", "لیست مجاز", "لیست فیلتر",
        "پاکسازی لیست بن", "پاکسازی لیست کیل", "پاکسازی لیست بلاک", "پاکسازی لیست معاف",
        "پاکسازی لیست سکوت", "پاکسازی لیست مجاز", "پاکسازی لیست فیلتر",
    ]),
    ("📊 آمار و رتبه", [
        "آمار", "آمارم", "آمارش", "آمار [آیدی]", "آمار گروه", "آمار هفتگی", "فعالیت",
        "سطح", "نشان ها", "لیست برترین‌ها", "تگ [تعداد]", "تگ همه", "تگ ادمین", "تگ فعال",
        "مقامم", "لیست ادمین", "لیست ویژه",
    ]),
    ("📚 آموزش و سخنگو", [
        "یادگیری - [سوال] - [پاسخ]", "حذف یادگیری - [سوال]", "لیست یادگیری ها",
        "سخنگو روشن", "سخنگو خاموش", "سخنگو شخصی روشن", "سخنگو شخصی خاموش",
        "هوش مصنوعی روشن", "هوش مصنوعی خاموش", "هوش مصنوعی فعال", "هوش مصنوعی غیرفعال",
        "یادگیری روشن", "یادگیری خاموش",
    ]),
    ("🎮 بازی و سرگرمی", [
        "بازی ریاضی", "بازی کلمات", "بازی حدس عدد", "جواب [پاسخ]", "دیالوگ", "انگیزشی", "لطیفه",
        "حقیقت جالب", "جمله فلسفی", "فال حافظ", "فال روز", "معما", "جواب معما", "چالش",
        "حدیث", "احادیث", "خاطره", "خاطرات", "داستان", "داستان‌ها", "تست شخصیت [گزینه]",
        "طالع بینی [ماه تولد]", "سن [تاریخ تولد]",
    ]),
    ("🕌 مذهبی", ["دعا", "حکم شرعی", "قبله", "رمضان"]),
    ("🧰 ابزارها", [
        "+سوال", "ساعت", "فونت [متن]", "سرچ [نام برنامه]", "عکس [موضوع]", "اخبار", "بیوگرافی",
        "قیمت ارز", "ارز", "ارزدیجیتال", "پیشنهاد فیلم", "نکته انگلیسی", "سلامتی", "تاس زوج", "تاس فرد",
    ]),
    ("🕵️ تکمیلی", [
        "راهنما", "راهنما 2", "راهنما 3", "راهنما 4", "راهنما 5", "سازنده", "اطلاعات من", "مشخصات من",
        "داشبورد", "تنظیمات", "ثبت لینک [لینک]", "حذف لینک", "تایمر پیام [مدت]", "حذف تایمر [شناسه]",
        "تلفن", "پروفایل", "عضویت", "هدایت پیام", "ذخیره اطلاعات", "بازیابی [کد]",
    ]),
]


def _code_command(command: str) -> str:
    command = str(command).replace('`', 'ʼ')
    return f"`{command}`"


# سه بخش در هر صفحه تا پیام‌ها بیش از حد بزرگ نشوند.
HELP_PAGES = [HELP_SECTIONS[i:i + 3] for i in range(0, len(HELP_SECTIONS), 3)]


def _help_page(page: int = 1) -> str:
    page = max(1, min(int(page), len(HELP_PAGES)))
    lines = [
        "🕵️ FBI — راهنمای دستورات",
        "",
        "📋 هر دستور یک کادر مستقل و جداگانه دارد.",
        "روی همان دستور کپی کنید؛ بقیهٔ دستورات داخل آن نیستند.",
        "",
    ]
    for title, commands in HELP_PAGES[page - 1]:
        lines.append(title)
        lines.extend(f"• {_code_command(command)}" for command in commands)
        lines.append("")
    lines.append(f"📖 صفحه {page} از {len(HELP_PAGES)}")
    if page < len(HELP_PAGES):
        lines.append(f"برای صفحه بعد: {_code_command(f'راهنما {page + 1}')}")
    if page > 1:
        lines.append(f"برای صفحه قبل: {_code_command(f'راهنما {page - 1}')}")
    return "\n".join(lines).strip()


HELP = _help_page(1)
HELP2 = _help_page(2)
HELP3 = _help_page(3)


async def _broadcast_targets():
    return [guid for guid, gg in db.data["groups"].items() if gg.get("active")]


async def _broadcast_text(ctx, title, payload):
    targets = await _broadcast_targets()
    sent = failed = 0
    body = f"{title}\n\n{payload}"
    for guid in targets:
        try:
            res = await ctx.api.send(guid, body)
            if res is not None:
                sent += 1
            else:
                failed += 1
        except Exception:
            failed += 1
    return sent, failed


async def _broadcast_poster(ctx, caption):
    if not ctx.reply_id:
        await ctx.err("برای ارسال پوستر، روی عکس/تصویر موردنظر ریپلای کنید.")
        return True
    path = await ctx.api.download_message_file(
        ctx.guid, ctx.reply_id, config.TEMP_DIR, default_ext="jpg"
    )
    if not path or not os.path.isfile(path):
        await ctx.err("دانلود تصویر ریپلای‌شده انجام نشد. روی یک عکس معتبر ریپلای کنید.")
        return True
    targets = await _broadcast_targets()
    sent = failed = 0
    caption = caption or "📢 اطلاع‌رسانی FBI"
    for guid in targets:
        try:
            res = await ctx.api.send_photo(guid, path, caption=caption)
            if res is not None:
                sent += 1
            else:
                failed += 1
        except Exception:
            failed += 1
    try:
        if os.path.exists(path):
            os.remove(path)
    except OSError:
        pass
    await ctx.reply(
        f"🖼 پوستر به {utils.fa_digits(sent)} گروه ارسال شد."
        + (f"\n❌ ناموفق: {utils.fa_digits(failed)}" if failed else "")
    )
    return True


async def handle(ctx) -> bool:
    text = ctx.text
    g = ctx.g

    # ------------------------------------------------ راهنما
    if text in ("راهنما", "دستورات", "help", "کمک"):
        await ctx.reply(HELP)
        return True

    m_help = re.fullmatch(r"راهنما\s+(\d+)", text)
    if m_help:
        page = int(m_help.group(1))
        if 1 <= page <= len(HELP_PAGES):
            await ctx.reply(_help_page(page))
            return True
        await ctx.err(f"شماره صفحه راهنما باید بین 1 تا {len(HELP_PAGES)} باشد.")
        return True

    # ------------------------------------------------ پنل مدیریت سازنده
    if text in ("/admin", "پنل", "پنل مدیریت"):
        if ctx.is_group and ctx.rank < ranks.ADMIN:
            return False
        if ctx.is_private and ctx.rank < ranks.CREATOR:
            return False
        groups = db.data["groups"]
        active = sum(1 for gg in groups.values() if gg.get("active"))
        if ctx.is_group:
            await ctx.reply(
                "🕵️ پنل مدیریت FBI\n\n"
                "دستورات مدیریت همین گروه:\n"
                "• تنظیم ادمین / تنظیم ویژه\n"
                "• برکناری\n"
                "• لیست ادمین / لیست ویژه\n"
                "• فعال / غیرفعال\n"
                "• قفل [نوع] روشن/خاموش\n"
                "• سکوت / بن / کیل / بلاک\n"
                "• کال / قطع کال / بستن میکروفون\n"
                "• پخش / قطع پخش\n"
                "• آمار گروه / آمارش\n"
                "• تنظیم نام گروه / تنظیم بیو گروه\n"
                "• ذخیره اطلاعات / بازیابی"
            )
        else:
            await ctx.reply(
                "🕵️ پنل مدیریت سازنده\n\n"
                f"📊 گروه‌های ثبت‌شده: {utils.fa_digits(len(groups))}\n"
                f"✅ گروه‌های فعال: {utils.fa_digits(active)}\n\n"
                "دستورات:\n"
                "• همگانی [متن]\n"
                "• آمار سازنده\n"
                "• دیباگ متد [نام]"
            )
        return True

    if text == "آمار سازنده":
        if ctx.rank < ranks.CREATOR:
            return False
        groups = db.data["groups"]
        users = db.data["users"]
        active = sum(1 for gg in groups.values() if gg.get("active"))
        await ctx.reply(
            "📊 آمار کلی FBI\n\n"
            f"👥 گروه‌های ثبت‌شده: {utils.fa_digits(len(groups))}\n"
            f"✅ گروه‌های فعال: {utils.fa_digits(active)}\n"
            f"👤 کاربران ثبت‌شده: {utils.fa_digits(len(users))}"
        )
        return True

    # ------------------------------------------------ پیام سراسری سازنده
    if text.startswith("پیام سراسری اطلاع رسانی"):
        if ctx.rank < ranks.CREATOR:
            return False
        payload = text[len("پیام سراسری اطلاع رسانی"):].strip()
        if not payload:
            await ctx.err("متن اطلاع‌رسانی را بعد از دستور بنویسید.")
            return True
        sent, failed = await _broadcast_text(ctx, "📢 اطلاع‌رسانی FBI", payload)
        await ctx.reply(
            f"✅ اطلاع‌رسانی به {utils.fa_digits(sent)} گروه ارسال شد."
            + (f"\n❌ ناموفق: {utils.fa_digits(failed)}" if failed else "")
        )
        return True

    if text.startswith("پیام سراسری پوستر"):
        if ctx.rank < ranks.CREATOR:
            return False
        caption = text[len("پیام سراسری پوستر"):].strip()
        return await _broadcast_poster(ctx, caption)

    # سازگاری با دستور قدیمی
    if text.startswith("همگانی"):
        if ctx.rank < ranks.CREATOR:
            return False
        payload = text[len("همگانی"):].strip()
        if not payload:
            await ctx.err("برای همگانی از «پیام سراسری اطلاع رسانی» استفاده کنید.")
            return True
        sent, failed = await _broadcast_text(ctx, "📢 اطلاع‌رسانی FBI", payload)
        await ctx.reply(
            f"✅ پیام به {utils.fa_digits(sent)} گروه ارسال شد."
            + (f"\n❌ ناموفق: {utils.fa_digits(failed)}" if failed else "")
        )
        return True

    # ------------------------------------------------ فهرست گروه‌ها برای سازنده
    if text in ("لیست", "لیست گروه ها", "لیست گروه‌ها", "لیست فعال"):
        if ctx.rank < ranks.CREATOR:
            return False
        groups = db.data["groups"]
        active_items = [(guid, gg) for guid, gg in groups.items() if gg.get("active")]
        items = active_items if text == "لیست فعال" else list(groups.items())
        if not items:
            await ctx.reply("📋 لیستی برای نمایش وجود ندارد.")
            return True
        lines = [
            "📋 گروه‌های فعال:" if text == "لیست فعال" else "📋 فهرست گروه‌های ثبت‌شده:",
            ""
        ]
        for i, (guid, gg) in enumerate(items[:100], 1):
            title = gg.get("title") or guid
            lines.append(f"{utils.fa_digits(i)}. {title} — `{guid}`")
        if len(items) > 100:
            lines.append(f"\n… و {utils.fa_digits(len(items)-100)} مورد دیگر")
        await ctx.reply("\n".join(lines))
        return True

    # ------------------------------------------------ دیباگ (فقط سازنده)
    m = re.match(r"^دیباگ متد (.+)$", text)
    if m:
        if ctx.rank < ranks.CREATOR:
            return False
        names = ctx.api.find_methods(m.group(1).strip())
        body = "\n".join(f"• {n}" for n in names[:60]) or "چیزی پیدا نشد."
        await ctx.reply(f"🔧 متدهای شامل «{m.group(1)}»:\n\n{body}")
        return True

    # ------------------------------------------------ لقب و اصل
    for fa, key in (("لقب", "nick"), ("اصل", "title")):
        m = re.match(rf"^تنظیم {fa}(?: (.+))?$", text, re.S)
        if m:
            value = (m.group(1) or "").strip()
            target = await ctx.target()
            if target and target != ctx.author:
                if ctx.rank < ranks.SPECIAL:
                    await ctx.err("فقط مالک و ادمین‌های ویژه می‌توانند برای "
                                  "دیگران لقب/اصل تنظیم کنند.")
                    return True
                if db.user(target).get(f"privacy_{key}") == "self":
                    await ctx.err("این کاربر در تنظیمات حریم خصوصی اجازه نداده "
                                  "دیگران برایش لقب/اصل تنظیم کنند.")
                    return True
            else:
                target = ctx.author
            if not value and ctx.reply_id:
                await ctx.err(f"{fa} را بعد از دستور بنویسید. مثال: تنظیم {fa} ماه")
                return True
            db.user(target)[key] = value[:60]
            db.save("users")
            name = await ctx.target_link(target)
            await ctx.ok(f"{fa} «{value}» برای {name} ثبت شد.")
            return True

        if text == f"حذف {fa}":
            db.user(ctx.author)[key] = ""
            db.save("users")
            await ctx.ok(f"{fa} شما حذف شد.")
            return True

        if text == f"{fa}م":
            value = db.user(ctx.author).get(key)
            await ctx.reply(f"🏷 {fa} شما: {value or '— ثبت نشده —'}")
            return True

        if text == f"{fa}ش":
            target = await ctx.target()
            if not target:
                await ctx.err("روی پیام کاربر ریپلای بزنید.")
                return True
            value = db.user(target).get(key)
            name = await ctx.target_link(target)
            await ctx.reply(f"🏷 {fa} {name}: {value or '— ثبت نشده —'}")
            return True

    if text in ("اطلاعات من", "مشخصات من"):
        user = db.user(ctx.author)
        self_link = await ctx.target_link(ctx.author, fallback_name=user.get("name") or "کاربر")
        await ctx.reply("\n".join([
            "👤 اطلاعات شما", "",
            f"• نام: {self_link}",
            f"• لقب: {user.get('nick') or '—'}",
            f"• اصل: {user.get('title') or '—'}",
            f"• مقام: {ctx.rank_name()}",
            f"• شناسه: `{ctx.author}`",
        ]))
        return True

    m = re.match(r"^حریم خصوصی (لقب|اصل) (همه|فقط خودم)$", text)
    if m:
        key = "nick" if m.group(1) == "لقب" else "title"
        db.user(ctx.author)[f"privacy_{key}"] = \
            "self" if m.group(2) == "فقط خودم" else "all"
        db.save("users")
        await ctx.ok(f"حریم خصوصی {m.group(1)} روی «{m.group(2)}» تنظیم شد.")
        return True


    # ------------------------------------------------ حریم خصوصی حساب
    privacy_map = {
        "آخرین بازدید": "last_seen",
        "تلفن": "phone",
        "پروفایل": "profile",
        "عضویت": "group_invites",
        "هدایت پیام": "forwarding",
    }
    m = re.match(r"^حریم خصوصی (آخرین بازدید|تلفن|پروفایل|عضویت|هدایت پیام) (همه|فقط خودم)$", text)
    if m:
        key = privacy_map[m.group(1)]
        value = "all" if m.group(2) == "همه" else "self"
        # اول تلاش واقعی روی API؛ در صورت تفاوت endpoint، تنظیم محلی هم نگه داشته می‌شود.
        ok = False
        for kwargs in (
            {"setting": key, "value": value},
            {"privacy_type": key, "privacy_value": value},
            {"key": key, "value": value},
        ):
            try:
                res = await ctx.api.privacy(**kwargs)
                if res is not None:
                    ok = True
                    break
            except Exception:
                continue
        db.user(ctx.author)[f"privacy_{key}"] = value
        db.save("users")
        await ctx.ok(
            f"حریم خصوصی «{m.group(1)}» روی «{m.group(2)}» تنظیم شد."
            + ("" if ok else "\n⚠️ متد حریم خصوصی در نسخه فعلی RubPy پاسخ نداد؛ تنظیم در FBI ذخیره شد.")
        )
        return True

    # ------------------------------------------------ خوشامدگویی / خداحافظی
    for fa, key in (("خوشامدگویی", "welcome_text"), ("خداحافظی", "goodbye_text")):
        m = re.match(rf"^تنظیم {fa}(?: |\n)(.+)$", ctx.raw_text.strip(), re.S)
        if m:
            if ctx.rank < ranks.OWNER:
                await ctx.err("فقط مالک گروه می‌تواند این متن را تنظیم کند.")
                return True
            g[key] = m.group(1).strip()
            g["welcome" if key == "welcome_text" else "goodbye"] = True
            db.save("groups")
            await ctx.ok(f"متن {fa} ثبت و فعال شد.\n\n"
                         "متغیرها: {name} {group} {count}")
            return True

    # ------------------------------------------------ قوانین و بنر
    for fa, key in (("قوانین", "rules"), ("بنر", "banner")):
        m = re.match(rf"^تنظیم {fa}(?: |\n)(.+)$", ctx.raw_text.strip(), re.S)
        if m:
            if ctx.rank < ranks.OWNER:
                await ctx.err("فقط مالک گروه می‌تواند این متن را تنظیم کند.")
                return True
            g[key] = m.group(1).strip()
            db.save("groups")
            await ctx.ok(f"متن {fa} ثبت شد.")
            return True

        if text == fa:
            if ctx.disabled(fa):
                await ctx.err("این دستور برای کاربران عادی غیرفعال است.")
                return True
            value = g.get(key)
            await ctx.reply(f"📜 {fa} گروه:\n\n{value}" if value
                            else f"❌ {fa} برای این گروه ثبت نشده است.")
            return True

    # ------------------------------------------------ پیام مجاز
    m = re.match(r"^تنظیم پیام مجاز(?: |\n)(.+)$", ctx.raw_text.strip(), re.S)
    if m:
        if ctx.rank < ranks.OWNER:
            await ctx.err("فقط مالک گروه می‌تواند این قابلیت را تنظیم کند.")
            return True
        g["allowed_message"] = m.group(1).strip()
        g["allowed_message_active"] = True
        db.save("groups")
        await ctx.ok(
            "پیام مجاز تنظیم و فعال شد.\n\n"
            "از این پس هر پیامی غیر از متن تنظیم‌شده از کاربران عادی حذف می‌شود."
        )
        return True

    if text == "پیام مجاز":
        value = g.get("allowed_message")
        state = "فعال ✅" if g.get("allowed_message_active") else "غیرفعال ❌"
        await ctx.reply(f"📌 پیام مجاز: {value or '—'}\nوضعیت: {state}")
        return True

    if text == "حذف پیام مجاز":
        if ctx.rank < ranks.OWNER:
            await ctx.err("فقط مالک گروه می‌تواند این قابلیت را تنظیم کند.")
            return True
        g["allowed_message"] = None
        g["allowed_message_active"] = False
        db.save("groups")
        await ctx.ok("پیام مجاز حذف و غیرفعال شد.")
        return True

    # ------------------------------------------------ فونت
    m = re.match(r"^فونت (.+)$", ctx.raw_text.strip(), re.S)
    if m:
        if ctx.disabled("فونت"):
            await ctx.err("این دستور برای کاربران عادی غیرفعال است.")
            return True
        await ctx.reply(utils.font_showcase(m.group(1).strip()[:30]))
        return True

    if text in utils.FONTS_EN:
        if not ctx.reply_id:
            await ctx.err("روی پیام موردنظر ریپلای بزنید.")
            return True
        from core import api as api_mod
        res = await ctx.api.get_messages(ctx.guid, [ctx.reply_id])
        msgs = api_mod._val(res, "messages", default=[]) or []
        body = api_mod._val(msgs[0], "text", default="") if msgs else ""
        if not body:
            await ctx.err("متنی برای تبدیل پیدا نشد.")
            return True
        await ctx.reply(utils.apply_font(body[:200], text))
        return True

    # ------------------------------------------------ یادگیری / سخنگو
    # Documented syntax: «یادگیری - سوال - جواب» and «حذف یادگیری - سوال» (same store as «!»).
    if utils.normalize(text) in (utils.normalize("سخنگو شخصی روشن"), utils.normalize("سخنگو شخصی خاموش")):
        if ctx.rank < ranks.OWNER:
            await ctx.err("فقط مالک گروه می‌تواند سخنگوی شخصی را تنظیم کند.")
            return True
        g["ai_chat"] = utils.normalize(text).endswith("روشن")
        db.save("groups")
        await ctx.ok("سخنگوی شخصی " + ("روشن" if g["ai_chat"] else "خاموش") + " شد.")
        return True

    m = re.match(r"^یادگیری\s*-\s*(.+?)\s*-\s*(.+)$", ctx.raw_text.strip(), re.S)
    if m and g.get("learning"):
        if ctx.rank < ranks.ADMIN:
            await ctx.err("فقط ادمین‌ها می‌توانند به ربات کلمه یاد بدهند.")
            return True
        key = utils.normalize(m.group(1))
        answer = m.group(2).strip()
        learn = db.learn(ctx.guid)
        learn.setdefault(key, [])
        if answer not in learn[key]:
            learn[key].append(answer)
        db.save("learn")
        await ctx.ok(f"یاد گرفتم؛ در جواب «{key}» می‌گویم: {answer}")
        return True

    m = re.match(r"^حذف یادگیری\s*-\s*(.+)$", ctx.raw_text.strip(), re.S)
    if m and g.get("learning"):
        if ctx.rank < ranks.ADMIN:
            return False
        key = utils.normalize(m.group(1))
        db.learn(ctx.guid).pop(key, None)
        db.save("learn")
        await ctx.ok(f"«{key}» از یادگیری‌ها حذف شد.")
        return True

    m = re.match(r"^(.+?) ! (.+)$", ctx.raw_text.strip(), re.S)
    if m and g.get("learning"):
        if ctx.rank < ranks.ADMIN:
            await ctx.err("فقط ادمین‌ها می‌توانند به ربات کلمه یاد بدهند.")
            return True
        key = utils.normalize(m.group(1))
        learn = db.learn(ctx.guid)
        learn.setdefault(key, [])
        answer = m.group(2).strip()
        if answer not in learn[key]:
            learn[key].append(answer)
        db.save("learn")
        await ctx.ok(f"یاد گرفتم؛ در جواب «{key}» می‌گویم: {answer}")
        return True

    m = re.match(r"^(.+?) !!(?: (.+))?$", ctx.raw_text.strip(), re.S)
    if m and g.get("learning"):
        if ctx.rank < ranks.ADMIN:
            return False
        key = utils.normalize(m.group(1))
        learn = db.learn(ctx.guid)
        if m.group(2):
            answer = m.group(2).strip()
            if key in learn and answer in learn[key]:
                learn[key].remove(answer)
        else:
            learn.pop(key, None)
        db.save("learn")
        await ctx.ok("جواب‌های موردنظر حذف شدند.")
        return True

    m = re.match(r"^\? (.+)$", ctx.raw_text.strip())
    if m:
        key = utils.normalize(m.group(1))
        answers = db.learn(ctx.guid).get(key, [])
        await ctx.reply(f"🧠 جواب‌های «{key}»:\n\n" +
                        "\n".join(f"• {a}" for a in answers)
                        if answers else "چیزی برای این کلمه یاد نگرفته‌ام.")
        return True

    if utils.normalize(text) in (utils.normalize("لیست کلمات"), utils.normalize("لیست یادگیری ها")):
        if ctx.rank < ranks.ADMIN:
            return False
        words = list(db.learn(ctx.guid))
        await ctx.reply("🧠 کلمات یادگرفته‌شده:\n\n" +
                        ("\n".join(f"• {w}" for w in words[:100])
                         if words else "لیست خالی است."))
        return True

    # ------------------------------------------------ زبان
    m = re.match(r"^تنظیم زبان (فارسی|انگلیسی)$", text)
    if m:
        if ctx.rank < ranks.OWNER:
            await ctx.err("فقط مالک گروه می‌تواند زبان را تغییر دهد.")
            return True
        g["lang"] = "fa" if m.group(1) == "فارسی" else "en"
        db.save("groups")
        await ctx.ok(f"زبان گروه روی «{m.group(1)}» تنظیم شد.")
        return True

    if text == "لیست زبان":
        await ctx.reply("🌐 زبان‌های پشتیبانی‌شده:\n• فارسی\n• انگلیسی")
        return True

    # ------------------------------------------------ قیمت‌ها
    if text in ("قیمت ارز", "قیمت طلا", "قیمت ارز دیجیتال", "قیمت ارزدیجیتال"):
        if ctx.disabled(text):
            await ctx.err("این دستور برای کاربران عادی غیرفعال است.")
            return True
        kind = ("currency" if text == "قیمت ارز"
                else "gold" if text == "قیمت طلا" else "crypto")
        await ctx.reply(await _prices(kind))
        return True

    # ------------------------------------------------ حالت بدجنسی (هوش مصنوعی آفلاین)
    if text in ("روشن کردن بدجنسی", "بدجنسی روشن"):
        if ctx.rank < ranks.ADMIN:
            await ctx.err("این دستور فقط برای ادمین‌هاست.")
            return True
        g["sassy"] = True
        db.save("groups")
        await ctx.ok("باشه از الان زبونم درازتر می‌شه، خودتون خواستید 😏")
        return True

    if text in ("خاموش کردن بدجنسی", "بدجنسی خاموش"):
        if ctx.rank < ranks.ADMIN:
            await ctx.err("این دستور فقط برای ادمین‌هاست.")
            return True
        g["sassy"] = False
        db.save("groups")
        await ctx.ok("باشه بابا آروم می‌شم، غر نزنید.")
        return True

    # ------------------------------------------------ ربات بگو
    m = re.match(r"^ربات بگو (.+)$", ctx.raw_text.strip(), re.S)
    if m:
        if ctx.rank < ranks.ADMIN:
            return False
        await ctx.api.send(ctx.guid, m.group(1).strip())
        return True

    return False


# ------------------------------------------------------------ سخنگو
SMALL_TALK = {
    "سلام": ["سلام 🌹", "سلام خوش اومدی ☘️", "سلام بر شما 🌱"],
    "خوبی": ["مرسی، تو خوبی؟", "عالیم 😎", "بد نیستم، تو چطوری؟"],
    "خداحافظ": ["خدانگهدار 🌹", "به سلامت ☘️"],
    "ممنون": ["خواهش می‌کنم 🌱", "قابلی نداشت ❤️"],
    "ربات": ["جانم؟", "بله، در خدمتم ⚡"],
}


async def speaker(ctx) -> bool:
    """سخنگوی رفتاری: سلام‌ها و Reply به خود FBI را خودکار جواب می‌دهد."""
    g = ctx.g
    if not g.get("speaker"):
        return False
    text = ctx.text

    direct_reply = False
    try:
        should, direct_reply = await sassy_ai.should_respond(ctx)
    except Exception:
        should, direct_reply = (bool(ctx.reply_id), False)
    if not should:
        return False

    learned = db.learn(ctx.guid).get(text)
    if learned:
        await ctx.reply(random.choice(learned))
        return True

    if g.get("ai_chat"):
        answer = await sassy_ai.generate(ctx, direct_to_bot=direct_reply)
        await ctx.reply(answer)
        return True

    if g.get("sassy") or sassy_ai.is_greeting(text):
        await ctx.reply(sassy_ai.reply_for(text))
        return True

    for key, answers in SMALL_TALK.items():
        if key in text:
            await ctx.reply(random.choice(answers))
            return True
    return False


# ------------------------------------------------------------ قیمت‌ها
async def _prices(kind) -> str:
    try:
        import aiohttp
    except ImportError:
        return "❌ کتابخانه aiohttp نصب نیست."

    timeout = aiohttp.ClientTimeout(total=12)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            if kind == "gold":
                lines = ["🥇 قیمت طلا و سکه", "━━━━━━━━━━━━"]
                for title, symbol in GOLD_COIN_ITEMS:
                    url = f"https://api.tgju.org/v1/market/indicator/summary-table-data/{symbol}"
                    async with session.get(url) as resp:
                        data = await resp.json(content_type=None)
                    price = _extract_tgju_price(data)
                    if price is not None:
                        lines.append(f"• {title}: {_fmt_price_rial(price)}")
                if len(lines) == 2:
                    return "❌ اطلاعات طلا و سکه دریافت نشد."
                lines += ["━━━━━━━━━━━━", f"🕐 {utils.date_text()} | {utils.clock_text()}", "📌 منبع: TGJU"]
                return "\n".join(lines)

            if kind == "currency":
                async with session.get(PRICE_ENDPOINTS["currency"]) as resp:
                    data = await resp.json(content_type=None)
                price = _extract_tgju_price(data)
                if price is None:
                    return "❌ قیمت دلار دریافت نشد."
                return ("💵 قیمت دلار آزاد\n"
                        "━━━━━━━━━━━━\n"
                        f"• دلار: {_fmt_price_rial(price)}\n"
                        "━━━━━━━━━━━━\n"
                        f"🕐 {utils.date_text()} | {utils.clock_text()}\n"
                        "📌 منبع: TGJU")

            async with session.get(PRICE_ENDPOINTS["crypto"]) as resp:
                data = await resp.json(content_type=None)
    except Exception as exc:
        db.log("price", str(exc))
        return "❌ دریافت قیمت‌ها در حال حاضر ممکن نیست. بعداً دوباره تلاش کنید."

    titles = {"bitcoin": "بیت‌کوین", "ethereum": "اتریوم", "tether": "تتر",
              "binancecoin": "بی‌ان‌بی", "solana": "سولانا", "dogecoin": "دوج"}
    lines = ["💎 قیمت ارزهای دیجیتال", "━━━━━━━━━━━━"]
    for key, title in titles.items():
        item = data.get(key) or {}
        price = item.get("usd")
        change = item.get("usd_24h_change")
        if price is None:
            continue
        arrow = "🟢" if (change or 0) >= 0 else "🔴"
        suffix = f" ({change:+.2f}%)" if change is not None else ""
        lines.append(f"{arrow} {title}: ${price:,.2f}{suffix}")
    lines += ["━━━━━━━━━━━━", f"🕐 {utils.date_text()} | {utils.clock_text()}"]
    return "\n".join(lines) if len(lines) > 2 else "❌ داده‌ای دریافت نشد."

