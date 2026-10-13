# -*- coding: utf-8 -*-
"""ابزارهای مدیریتی گروه: حذف پیام، پین، کال، لینک، درخواست عضویت، تگ و منشن."""

import re
import random

from core import api as api_mod
from core import ranks, utils
from core.db import db

MENTION_LINES = [
    "کجایی؟ گروه بدون تو خالیه 🌱",
    "یه سر بزن ببین چه خبره 👀",
    "بیا که جات خالیه ⚡",
    "پیام‌ها رو از دست ندی ☘️",
    "یه نگاه بنداز، ارزششو داره 🍃",
    "سلام! حضور شما لازمه 🌹",
]

ACCESS_MAP = {
    "ارسال پیام": "SendMessages",
    "افزودن عضو": "AddMember",
    "مشاهده مدیران": "ViewAdmins",
    "مشاهده اعضا": "ViewMembers",
    "حفاظت از محتوا": "RestrictContentSaving",
    "پیام های شیشه ای": "SendGlassMessage",
    "تاریخچه": "ChatHistoryForNewMembers",
}


def _starts_with_command(text, names):
    """True for the bare command or the command followed by a target (e.g. «بستن مایک @user»)."""
    from core import utils as _u
    t = _u.normalize(text)
    return any(t == _u.normalize(n) or t.startswith(_u.normalize(n) + " ") for n in names)


