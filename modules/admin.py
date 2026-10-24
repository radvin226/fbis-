# -*- coding: utf-8 -*-
"""فعال‌سازی، مقام‌ها، مالک، ادمین‌ها، تنظیمات و بکاپ FBI."""
from __future__ import annotations

import re
import time

import config
from core import api as api_mod
from core import msgstyle, ranks, utils
from core.db import db

ACTIVATE_WORDS = ("فعال", "فعالسازی", "روشن شو", "activate")
DEACTIVATE_WORDS = ("غیرفعال", "خاموش شو", "deactivate")


async def fetch_admins(ctx):
    try:
        snap = await ctx.api._role_snapshot(ctx.guid, fresh=True)
    except Exception:
        snap = {"owner": None, "admins": [], "members": []}
    owner = str(snap.get("owner")) if snap.get("owner_verified") and snap.get("owner") is not None else None
    if owner is not None:
        ctx.g["live_owner"] = owner
        ctx.g["live_owner_verified"] = True
    elif snap.get("role_scan_ok"):
        ctx.g["live_owner"] = None
        ctx.g["live_owner_verified"] = False
    admins = list(dict.fromkeys(str(x) for x in (snap.get("admins") or []) if x is not None))
    members = list(snap.get("members") or [])
    # IMPORTANT: never infer ownership from the message sender here.
    # The chat-wide snapshot is the only source allowed to set owner.
    for member in members:
        uid = api_mod.guid_of(member)
        if uid is None:
            continue
        user = db.user(str(uid))
        name = api_mod.name_of(member)
        username = api_mod._val(member, "username", "user_name", default="") or ""
        if name != "کاربر":
            user["name"] = name
        if username:
            user["username"] = str(username)
    db.save("users")
    return owner, admins, members


async def _ensure_creators_present(ctx, members):
    """سازندهٔ تعریف‌شده (CREATOR_IDS) اگر عضو گروه نیست، به‌طور خودکار اضافه می‌شود."""
    present = {str(api_mod.guid_of(m)) for m in (members or []) if api_mod.guid_of(m) is not None}
    added = []
    for cid in getattr(config, "CREATOR_IDS", ()):
        if str(cid) in present:
            continue
        try:
            if await ctx.api.add_member(ctx.guid, str(cid)):
                added.append(str(cid))
        except Exception:
            pass
    if added:
        db.log("creator", f"added creators chat={ctx.guid}: {', '.join(added)}")
    return added


async def activate(ctx, *, explicit=False, silent=False) -> bool:
    # silent=True: بدون کارت گزارش؛ برای فعال‌سازی سراسری که از خود اطلاع‌رسانی انجام می‌شود.
    g = ctx.g
    # Manual FBI owner must survive restart/reactivation. Older unknown owner
    # records are still cleared; live creator ownership is refreshed otherwise.
    keep_manual_owner = bool(
        g.get("owner_verified")
        and g.get("owner_source") == "manual_owner"
        and g.get("owner")
    )
    if not keep_manual_owner:
        g["owner"] = None
        g["owner_verified"] = False
        g["owner_source"] = ""
    # Cache the live group title for later owner-panel previews. Failure is non-fatal.
    try:
        info = await ctx.api.group_info(ctx.guid)
        title = api_mod.deep(info, "group", "group_title", default=None) or api_mod.deep(info, "chat", "title", default=None)
        if title:
            g["title"] = str(title)
    except Exception:
        pass
    if not g.get("subordinate") and not g.get("subscription_active", True):
        if explicit:
            await ctx.err("اشتراک این گروه فعال نیست. برای تمدید، سازنده FBI باید اشتراک گروه را تمدید کند.")
        return False
    try:
        bot_is_admin = await ctx.api.is_admin(ctx.guid, ctx.me, fresh=True)
    except Exception as exc:
        bot_is_admin = False
        if explicit:
            await ctx.err(f"بررسی ادمین بودن FBI خطا داد: {type(exc).__name__}: {exc}")
    g["_bot_admin"] = bool(bot_is_admin)
    if not bot_is_admin:
        if explicit:
            await ctx.err("FBI در این گروه ادمین نیست. ادمین کامل و دسترسی‌های لازم را بدهید و دوباره «فعال» را بفرستید.")
        return False

    owner, admins, members = await fetch_admins(ctx)
    await _ensure_creators_present(ctx, members)
    if explicit and ctx.author:
        aid = str(ctx.author)
        # Activation permission can come from a verified owner/admin, but this
        # check must never write that sender into the owner field.
        if owner is not None and aid == str(owner):
            pass
        elif aid in {str(x) for x in admins}:
            pass
        elif aid in {str(x) for x in config.SUDOS}:
            pass
        else:
            # If role enumeration succeeded and the sender is not an admin,
            # reject activation. When enumeration is unavailable, do not invent
            # ownership; allow only SUDO to manage activation.
            # fetch_admins() returns real admin IDs only from the role snapshot.
            # A non-empty verified admin/owner result means role enumeration succeeded.
            snap_ok = bool(owner is not None or admins)
            if snap_ok:
                await ctx.err("دستور «فعال» باید توسط مالک یا ادمین واقعی گروه اجرا شود.")
                return False
            if aid not in {str(x) for x in config.SUDOS}:
                await ctx.err("مالک/ادمین واقعی گروه از API قابل شناسایی نشد؛ فعال‌کننده به‌عنوان مالک ثبت نمی‌شود.")
                return False

    was_active = bool(g.get("active"))
    g["active"] = True
    g["manual_disabled"] = False
    g["_bot_admin"] = True
    g["opened_at"] = g.get("opened_at") or time.time()
    # Keep a manually configured FBI owner; otherwise refresh from the live creator.
    # Always keep the server's real owner separately. If a manual FBI owner
    # exists, both people remain OWNER-level in the bot.
    g["live_owner"] = str(owner) if owner is not None else None
    g["live_owner_verified"] = bool(owner is not None)
    if not keep_manual_owner:
        g["owner"] = str(owner) if owner is not None else None
        g["owner_verified"] = bool(owner is not None)
        g["owner_source"] = "live_creator" if owner is not None else ""
    g["activated_by"] = str(ctx.author) if ctx.author else ""
    owner_for_admin_filter = g.get("owner")
    g["admins"] = [x for x in dict.fromkeys(str(a) for a in admins) if str(x) != str(owner_for_admin_filter)]
    db.save("groups")
    if was_active and not explicit:
        return True
    if silent:
        return True

    scanned_messages = 0
    try:
        rows = await ctx.api.call("get_messages", ctx.api._chat_arg(ctx.guid), 100)
        scanned_messages = len(rows or [])
    except Exception:
        scanned_messages = 0

    owner_link = await ctx.target_link(owner) if owner else "شناسایی نشد"
    admin_ids = [x for x in admins if str(x) != str(owner)]
    admin_lines = []
    for uid in admin_ids:
        try:
            admin_lines.append(f"• {await ctx.target_link(uid)}")
        except Exception:
            admin_lines.append(f"• `{uid}`")

    lines = [
        "🕵️ FBI — فعال‌سازی موفق ✅",
        "━━━━━━━━━━━━━━━━━━━━",
        f"👑 مالک واقعی گروه: {owner_link}",
        f"🛡 ادمین‌های شناسایی‌شده: {utils.fa_digits(len(admin_ids))} نفر",
    ]
    lines += admin_lines or ["• ادمین دیگری شناسایی نشد."]
    lines += [
        "",
        f"👥 اعضای اسکن‌شده: {utils.fa_digits(len(members))}",
        f"💬 پیام‌های اسکن‌شده: {utils.fa_digits(scanned_messages)}",
        "",
        f"🏅 مقام FBI: {ranks.name(ranks.BOT)}",
        "🛡 کاربران معاف: مالک، ادمین‌ها، ویژه‌ها، SUDO و خود FBI",
        "",
        f"👤 سازنده: {config.MAKER}",
        f"🆔 شناسه سازندگان: {' ، '.join(f'`{x}`' for x in config.SUDOS)}",
        f"🤖 FBI BOT ID: `{ctx.me}`",
        "",
        "📖 برای دیدن تمام دستورات «راهنما» را بفرستید.",
    ]
    if msgstyle.is_card(ctx.g):
        text = msgstyle.card("FBI — فعال‌سازی موفق ✅", "\n".join(lines[2:]), "🕵️")
    else:
        text = "\n".join(lines)
    await ctx.reply(text, reply=False)
    return True