async def handle(ctx) -> bool:
    text = ctx.text
    g = ctx.g

    # ------------------------------------------------ حذف پیام
    if text == "حذف" or re.match(r"^حذف \d+$", text):
        if ctx.rank < ranks.ADMIN:
            return await _deny(ctx)
        count = int(text[5:]) if len(text) > 4 else 0

        if ctx.reply_id and not count:
            await ctx.api.delete(ctx.guid, [ctx.reply_id, ctx.message_id])
            return True

        count = min(max(count, 1), 250)
        target = await ctx.target() if ctx.reply_id else None
        removed = await _bulk_delete(ctx, count, only_user=target)
        await ctx.reply(f"🗑 {utils.fa_digits(removed)} پیام حذف شد.")
        return True

    m = re.match(r"^حذف (شیشه|ادیت|ویرایش) (\d+)$", text)
    if m:
        if ctx.rank < ranks.SPECIAL:
            return await _deny(ctx, "ادمین ویژه و مالک")
        kind = "edited" if m.group(1) in ("ادیت", "ویرایش") else "glass"
        removed = await _bulk_delete(ctx, int(m.group(2)), kind=kind)
        await ctx.reply(f"🗑 {utils.fa_digits(removed)} پیام حذف شد.")
        return True

    # ------------------------------------------------ پین
    if text in ("پین", "سنجاق"):
        if ctx.rank < ranks.ADMIN:
            return await _deny(ctx)
        if not ctx.reply_id:
            await ctx.err("روی پیام موردنظر ریپلای بزنید.")
            return True
        await ctx.api.pin(ctx.guid, ctx.reply_id, "Pin")
        await ctx.ok("پیام سنجاق شد. 📌")
        return True

    if text in ("حذف پین", "حذف سنجاق", "انپین"):
        if ctx.rank < ranks.ADMIN:
            return await _deny(ctx)
        if not ctx.reply_id:
            await ctx.err("روی پیام سنجاق‌شده ریپلای بزنید.")
            return True
        await ctx.api.pin(ctx.guid, ctx.reply_id, "Unpin")
        await ctx.ok("سنجاق پیام برداشته شد.")
        return True

    # ------------------------------------------------ کال (ویس چت)
    if text == "کال":
        if ctx.rank < ranks.SPECIAL:
            return await _deny(ctx, "ادمین ویژه و مالک")
        ok, result = await ctx.api.voice_join(ctx.guid)
        if not ok:
            detail = str(result) if result else "خطای نامشخص"
            await ctx.err(
                "ویس‌کال ساخته/متصل نشد.\n\n"
                f"جزئیات: {detail}\n\n"
                "دسترسی مدیریت کال باید برای FBI فعال باشد و LiveKit هم باید در محیط نصب باشد."
            )
        else:
            slug = None
            link = None
            joined = False
            if isinstance(result, dict):
                slug = result.get("slug") or result.get("call_id") or result.get("id")
                link = result.get("link") or result.get("join_link") or result.get("url")
                joined = bool(result.get("already_joined"))
            # بعضی سرورهای Splus لینک را در create برنمی‌گردانند؛ اطلاعات کال را دوباره می‌گیریم.
            if slug and not link:
                try:
                    info = await ctx.api.voice_info(str(slug))
                    if isinstance(info, dict):
                        link = info.get("link") or info.get("join_link") or info.get("url")
                except Exception:
                    pass
            msg = "🎙 FBI واقعاً وارد ویس‌کال شد." if not joined else "🎙 FBI از قبل داخل ویس‌کال بود."
            if link and isinstance(link, str) and link.startswith(("http://", "https://")):
                msg += f"\n\n🔗 لینک ورود به کال:\n{link}"
            elif slug:
                msg += f"\n\nشناسه کال: `{slug}`"
            else:
                msg += "\n\nلینک ورود از پاسخ سرویس دریافت نشد."
            await ctx.ok(msg)
        return True

    if text in ("قطع کال", "قطعکال", "پایان کال", "پایانکال"):
        if ctx.rank < ranks.SPECIAL:
            return await _deny(ctx, "ادمین ویژه و مالک")
        vid = await ctx.api.active_voice_id(ctx.guid)
        ok, res = await ctx.api.voice_discard(ctx.guid, vid)
        if not ok:
            await ctx.err("قطع کال ناموفق بود؛ دسترسی مدیریت ویس‌کال را بررسی کنید.")
        else:
            await ctx.ok("کال و پخش صوتی گروه قطع شد. 🔇")
        return True

    if _starts_with_command(text, ("بستن میکروفون", "بستن میک", "بستن مایک", "میک بسته", "مایک بسته")):
        if ctx.rank < ranks.SPECIAL:
            return await _deny(ctx, "ادمین ویژه و مالک")
        target = await ctx.target()
        vid = await ctx.api.active_voice_id(ctx.guid)
        if not vid:
            await ctx.err("در حال حاضر کالی در گروه فعال نیست.")
            return True
        if target:
            await ctx.api.voice_mute(ctx.guid, vid, target, True)
            await ctx.ok(f"میکروفون {await ctx.target_link(target)} بسته شد. 🔇")
        else:
            res = await ctx.api.voice_participants(ctx.guid, vid)
            count = 0
            for member in api_mod.members_of(res):
                guid = api_mod.guid_of(member)
                if guid and not await ctx.protection_reason(guid):
                    await ctx.api.voice_mute(ctx.guid, vid, guid, True)
                    count += 1
            await ctx.ok(f"میکروفون {utils.fa_digits(count)} نفر بسته شد. 🔇")
        return True

    if _starts_with_command(text, ("باز کردن میکروفون", "باز کردن میک", "باز کردن مایک", "میک باز", "مایک باز")):
        if ctx.rank < ranks.SPECIAL:
            return await _deny(ctx, "ادمین ویژه و مالک")
        target = await ctx.target()
        vid = await ctx.api.active_voice_id(ctx.guid)
        if vid and target:
            await ctx.api.voice_mute(ctx.guid, vid, target, False)
            await ctx.ok("میکروفون باز شد. 🔊")
        else:
            await ctx.err("کال فعال نیست یا کاربر مشخص نشده.")
        return True

    # ------------------------------------------------ باز/بسته کردن گروه
    from core import utils as _u
    if _u.normalize(text) == _u.normalize("وضعیت"):
        from modules import locks as _locks
        on = [title for key, title in _locks.TITLES.items() if g.get("locks", {}).get(key)]
        lines = [
            "⚙️ وضعیت گروه",
            f"• ربات: {'فعال' if g.get('active') else 'غیرفعال'}",
            f"• ارسال پیام اعضای عادی: {'بسته' if g.get('closed') else 'باز'}",
            f"• قفل‌های روشن: {'، '.join(on) if on else 'هیچ‌کدام'}",
            f"• کاربران در سکوت: {_u.fa_digits(len(g.get('mute') or {}))}",
            f"• بن‌های ثبت‌شده توسط ربات: {_u.fa_digits(len(g.get('ban_record') or {}))}",
        ]
        await ctx.reply("\n".join(lines))
        return True

    if _u.normalize(text) == _u.normalize("سازنده"):
        from config import CREATOR_IDS
        ids = "، ".join(str(x) for x in CREATOR_IDS)
        await ctx.reply(f"🛡 شناسه‌های رسمی سازندگان FBI:\n{ids}\n\n🌹 ساخته شده توسط رادوین")
        return True

    if text == "گروه بسته":
        if ctx.rank < ranks.SPECIAL:
            return await _deny(ctx, "ادمین ویژه و مالک")
        server_ok = False
        try:
            server_ok = bool(await ctx.api.close_group_permissions(ctx.guid))
        except Exception:
            server_ok = False
        if server_ok:
            g["closed"] = True
            g["closed_until"] = 0  # بستن دستی = بدون زمان انقضا
            db.save("groups")
        await ctx.reply(
            "🔒 گروه بسته شد؛ تمام دسترسی‌های ارسال اعضای عادی بسته شد."
            + ("\n✅ Permission قبلی ذخیره شد و برای بازگشایی محفوظ است." if server_ok else
               "\n⚠️ بستن Permission سروری انجام نشد؛ تغییر محلی هم ثبت نشد.")
        )
        return True

    if text == "گروه باز":
        if ctx.rank < ranks.SPECIAL:
            return await _deny(ctx, "ادمین ویژه و مالک")
        server_ok = False
        try:
            server_ok = bool(await ctx.api.open_group_permissions(ctx.guid))
        except Exception:
            server_ok = False
        if server_ok:
            g["closed"] = False
            g["closed_until"] = 0
            g["opened_at"] = __import__("time").time()
            db.save("groups")
        await ctx.reply(
            "🔓 گروه باز شد." +
            ("\n✅ تمام Permissionهای قبلی گروه دقیقاً برگردانده شد." if server_ok else
             "\n⚠️ Permission قبلی برنگشت؛ گروه در حالت بسته نگه داشته شد.")
        )
        return True

    # ------------------------------------------------ تنظیم دسترسی گروه
    for fa, key in ACCESS_MAP.items():
        m = re.match(rf"^{fa} (روشن|خاموش)$", text)
        if m:
            if ctx.rank < ranks.OWNER:
                return await _deny(ctx, "مالک گروه")
            current = await ctx.api.get_default_access(ctx.guid)
            access = api_mod._val(current, "access_list", "default_access",
                                  default=[]) or []
            access = list(access)
            if m.group(1) == "روشن" and key not in access:
                access.append(key)
            elif m.group(1) == "خاموش" and key in access:
                access.remove(key)
            # Keep the complete access list in the local DB and let the API
            # adapter apply the server-level SendMessages default permission.
            await ctx.api.default_access(ctx.guid, access)
            server_ok = True
            if key == "SendMessages":
                try:
                    server_ok = bool(await ctx.api.set_group_closed(ctx.guid, m.group(1) == "خاموش"))
                except Exception:
                    server_ok = False
            suffix = "" if server_ok else "\n⚠️ Permission سروری اعمال نشد."
            await ctx.ok(f"«{fa}» {m.group(1)} شد.{suffix}")
            return True

    m = re.match(r"^تایمر پیام (.+)$", text)
    if m:
        if ctx.rank < ranks.OWNER:
            return await _deny(ctx, "مالک گروه")
        seconds = utils.parse_duration(m.group(1)) or 0
        await ctx.api.set_timer(ctx.guid, seconds)
        await ctx.ok(f"تایمر حذف پیام‌های گروه روی "
                     f"{utils.human_duration(seconds)} تنظیم شد.")
        return True

    # ------------------------------------------------ لینک گروه
    if text in ("لینک گروه", "لینک"):
        if ctx.disabled("لینک"):
            await ctx.err("این دستور برای کاربران عادی غیرفعال است.")
            return True
        if g.get("link"):
            await ctx.reply(f"🔗 لینک گروه:\n{g['link']}")
            return True
        res = await ctx.api.group_link(ctx.guid)
        link = api_mod._val(res, "join_link", "link", default=None)
        if link:
            await ctx.reply(f"🔗 لینک گروه:\n{link}")
        else:
            await ctx.err("لینکی ثبت نشده است. مالک می‌تواند با دستور "
                          "«ثبت لینک [لینک]» آن را ثبت کند.")
        return True

    m = re.match(r"^ثبت لینک (.+)$", text)
    if m:
        if ctx.rank < ranks.OWNER:
            return await _deny(ctx, "مالک گروه")
        g["link"] = m.group(1).strip()
        db.save("groups")
        await ctx.ok("لینک گروه ثبت شد.")
        return True

    if text == "حذف لینک":
        if ctx.rank < ranks.OWNER:
            return await _deny(ctx, "مالک گروه")
        g["link"] = ""
        db.save("groups")
        await ctx.ok("لینک ثبت‌شده حذف شد.")
        return True

    if text in ("لینک جدید", "ساخت لینک", "لینک یکبار مصرف", "لینک درخواستی"):
        if ctx.rank < ranks.OWNER:
            return await _deny(ctx, "مالک گروه")
        res = await ctx.api.new_link(ctx.guid)
        link = api_mod._val(res, "join_link", "link", default=None)
        await ctx.reply(f"🔗 لینک جدید:\n{link}" if link
                        else "❌ ساخت لینک ممکن نشد.")
        return True

    # ------------------------------------------------ درخواست عضویت
    if text == "تعداد درخواست ها":
        if ctx.rank < ranks.SPECIAL:
            return await _deny(ctx, "ادمین ویژه و مالک")
        res = await ctx.api.join_requests(ctx.guid)
        items = api_mod.members_of(res)
        await ctx.reply(f"📥 تعداد درخواست‌های عضویت: {utils.fa_digits(len(items))}")
        return True

    m = re.match(r"^(قبول کردن|رد کردن) درخواست(?: ها)?(?: (\d+))?$", text)
    if m:
        if ctx.rank < ranks.SPECIAL:
            return await _deny(ctx, "ادمین ویژه و مالک")
        action = "Accept" if m.group(1) == "قبول کردن" else "Decline"
        limit = int(m.group(2)) if m.group(2) else 10 ** 6
        res = await ctx.api.join_requests(ctx.guid)
        items = api_mod.members_of(res)[:limit]
        for member in items:
            guid = api_mod.guid_of(member)
            if guid:
                await ctx.api.answer_join(ctx.guid, guid, action)
        word = "قبول" if action == "Accept" else "رد"
        await ctx.ok(f"{utils.fa_digits(len(items))} درخواست {word} شد.")
        return True

    # ------------------------------------------------ تگ و منشن
    m = re.match(r"^(تگ|منشن)(?: (.+))?$", text)
    if m:
        if ctx.rank < ranks.SPECIAL:
            return await _deny(ctx, "ادمین ویژه و مالک")
        scope = (m.group(2) or "").strip()
        # ریپلای روی یک کاربر = تگ/منشن مستقیم همان کاربر. اگر scope نوشته شده
        # باشد رفتار قبلی (مثلاً «تگ ادمین ها») حفظ می‌شود.
        if not scope and ctx.reply_id:
            target = await ctx.target()
            if target:
                await _tag(ctx, m.group(1), "__reply_target__", [target])
                return True
        await _tag(ctx, m.group(1), scope)
        return True

    # ------------------------------------------------ تنظیم نام/بیو گروه
    m = re.match(r"^(?:تنظیم نام گروه|تنظیم اسم گروه|تنظیم اسم پروف گروه|تنظیم نام پروف گروه) (.+)$", text, re.S)
    if m:
        if ctx.rank < ranks.SPECIAL:
            return await _deny(ctx, "ادمین ویژه و مالک")
        new_title = m.group(1).strip()[:64]
        res = await ctx.api.edit_info(ctx.guid, title=new_title)
        if res is None:
            await ctx.err("تغییر نام گروه ناموفق بود (ربات باید دسترسیِ "
                          "«تغییر اطلاعات» داشته باشد).")
        else:
            await ctx.ok(f"نام گروه به «{new_title}» تغییر کرد.")
        return True

    m = re.match(r"^(?:تنظیم بیو گروه|تنظیم توضیحات گروه)(?: (.+))?$", text, re.S)
    if m:
        if ctx.rank < ranks.SPECIAL:
            return await _deny(ctx, "ادمین ویژه و مالک")
        new_bio = (m.group(1) or "").strip()[:512]
        res = await ctx.api.edit_info(ctx.guid, description=new_bio)
        if res is None:
            await ctx.err("تغییر بیو گروه ناموفق بود (ربات باید دسترسیِ "
                          "«تغییر اطلاعات» داشته باشد).")
        else:
            await ctx.ok("بیو گروه تغییر کرد." if new_bio else "بیو گروه حذف شد.")
        return True

    # ------------------------------------------------ آپدیت گروه
    if text == "اپدیت گروه" or text == "آپدیت گروه":
        if ctx.rank < ranks.ADMIN:
            return await _deny(ctx)
        info = await ctx.api.group_info(ctx.guid)
        title = api_mod.deep(info, "group", "group_title") or \
            api_mod.deep(info, "chat", "title") or "نامشخص"
        count = api_mod.deep(info, "group", "count_members") or 0
        await ctx.reply(f"🏷 نام گروه: {title}\n"
                        f"👥 تعداد اعضا: {utils.fa_digits(count)}")
        return True

    return False