def _is_rank_query(text):
    """«مقام», «مقامش», «مقام @user» or «مقامش 123» (but not «مقامم»)."""
    import re as _re
    from core import utils as _u
    t = _u.normalize(text)
    return bool(_re.match(r"^مقام(?:ش)?(?: |$)", t)) and t not in ("مقامم", "مقام من")


async def handle(ctx) -> bool:
    text = ctx.text
    g = ctx.g

    if text in ACTIVATE_WORDS:
        if g.get("active"):
            await ctx.reply("🤖 من فعالم ✅")
            return True
        return await activate(ctx, explicit=True)

    if text in DEACTIVATE_WORDS:
        if ctx.rank < ranks.OWNER:
            return await _deny(ctx, "مالک گروه")
        g["active"] = False
        g["manual_disabled"] = True
        db.save("groups")
        await ctx.ok("ربات در این گروه غیرفعال شد.")
        return True

    if text == "عدم دسترسی":
        owner, admins, _ = await fetch_admins(ctx)
        if owner and str(owner) == str(ctx.author):
            if not (g.get("owner_verified") and g.get("owner_source") == "manual_owner" and g.get("owner")):
                g["owner"] = str(owner)
                g["owner_verified"] = True
                g["owner_source"] = "live_creator"
            g["admins"] = [x for x in admins if str(x) != str(g.get("owner"))]
            db.save("groups")
            await ctx.ok("مالک واقعی گروه شناسایی شد و به‌عنوان مالک FBI ثبت شد. 🌟")
        else:
            await ctx.err("شما مالک واقعی گروه نیستید یا مالک از API قابل شناسایی نیست.")
        return True

    if text in ("اپدیت لیست ادمین", "آپدیت لیست ادمین"):
        if ctx.rank < ranks.SPECIAL:
            return await _deny(ctx)
        owner, admins, _ = await fetch_admins(ctx)
        manual_owner = bool(g.get("owner_verified") and g.get("owner_source") == "manual_owner" and g.get("owner"))
        g["admins"] = [x for x in admins if str(x) != str(g.get("owner") if manual_owner else owner)]
        if owner and not manual_owner:
            g["owner"] = str(owner)
            g["owner_verified"] = True
            g["owner_source"] = "live_creator"
        db.save("groups")
        await ctx.ok(f"{utils.fa_digits(len(g['admins']))} ادمین واقعی ثبت شد.")
        return True

    if (utils.command_hit(text, "تنظیم مالک") or utils.command_hit(text, "انتقال مالکیت")
            or (ctx.reply_id and text in ("تنظیم مالک", "انتقال مالکیت"))):
        if ctx.rank < ranks.OWNER and ctx.rank != ranks.CREATOR:
            return await _deny(ctx, "مالک گروه")
        target = await ctx.target()
        if not target:
            return await _need_target(ctx)

        # «تنظیم مالک» یک مقام داخلی FBI است و باید باقی بماند؛ نباید اسکن
        # بعدی مالک را دوباره به «ادمین» برگرداند. مالک واقعی SPLUS همچنان
        # از API به‌صورت جداگانه قابل تشخیص است.
        g["owner"] = str(target)
        g["owner_verified"] = True
        g["owner_source"] = "manual_owner"
        g["admins"] = [x for x in g.get("admins", []) if str(x) != str(target)]
        g["specials"] = [x for x in g.get("specials", []) if str(x) != str(target)]
        owner_granted = False
        try:
            await ctx.api.set_admin(ctx.guid, target, access="full")
            owner_granted = True
        except Exception:
            owner_granted = False
        db.save("groups")
        await ctx.ok(f"{await ctx.target_link(target)} به مقام «مالک» FBI تنظیم شد. 🌟\n🎖 مقام فعلی: مالک\n🛡 دسترسی: فول" + ("" if owner_granted else "\n⚠️ ارتقای دسترسی سروری تأیید نشد."), mention=target)
        return True

    if await _promote(ctx):
        return True

    if text in ("مقامم", "مقام من"):
        await ctx.reply(f"🎖 مقام شما: {ranks.name(ctx.rank)}")
        return True
    if _is_rank_query(text):
        target = await ctx.target()
        if not target:
            return await _need_target(ctx)
        level = ranks.rank(ctx.guid, target)
        if level < ranks.SPECIAL:
            try:
                role = await ctx.api.user_role(ctx.guid, target, fresh=True)
                if role.get("is_owner"):
                    level = ranks.OWNER
                elif role.get("is_admin"):
                    level = ranks.ADMIN
            except Exception:
                pass
        await ctx.reply(f"🎖 مقام {await ctx.target_link(target)}: {ranks.name(level)}")
        return True

    if text in ("داشبورد", "تنظیمات"):
        if ctx.rank < ranks.ADMIN:
            return await _deny(ctx)
        await ctx.reply(_dashboard(g))
        return True

    m = re.match(r"^تنظیم حداکثر اخطار (\d+)$", text)
    if m:
        if ctx.rank < ranks.OWNER:
            return await _deny(ctx, "مالک گروه")
        g["max_warn"] = max(1, min(100, int(m.group(1))))
        db.save("groups")
        await ctx.ok(f"حداکثر اخطار روی {utils.fa_digits(g['max_warn'])} تنظیم شد.")
        return True

    switches = {"حالت خواب":"sleep", "سرگرمی":"fun", "حالت مدیریت":"manage_mode", "خوشامدگویی":"welcome", "خداحافظی":"goodbye", "سخنگو":"speaker", "پرحرفی":"talkative", "یادگیری":"learning", "حذف خودکار":"auto_delete"}
    for fa, key in switches.items():
        m = re.match(rf"^{fa} (روشن|خاموش)$", text)
        if m:
            if ctx.rank < ranks.OWNER:
                return await _deny(ctx, "مالک گروه")
            g[key] = m.group(1) == "روشن"
            db.save("groups")
            await ctx.ok(f"«{fa}» {'فعال' if g[key] else 'غیرفعال'} شد.")
            return True

    m = re.match(r"^تنظیم زمان حذف خودکار (.+)$", text)
    if m:
        if ctx.rank < ranks.OWNER:
            return await _deny(ctx, "مالک گروه")
        g["auto_delete_time"] = utils.parse_duration(m.group(1)) or 60
        db.save("groups")
        await ctx.ok(f"زمان حذف خودکار روی {utils.human_duration(g['auto_delete_time'])} تنظیم شد.")
        return True

    m = re.match(r"^دستور (.+?) (روشن|خاموش|فعال|غیرفعال)$", text)
    if m:
        if ctx.rank < ranks.OWNER:
            return await _deny(ctx, "مالک گروه")
        cmd = m.group(1).strip()
        disabled = m.group(2) in ("خاموش", "غیرفعال")
        lst = g.setdefault("disabled_cmds", [])
        if disabled and cmd not in lst:
            lst.append(cmd)
        elif not disabled and cmd in lst:
            lst.remove(cmd)
        db.save("groups")
        await ctx.ok(f"دستور «{cmd}» برای کاربران عادی {'غیرفعال' if disabled else 'فعال'} شد.")
        return True

    if text == "محدودیت":
        if ctx.rank < ranks.SPECIAL:
            return await _deny(ctx)
        body = "\n".join(f"• {x}" for x in g.get("disabled_cmds", [])) or "هیچ دستوری غیرفعال نیست."
        await ctx.reply("🚫 محدودیت دستورات:\n" + body)
        return True

    resets = {"ریست آمار":"stats", "ریست لیست قفل":"locks", "ریست دستورات":"cmds", "ریست داشبورد":"dashboard", "ریست کامل":"all"}
    if text in resets:
        if ctx.rank < ranks.OWNER:
            return await _deny(ctx, "مالک گروه")
        db.reset_group(ctx.guid, resets[text])
        await ctx.ok(f"«{text}» انجام شد.")
        return True

    if text == "ذخیره اطلاعات":
        if ctx.rank < ranks.OWNER:
            return await _deny(ctx, "مالک گروه")
        code = db.backup_make(ctx.guid)
        await ctx.reply(f"💾 بکاپ ساخته شد.\n\nکد بازیابی: `{code}`\n\nبازیابی {code}")
        return True

    m = re.match(r"^بازیابی (\S+)$", text)
    if m:
        if ctx.rank < ranks.OWNER:
            return await _deny(ctx, "مالک گروه")
        if db.backup_restore(ctx.guid, m.group(1)):
            await ctx.ok("تنظیمات بازیابی شد. ✅")
        else:
            await ctx.err("کد بازیابی نامعتبر است.")
        return True
    return False