# ------------------------------------------------------------ تگ
async def _tag(ctx, mode, scope, forced_guids=None):
    g = ctx.g
    guids = []
    label = "اعضا"

    scope_map = {"ادمین ها": "admins", "ادمینها": "admins", "ویژه ها": "specials",
                 "ویژهها": "specials", "معاف": "exempt", "بلاک": "block",
                 "کیل": "kill", "سکوت": "mute"}

    if forced_guids is not None:
        guids = list(forced_guids)
        label = "کاربر ریپلای‌شده"
    elif scope in scope_map:
        key = scope_map[scope]
        guids = list(g[key].keys()) if key == "mute" else list(g[key])
        label = scope
    elif scope.isdigit():
        count = min(int(scope), 200)
        res = await ctx.api.members(ctx.guid)
        guids = [api_mod.guid_of(x) for x in api_mod.members_of(res)][:count]
    else:
        res = await ctx.api.members(ctx.guid)
        guids = [api_mod.guid_of(x) for x in api_mod.members_of(res)][:50]

    guids = [x for x in guids if x and x != ctx.me]
    if not guids:
        await ctx.err("موردی برای تگ کردن پیدا نشد.")
        return

    chunk_size = 20
    for i in range(0, len(guids), chunk_size):
        part = guids[i:i + chunk_size]
        lines = []
        for guid in part:
            name = await ctx.target_link(guid)
            if mode == "تگ":
                lines.append(name)
            else:
                lines.append(f"• {name} — {random.choice(MENTION_LINES)}")
        head = f"📣 {'تگ' if mode == 'تگ' else 'منشن'} {label} " \
               f"({utils.fa_digits(len(guids))} نفر)\n\n"
        await ctx.api.send(ctx.guid, head + "\n".join(lines))