FULL_ADMIN_ACCESS = {
    "profile": "full",
    "change_info": True, "post_messages": True, "edit_messages": True,
    "delete_messages": True, "ban_users": True, "invite_users": True,
    "pin_messages": True, "add_admins": True, "manage_call": True,
    "manage_topics": True, "post_stories": True, "edit_stories": True,
    "delete_stories": True, "anonymous": False,
}
ADMIN_DEFAULT_ACCESS = {
    "profile": "default",
    "change_info": True, "delete_messages": True, "ban_users": True,
    "invite_users": True, "pin_messages": True, "manage_call": True,
    "anonymous": False,
}
SPECIAL_FULL_ACCESS = dict(FULL_ADMIN_ACCESS)
OWNER_FULL_ACCESS = dict(FULL_ADMIN_ACCESS)


async def _grant_real_admin(ctx, target, access=None):
    try:
        # "default" intentionally means SPLUS's own default admin profile:
        # do not send custom rights; the native set_admin(chat,user) call owns
        # the exact default profile. "full" uses the explicit full profile.
        await ctx.api.set_admin(ctx.guid, target, access=(access or "full"))
        return True
    except Exception:
        return False


async def _revoke_real_admin(ctx, target):
    try:
        await ctx.api.unset_admin(ctx.guid, target)
        return True
    except Exception:
        return False