# ------------------------------------------------------------ حذف گروهی
async def _bulk_delete(ctx, count, only_user=None, kind=None):
    """آخرین پیام‌های گروه را می‌خواند و بر اساس شرط حذف می‌کند."""
    collected, removed = [], 0

    # SplusLib message dictionaries do not always expose ``is_edited`` after
    # pagination. We keep an authoritative local history flag for edits.
    if kind == "edited":
        try:
            local_history = db.data.get("history", {}).get(str(ctx.guid), [])
            for item in reversed(local_history):
                mid = api_mod._val(item, "m", "id", "message_id", default=None)
                author = api_mod._val(item, "u", "sender_id", "author_id", default=None)
                if not mid or api_mod._val(item, "e", "is_edited", default=False) is not True:
                    continue
                if str(mid) == str(ctx.message_id):
                    continue
                if only_user and str(author) != str(only_user):
                    continue
                collected.append(str(mid))
                if len(collected) >= count:
                    break
        except Exception:
            pass

    middle = ctx.message_id
    guard = 0
    while len(collected) < count and guard < 20:
        guard += 1
        res = await ctx.api.get_last_messages(ctx.guid, middle)
        messages = api_mod._val(res, "messages", default=[]) or []
        if not messages:
            break
        for msg in messages:
            mid = api_mod._val(msg, "id", "message_id")
            if not mid or str(mid) == str(ctx.message_id):
                continue
            author = api_mod.sender_guid_of(msg)
            if only_user and author != only_user:
                continue
            if kind == "edited" and not api_mod._val(msg, "is_edited", "edit_date", default=False):
                continue
            if kind == "glass":
                blob = str(msg).lower()
                if "keypad" not in blob and "glass" not in blob:
                    continue
            collected.append(str(mid))
            if len(collected) >= count:
                break
        # SplusLib returns newest -> oldest; the final id is therefore the
        # correct cursor for the next page.
        middle = api_mod._val(messages[-1], "id", "message_id")

    for i in range(0, len(collected), 50):
        await ctx.api.delete(ctx.guid, collected[i:i + 50])
        removed += len(collected[i:i + 50])
    await ctx.api.delete(ctx.guid, [ctx.message_id])
    return removed


async def _deny(ctx, who="ادمین‌ها، ادمین‌های ویژه و مالک"):
    await ctx.err(f"این دستور فقط برای {who} قابل اجراست.")
    return True