async def sync_real_admins(ctx):
    g = ctx.g
    try:
        snap = await ctx.api._role_snapshot(ctx.guid, fresh=True)
        owner = snap.get("owner") if snap.get("owner_verified") else None
        real_admins = list(snap.get("admins") or [])
        scan_ok = bool(snap.get("role_scan_ok"))
    except Exception:
        owner = None
        real_admins = []
        scan_ok = False
    real = {str(x) for x in real_admins if x is not None}
    bot_is_admin = str(ctx.me) in real
    changed = g.get("_bot_admin") != bot_is_admin
    g["_bot_admin"] = bot_is_admin
    try:
        info = await ctx.api.group_info(ctx.guid)
        title = api_mod.deep(info, "group", "group_title", default=None) or api_mod.deep(info, "chat", "title", default=None)
        if title and g.get("title") != str(title):
            g["title"] = str(title)
            changed = True
    except Exception:
        pass

    # A manual FBI owner is an internal role and must survive every live admin
    # sync.  The live SPLUS creator is tracked separately; otherwise a routine
    # sync would silently replace a manually assigned FBI owner with the real
    # chat owner (or with None), making «تنظیم مالک» appear to have no effect.
    manual_owner = bool(
        g.get("owner_verified")
        and g.get("owner_source") == "manual_owner"
        and g.get("owner") is not None
    )

    # Never erase a previously verified owner because of a transient RPC failure.
    # When the role scan itself succeeded, replace the local owner only when the
    # owner is NOT a manually configured FBI owner.
    if scan_ok and not manual_owner:
        normalized_owner = str(owner) if owner is not None else None
        if (g.get("owner") != normalized_owner
                or bool(g.get("owner_verified")) != bool(normalized_owner)
                or g.get("owner_source") != ("live_creator" if normalized_owner else "")):
            g["owner"] = normalized_owner
            g["owner_verified"] = bool(normalized_owner)
            g["owner_source"] = "live_creator" if normalized_owner else ""
            changed = True

    known_owner = str(g.get("owner")) if g.get("owner") is not None else None
    live_owner = str(owner) if owner is not None else None
    if scan_ok:
        if g.get("live_owner") != live_owner or bool(g.get("live_owner_verified")) != bool(live_owner):
            g["live_owner"] = live_owner
            g["live_owner_verified"] = bool(live_owner)
            changed = True
        old = list(g.get("admins", []))
        special_ids = {str(x) for x in g.get("specials", [])}
        owner_ids = {x for x in (known_owner, live_owner) if x is not None}
        new_admins = [u for u in old if str(u) in real and str(u) not in owner_ids and str(u) not in special_ids]
        for uid in real:
            if uid in owner_ids or uid in special_ids:
                continue
            if uid not in {str(x) for x in new_admins}:
                new_admins.append(uid)
        sudo_ids = {str(x) for x in config.SUDOS}
        for sid in sudo_ids:
            if sid != known_owner and sid not in special_ids and sid not in {str(x) for x in new_admins}:
                new_admins.append(sid)
        if old != new_admins:
            g["admins"] = new_admins
            changed = True
    if changed:
        db.save("groups")
    # Report the effective FBI owner. For «تنظیم مالک», this is the manual
    # owner even when SPLUS has a different real creator.
    effective_owner = known_owner if manual_owner else (str(owner) if owner is not None else None)
    return effective_owner, real_admins


async def _promote(ctx) -> bool:
    text = ctx.text
    g = ctx.g
    specs = {
        "تنظیم ویژه": ("specials", "ادمین ویژه", ranks.OWNER),
        "تنظیم ادمین": ("admins", "ادمین", ranks.SPECIAL),
    }
    for fa, (key, title, need) in specs.items():
        if utils.command_hit(text, fa) or (ctx.reply_id and text == fa):
            if ctx.rank < need:
                return await _deny(ctx, "مالک" if need == ranks.OWNER else "مالک و ادمین‌های ویژه")
            target = await ctx.target()
            if not target:
                return await _need_target(ctx)
            # «تنظیم ویژه» باید مقام داخلی FBI را واقعاً به «ادمین ویژه»
            # تغییر دهد، حتی اگر کاربر در SPLUS از قبل ادمین واقعی باشد.
            # فقط مالک/سازنده، خود FBI و کاربران معاف همچنان محافظت می‌شوند.
            if key == "specials":
                if str(target) == str(getattr(ctx, "me", "")):
                    await ctx.err("❌ این دستور روی خودِ FBI قابل اجرا نیست.")
                    return True
                if str(target) in {str(x) for x in ctx.g.get("exempt", [])}:
                    await ctx.err(f"{await ctx.target_link(target)} در لیست معاف است و مقامش تغییر نمی‌کند.")
                    return True
                if str(target) in {str(x) for x in config.SUDOS}:
                    await ctx.err(f"{await ctx.target_link(target)} مقام «سازنده» دارد؛ مقامش تغییر نمی‌کند.")
                    return True
                try:
                    live_role = await ctx.api.user_role(ctx.guid, target, fresh=True)
                except Exception:
                    live_role = {}
                local_level = ranks.rank(ctx.guid, target)
                if live_role.get("is_owner") or local_level in (ranks.OWNER, ranks.CREATOR, ranks.BOT):
                    await ctx.err(f"{await ctx.target_link(target)} مقام «{ranks.name(max(local_level, ranks.OWNER))}» دارد؛ مقامش تغییر نمی‌کند.")
                    return True
            else:
                protection = await ctx.protection_reason(target)
                if protection:
                    await ctx.err(protection)
                    return True
            other = "admins" if key == "specials" else "specials"
            g[other] = [x for x in g.get(other, []) if str(x) != str(target)]
            if str(target) not in {str(x) for x in g.get(key, [])}:
                g.setdefault(key, []).append(str(target))
            db.save("groups")
            granted = await _grant_real_admin(ctx, target, access="full" if key == "specials" else "default")
            note = "" if granted else "\n⚠️ ارتقای واقعی در سرور انجام نشد؛ FBI باید دسترسی SetAdmin داشته باشد."
            extra = "\n🛡 در برابر قفل‌ها، بن و سکوت معاف است." if key == "specials" else ""
            await ctx.ok(f"{await ctx.target_link(target)} به مقام «{title}» ارتقا یافت.{extra}{note}", mention=target)
            return True

    if utils.command_hit(text, "برکناری") or (ctx.reply_id and text == "برکناری"):
        if ctx.rank < ranks.SPECIAL:
            return await _deny(ctx, "مالک و ادمین‌های ویژه")
        target = await ctx.target()
        if not target:
            return await _need_target(ctx)
        level = ranks.rank(ctx.guid, target)
        if level >= ctx.rank and ctx.rank != ranks.CREATOR:
            await ctx.err("شما اجازه‌ی برکناری این مقام را ندارید.")
            return True
        for key in ("admins", "specials"):
            g[key] = [x for x in g.get(key, []) if str(x) != str(target)]
        db.save("groups")
        revoked = await _revoke_real_admin(ctx, target)
        note = "" if revoked else "\n⚠️ ادمینی واقعی حذف نشد؛ ممکن است دسترسی SetAdmin لازم باشد."
        await ctx.ok(f"{await ctx.target_link(target)} از مقام ربات برکنار شد.{note}", mention=target)
        return True

    for text_cmd, key, title in (("پاکسازی لیست ویژه", "specials", "ادمین ویژه"), ("پاکسازی لیست ادمین", "admins", "ادمین"), ("ریست لیست ادمین", "admins", "ادمین"), ("ریست لیست ویژه", "specials", "ادمین ویژه")):
        if text == text_cmd:
            if ctx.rank < ranks.OWNER:
                return await _deny(ctx, "مالک گروه")
            g[key] = []
            db.save("groups")
            await ctx.ok(f"تمام {title}‌ها حذف شدند.")
            return True

    for fa, key, title in (("لیست ویژه", "specials", "ادمین‌های ویژه"), ("لیست ادمین", "admins", "ادمین‌های گروه"), ("لیست ادمین ها", "admins", "ادمین‌های گروه")):
        m = re.match(rf"^{fa}(?: (\d+))?$", text)
        if m:
            if ctx.rank < ranks.ADMIN:
                return await _deny(ctx)
            items = list(g.get(key, []))
            chunk, page, total = utils.paginate(items, int(m.group(1) or 1)) if items else ([], 1, 1)
            lines = [f"📋 {title} — صفحه {utils.fa_digits(page)}/{utils.fa_digits(total)}", ""]
            if g.get("owner_verified") and g.get("owner"):
                lines.append(f"🌟 مالک: {await ctx.target_link(g['owner'])}")
            if items:
                for i, uid in enumerate(chunk, 1):
                    lines.append(f"{utils.fa_digits(i)}. {await ctx.target_link(uid)}")
            else:
                lines.append("• لیست خالی است.")
            await ctx.reply("\n".join(lines))
            return True
    return False


def _dashboard(g) -> str:
    def st(k):
        return "🟢 روشن" if g.get(k) else "🔴 خاموش"
    return "\n".join([
        "⚙️ داشبورد FBI", "",
        f"• خواب: {st('sleep')}", f"• سرگرمی: {st('fun')}", f"• مدیریت: {st('manage_mode')}",
        f"• خوشامد: {st('welcome')}", f"• خداحافظی: {st('goodbye')}", f"• سخنگو: {st('speaker')}",
        f"• یادگیری: {st('learning')}", f"• حذف خودکار: {st('auto_delete')}", f"• گروه بسته: {st('closed')}",
        f"• سقف اخطار: {utils.fa_digits(g.get('max_warn', 5))}",
    ])


async def _deny(ctx, who="ادمین‌ها، ادمین‌های ویژه و مالک"):
    await ctx.err(f"این دستور فقط برای {who} قابل اجراست.")
    return True


async def _need_target(ctx):
    await ctx.err("روی پیام کاربر ریپلای بزنید یا شناسه‌ی او را بنویسید.")
    return True
