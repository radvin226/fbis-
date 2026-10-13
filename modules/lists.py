# -*- coding: utf-8 -*-
"""لیست‌های کاربری: معاف، بلاک، سکوت، کیل، بن، مجاز، فیلتر و اخطار."""

import asyncio
import re
import time

from core import ranks, utils
from core.db import db

# نام فارسی -> (کلید, عنوان, نیاز به مقام)
USER_LISTS = {
    "معاف": ("exempt", "معاف", ranks.ADMIN),
    "بلاک": ("block", "بلاک", ranks.ADMIN),
    "کیل": ("kill", "کیل", ranks.ADMIN),
    "سکوت": ("mute", "سکوت", ranks.ADMIN),
}

WORD_LISTS = {
    "مجاز": ("allowed", "مجاز", ranks.ADMIN),
    "فیلتر": ("filters", "فیلتر", ranks.ADMIN),
}


# ------------------------------------------------------------ کمکی
def _muted(g, guid) -> bool:
    item = g.get("mute", {}).get(guid)
    if item is None:
        return False
    if item in (0, True):          # سکوت دائم
        return True
    if time.time() > float(item):  # منقضی شده
        # The entry stays in ``mute`` on purpose: cleanup_expired_mutes must still
        # see it to release the server restriction. Dropping it here would leave
        # the member restricted on the server for good.
        g.setdefault("mute_expired_cleanup", {})[guid] = float(g.get("mute_started", {}).get(guid) or 0)
        return False
    return True


def is_blocked(g, guid) -> bool:
    return guid in g.get("block", [])


def is_killed(g, guid) -> bool:
    return guid in g.get("kill", [])


def is_muted(g, guid) -> bool:
    return _muted(g, guid)

async def _scan_server_messages(ctx, target, since, limit=3000):
    """Best-effort history scan using RubPy message interval API."""
    ids = []
    seen = set()
    middle = ctx.message_id
    for _ in range(40):
        res = await ctx.api.get_last_messages(ctx.guid, middle)
        messages = getattr(res, "messages", None) if res is not None else None
        if messages is None and isinstance(res, dict):
            messages = res.get("messages", [])
        messages = messages or []
        if not messages:
            break
        for msg in messages:
            def val(*keys):
                for k in keys:
                    if isinstance(msg, dict) and k in msg: return msg[k]
                    if hasattr(msg, k):
                        try: return getattr(msg, k)
                        except Exception: pass
                return None
            mid = val("id", "message_id")
            author = val("from_id", "sender_id", "author_object_guid", "author_guid", "sender_guid")
            if mid and author == target and str(mid) not in seen:
                seen.add(str(mid)); ids.append(str(mid))
                if len(ids) >= limit: return ids
        last = None
        last_msg = messages[-1]
        if isinstance(last_msg, dict): last = last_msg.get("id", last_msg.get("message_id"))
        else: last = getattr(last_msg, "id", getattr(last_msg, "message_id", None))
        if not last or str(last) == str(middle): break
        middle = last
    return ids


# ------------------------------------------------------------ دستورات


# نمایش دسترسی‌هایی که اگر این نسخه SplusLib آن‌ها را نشناسد، به کاربر گفته می‌شود.
RIGHT_LABELS = {
    "send_photos": "عکس", "send_media": "رسانه", "send_videos": "ویدیو",
    "send_animations": "گیف", "send_gifs": "گیف", "send_voices": "ویس",
    "send_voice_notes": "ویس", "send_audios": "موزیک", "send_documents": "فایل",
    "send_stickers": "استیکر", "send_links": "لینک", "embed_links": "پیش‌نمایش لینک",
    "send_polls": "نظرسنجی", "send_contacts": "مخاطب", "send_locations": "موقعیت",
    "send_video_notes": "ویدیو پیام",
}


def _skipped_note(skipped) -> str:
    labels = []
    for name in skipped or []:
        label = RIGHT_LABELS.get(name)
        if label and label not in labels:
            labels.append(label)
    if not labels:
        return ""
    return ("\n⚠️ این دسترسی‌ها در این نسخه روی سرور قابل بستن نبود: "
            + "، ".join(labels) + ". ربات آن‌ها را محلی هم حذف می‌کند.")


async def _release_server_mute(ctx, uid) -> bool:
    """Lift the server restriction of ONE member.

    A failed release is queued in ``unmute_pending`` and retried by
    ``restore_active_mutes``, so a restriction is never forgotten.
    """
    key = str(uid)
    try:
        await ctx.api.unmute_member(ctx.guid, uid)
    except Exception:
        ctx.g.setdefault("unmute_pending", {})[key] = time.time()
        return False
    ctx.g.setdefault("unmute_pending", {}).pop(key, None)
    return True


async def cleanup_expired_mutes(ctx):
    """تمام سکوت‌های منقضی‌شده را یک‌بار پاکسازی می‌کند."""
    g = ctx.g
    mute = g.setdefault("mute", {})
    now = time.time()
    expired = [guid for guid, until in list(mute.items())
               if until not in (0, True) and float(until or 0) <= now]
    total = 0
    for guid in expired:
        await _release_server_mute(ctx, guid)
        # حذف از mute و ثبت بازه؛ سپس تاریخچه همان بازه حذف می‌شود.
        mute.pop(guid, None)
        g.setdefault("mute_server_applied", {}).pop(str(guid), None)
        g.setdefault("mute_permission_last", {}).pop(str(guid), None)
        started = float(g.setdefault("mute_started", {}).pop(guid, 0) or 0)
        ids = list(g.setdefault("mute_messages", {}).pop(guid, []))
        if started:
            for item in db.data.get("history", {}).get(ctx.guid, []):
                if (item.get("u") == guid and item.get("m")
                        and float(item.get("t") or 0) >= started
                        and item.get("m") not in ids):
                    ids.append(item.get("m"))
            try:
                server_ids = await _scan_server_messages(ctx, guid, started, 3000)
                for mid in server_ids:
                    if mid not in ids:
                        ids.append(mid)
            except Exception:
                pass
        if ids:
            try:
                await ctx.api.delete(ctx.guid, ids[-3000:])
                total += min(len(ids), 3000)
            except Exception:
                pass
    if expired:
        db.save("groups")
    return total

async def cleanup_expired_mute(ctx, guid):
    """بعد از پایان سکوت، پیام‌های بازه شروع تا پایان سکوت را پاک می‌کند."""
    g = ctx.g
    pending = g.setdefault("mute_expired_cleanup", {})
    started = pending.pop(guid, None)
    if not started:
        return 0
    try:
        started = float(started)
    except Exception:
        return 0
    ids = list(g.setdefault("mute_messages", {}).pop(guid, []))
    try:
        server_ids = await _scan_server_messages(ctx, guid, started, 3000)
        for mid in server_ids:
            if mid not in ids:
                ids.append(mid)
    except Exception:
        pass
    if ids:
        try:
            await ctx.api.delete(ctx.guid, ids[-3000:])
        except Exception:
            pass
    db.save("groups")
    return len(ids)


async def restore_active_mutes(ctx):
    """Re-apply every still-active local mute to the server after a restart.

    Releases that failed earlier (``unmute_pending``) are retried first. This
    function never bans anyone. Protected ranks are released and removed from
    the local mute list.
    """
    if not ctx.is_group or not ctx.guid:
        return {"restored": 0, "removed_protected": 0, "expired": 0}
    g = ctx.g
    mute = g.setdefault("mute", {})
    pending = g.setdefault("unmute_pending", {})
    now = time.time()
    restored = removed_protected = expired = 0
    changed = False

    for uid in list(pending.keys()):
        until = mute.get(uid)
        if until is not None and (until in (0, True) or float(until or 0) > now):
            # Muted again since the release failed: the mute loop below applies it.
            pending.pop(uid, None)
            changed = True
            continue
        await _release_server_mute(ctx, uid)
        changed = True

    for guid, until in list(mute.items()):
        gid = str(guid)
        if until not in (0, True) and float(until or 0) <= now:
            await _release_server_mute(ctx, gid)
            mute.pop(gid, None)
            g.setdefault("mute_server_applied", {}).pop(gid, None)
            g.setdefault("mute_permission_last", {}).pop(gid, None)
            g.setdefault("mute_started", {}).pop(gid, None)
            g.setdefault("mute_reason", {}).pop(gid, None)
            g.setdefault("mute_messages", {}).pop(gid, None)
            expired += 1
            changed = True
            continue
        try:
            protection = await ctx.protection_reason(gid)
        except Exception:
            protection = None
        if protection and ("مقام" in protection or "سودو" in protection or "معاف" in protection):
            await _release_server_mute(ctx, gid)
            mute.pop(gid, None)
            g.setdefault("mute_server_applied", {}).pop(gid, None)
            g.setdefault("mute_permission_last", {}).pop(gid, None)
            g.setdefault("mute_started", {}).pop(gid, None)
            g.setdefault("mute_reason", {}).pop(gid, None)
            removed_protected += 1
            changed = True
            continue
        minutes = None
        if until not in (0, True):
            minutes = max(1, int((float(until) - now + 59) // 60))
        try:
            # DB is authoritative on startup: re-apply the full server restriction
            # even when the local flag said it was already applied.
            applied = await ctx.api.ensure_mute_permission(ctx.guid, gid, minutes=minutes)
            if not applied:
                raise RuntimeError("mute permission could not be re-applied")
            g.setdefault("mute_server_applied", {})[gid] = True
            g.setdefault("mute_permission_last", {})[gid] = time.time()
            restored += 1
        except Exception:
            g.setdefault("mute_server_applied", {})[gid] = False
            g.setdefault("mute_permission_last", {})[gid] = time.time()
            # The local dispatcher still deletes this member's messages; the restore
            # loop retries the server restriction.
    if changed or restored:
        db.save("groups")
    return {"restored": restored, "removed_protected": removed_protected, "expired": expired}

# ------------------------------------------------------------ بن همه
# فاصله‌ی بین هر بن تا محدودیت سرعت (flood wait) سرور رعایت شود.
BAN_ALL_DELAY = 0.35
# فهرست کاربرانی که «بن همه» بن کرده؛ «حذف بن همه» همین‌ها را برمی‌دارد و دوباره اضافه می‌کند.
BAN_ALL_RECORD = "ban_all_ids"


async def _ban_everyone(ctx) -> bool:
    """بن همه‌ی اعضای غیرمحافظت‌شده‌ی گروه.

    مقام‌های محافظت‌شده هرگز بن نمی‌شوند: خود FBI، فرستنده‌ی دستور، سازنده/سودو،
    مالک، ادمین، ادمین ویژه، کاربران معاف و ادمین‌های واقعی گروه.
    """
    import config
    from core import api as api_mod

    exempt = {str(x) for x in ctx.g.get("exempt", [])}
    sudos = {str(x) for x in getattr(config, "SUDOS", [])}
    rows = api_mod.members_of(await ctx.api.members(ctx.guid))
    if not rows:
        await ctx.err("فهرست اعضای گروه از سرور دریافت نشد. بات باید ادمین گروه باشد و دسترسی مدیریت اعضا داشته باشد؛ سپس دوباره تلاش کنید.")
        return True

    targets, protected, seen = [], 0, set()
    for row in rows:
        uid = api_mod.guid_of(row)
        if not uid:
            continue
        uid = str(uid)
        if uid in seen:
            continue
        seen.add(uid)
        is_real_staff = bool(api_mod._val(row, "is_admin", default=False)
                             or api_mod._val(row, "is_owner", default=False))
        if (uid == str(ctx.me) or uid == str(ctx.author) or uid in exempt or uid in sudos
                or is_real_staff or ranks.rank(ctx.guid, uid) > ranks.USER):
            protected += 1
            continue
        targets.append(uid)

    if not targets:
        await ctx.err(f"کاربری برای بن پیدا نشد؛ {utils.fa_digits(protected)} عضو محافظت‌شده هستند.")
        return True

    await ctx.ok(f"⏳ شروع بن {utils.fa_digits(len(targets))} کاربر...")
    record = ctx.g.setdefault(BAN_ALL_RECORD, [])
    banned, failed = 0, 0
    for uid in targets:
        try:
            await ctx.api.ban(ctx.guid, uid)
            banned += 1
            if uid not in record:
                record.append(uid)
            if banned % 25 == 0:
                db.save("groups")
        except Exception as exc:
            failed += 1
            db.log("ban_all", f"chat={ctx.guid} user={uid}: {type(exc).__name__}: {exc}")
        await asyncio.sleep(BAN_ALL_DELAY)

    db.save("groups")
    db.log("ban_all", f"chat={ctx.guid} by={ctx.author} banned={banned} failed={failed} protected={protected}")
    db.save("logs")
    await ctx.ok(
        "🚫 بن همگانی انجام شد.\n"
        f"✅ بن‌شده: {utils.fa_digits(banned)}\n"
        f"🛡 محافظت‌شده: {utils.fa_digits(protected)}\n"
        f"⚠️ ناموفق: {utils.fa_digits(failed)}"
    )
    return True


def _add_succeeded(res) -> bool:
    """همان منطق موفقیت افزودن عضو که در «حذف بن» استفاده می‌شود."""
    if isinstance(res, dict):
        return bool(res.get("ok", True)) and str(res.get("status", "OK")).upper() not in {"ERROR", "FAILED"}
    return bool(res) or res is None


async def _unban_everyone(ctx) -> bool:
    """رفع بن کاربرانی که «بن همه» بن کرده و افزودن دوباره‌ی آن‌ها به گروه.

    فقط کاربرانی برگردانده می‌شوند که خودِ «بن همه» بن کرده است؛ بن‌های دستی
    دیگر دست‌نخورده می‌مانند. موفق‌ها از فهرست خارج می‌شوند و ناموفق‌ها باقی
    می‌مانند تا با اجرای دوباره‌ی دستور تلاش شوند.
    """
    from core import api as api_mod

    record = ctx.g.setdefault(BAN_ALL_RECORD, [])
    pending = list(dict.fromkeys(str(x) for x in record))
    if not pending:
        await ctx.err("هیچ کاربری با «بن همه» بن نشده است.")
        return True

    await ctx.ok(f"♻️ شروع رفع بن و افزودن دوباره‌ی {utils.fa_digits(len(pending))} کاربر...")
    # اعضای فعلی را یک‌بار می‌خوانیم تا کسانی که خودشان دوباره آمده‌اند، بی‌دلیل اضافه نشوند.
    current = {
        str(api_mod.guid_of(row))
        for row in api_mod.members_of(await ctx.api.members(ctx.guid))
        if api_mod.guid_of(row)
    }

    readded = already_in = unban_failed = add_failed = 0
    for uid in pending:
        if uid in current:
            already_in += 1
            record.remove(uid)
            continue

        try:
            await ctx.api.unban(ctx.guid, uid)
        except Exception as exc:
            unban_failed += 1
            db.log("unban_all", f"chat={ctx.guid} user={uid}: {type(exc).__name__}: {exc}")
            await asyncio.sleep(BAN_ALL_DELAY)
            continue

        added = False
        for attempt in range(2):
            try:
                added = _add_succeeded(await ctx.api.add_member(ctx.guid, uid))
            except Exception as exc:
                db.log("unban_all", f"chat={ctx.guid} add user={uid}: {type(exc).__name__}: {exc}")
                added = False
            if added:
                break
            if attempt == 0:
                await asyncio.sleep(0.5)

        if added:
            readded += 1
            record.remove(uid)
        else:
            add_failed += 1
        if (readded + already_in) % 25 == 0:
            db.save("groups")
        await asyncio.sleep(BAN_ALL_DELAY)

    db.save("groups")
    db.log("unban_all", f"chat={ctx.guid} by={ctx.author} readded={readded} already_in={already_in} "
                        f"unban_failed={unban_failed} add_failed={add_failed}")
    db.save("logs")

    remaining = unban_failed + add_failed
    tail = "\n⚠️ کاربرانِ ناموفق در فهرست می‌مانند؛ دوباره «حذف بن همه» را بزنید." if remaining else ""
    await ctx.ok(
        "♻️ حذف بن همگانی انجام شد.\n"
        f"🔓 بن برداشته‌شده و دوباره اضافه‌شده: {utils.fa_digits(readded)}\n"
        f"👥 از قبل در گروه: {utils.fa_digits(already_in)}\n"
        f"⚠️ ناموفق: {utils.fa_digits(remaining)}"
        f"{tail}"
    )
    return True


def _starts(text, word):
    from core import utils as _u
    t, w = _u.normalize(text), _u.normalize(word)
    return t == w or t.startswith(w + " ")


async def handle(ctx) -> bool:
    text = ctx.text
    g = ctx.g

    # ------------------------------------------------ لیست بن (بن‌های ثبت‌شده توسط ربات)
    if utils.normalize(text) == utils.normalize("لیست بن"):
        if ctx.rank < ranks.ADMIN:
            return await _deny(ctx)
        record = g.get("ban_record") or {}
        if not record:
            await ctx.reply("🚫 لیست بن خالی است.\n(فقط بن‌هایی نمایش داده می‌شوند که از طریق ربات انجام شده‌اند.)")
            return True
        lines = ["🚫 بن‌های ثبت‌شده توسط ربات:"]
        for i, uid in enumerate(sorted(record, key=lambda u: record[u], reverse=True)[:50], 1):
            name = await ctx.target_link(uid)
            lines.append(f"{utils.fa_digits(i)}. {name}")
        await ctx.reply("\n".join(lines))
        return True

    # ------------------------------------------------ لیست‌های کاربری
    # ------------------------------------------------ سیک / آن سیک (اخراج بدون بن دائمی)
    for word, mode in (("آن سیک", "unsik"), ("سیک", "sik")):
        if _starts(text, word):
            if ctx.rank < ranks.ADMIN:
                return await _deny(ctx)
            target = await ctx.target()
            if not target:
                await ctx.err("روی پیام کاربر ریپلای بزنید یا آیدی/یوزرنیم او را بفرستید.")
                return True
            reason = await ctx.protection_reason(target)
            if reason and mode == "sik":
                await ctx.err(f"این کاربر محافظت شده است ({reason}).")
                return True
            name = await ctx.target_link(target)
            if mode == "sik":
                await ctx.api.ban(ctx.guid, target)      # ban then unban: removed, not blocked
                await ctx.api.unban(ctx.guid, target)
                await ctx.ok(f"کاربر {name} از گروه خارج شد (بن دائمی نشد).")
            else:
                await ctx.api.unban(ctx.guid, target)
                await ctx.ok(f"بن کاربر {name} برداشته شد.")
            return True

    # ------------------------------------------------ کاهش / پاکسازی اخطار
    for word, mode in (("کاهش اخطار", "dec"), ("پاکسازی اخطار", "clear")):
        if _starts(text, word):
            if ctx.rank < ranks.ADMIN:
                return await _deny(ctx)
            target = await ctx.target()
            if not target:
                await ctx.err("روی پیام کاربر ریپلای بزنید یا آیدی/یوزرنیم او را بفرستید.")
                return True
            warns = g.setdefault("warns", {})
            current = int(warns.get(target, 0) or 0)
            new = max(0, current - 1) if mode == "dec" else 0
            if new:
                warns[target] = new
            else:
                warns.pop(target, None)
            db.save("groups")
            name = await ctx.target_link(target)
            await ctx.ok(f"اخطار {name} {'کاهش یافت' if mode == 'dec' else 'پاک شد'}؛ اکنون {utils.fa_digits(new)} اخطار دارد.")
            return True

    # ------------------------------------------------ پاکسازی لیست بن (فهرست محلی ربات)
    if utils.normalize(text) == utils.normalize("پاکسازی لیست بن"):
        if ctx.rank < ranks.ADMIN:
            return await _deny(ctx)
        count = len(g.get("ban_record") or {})
        g["ban_record"] = {}
        db.save("groups")
        await ctx.ok(f"فهرست بن محلی ربات پاک شد ({utils.fa_digits(count)} مورد). بن‌های سرور تغییر نکردند.")
        return True

    for fa, (key, title, need) in USER_LISTS.items():
        # پاکسازی
        if text == f"پاکسازی لیست {fa}":
            if ctx.rank < need:
                return await _deny(ctx)
            if key == "mute":
                released_uids = list(g.get("mute", {}).keys())
                g["mute"] = {}
                for uid in released_uids:
                    g.setdefault("mute_server_applied", {}).pop(str(uid), None)
                    g.setdefault("mute_permission_last", {}).pop(str(uid), None)
                    await _release_server_mute(ctx, uid)
            else:
                g[key] = []
            db.save("groups")
            pending_count = len(g.get("unmute_pending", {}))
            tail = f"\n⚠️ {utils.fa_digits(pending_count)} رفع سکوت سروری هنوز تأیید نشده؛ ربات ادامه می‌دهد." if pending_count else ""
            await ctx.ok(f"لیست {title} پاکسازی شد.{tail}")
            return True

        # نمایش لیست
        m = re.match(rf"^لیست {fa}(?: (\d+))?$", text)
        if m:
            if ctx.rank < need:
                return await _deny(ctx)
            items = list(g[key].keys()) if key == "mute" else list(g[key])
            await _show(ctx, items, title, m.group(1))
            return True

        # حذف از لیست
        if utils.command_hit(text, f"حذف {fa}") or (ctx.reply_id and text == f"حذف {fa}"):
            if ctx.rank < need:
                return await _deny(ctx)
            target = await ctx.target()
            if not target:
                return await _need_target(ctx)
            if key == "mute":
                g["mute"].pop(target, None)
                g.setdefault("mute_server_applied", {}).pop(target, None)
                g.setdefault("mute_permission_last", {}).pop(target, None)
                g.setdefault("mute_reason", {}).pop(target, None)
                released = await _release_server_mute(ctx, target)
                started = float(g.setdefault("mute_started", {}).pop(target, 0) or 0)
                # V2: پاکسازی از زمان باز/فعال شدن گروه؛ در کنار تاریخچه محلی.
                group_opened = float(g.get("opened_at") or 0)
                if group_opened and (not started or group_opened < started):
                    started = group_opened
                ids = list(g.setdefault("mute_messages", {}).pop(target, []))
                # تاریخچه‌ی ثبت‌شده فقط از زمان شروع سکوت به بعد پاک شود.
                for item in db.data.get("history", {}).get(ctx.guid, []):
                    if (item.get("u") == target and item.get("m")
                            and float(item.get("t") or 0) >= started
                            and item.get("m") not in ids):
                        ids.append(item.get("m"))
                # علاوه بر تاریخچه محلی، تا جای ممکن تاریخچه سرور هم اسکن می‌شود.
                try:
                    server_ids = await _scan_server_messages(ctx, target, started, 3000)
                    for mid in server_ids:
                        if mid not in ids: ids.append(mid)
                except Exception:
                    pass
                if ids:
                    await ctx.api.delete(ctx.guid, ids[-3000:])
                g.setdefault("mute_messages", {}).pop(target, None)
            elif target in g[key]:
                g[key].remove(target)
            db.save("groups")
            name = await ctx.target_link(target)
            tail = ""
            if key == "mute" and not released:
                tail = "\n⚠️ رفع سکوت سروری هنوز تأیید نشده؛ ربات تا تأیید خودکار ادامه می‌دهد."
            await ctx.ok(f"کاربر {name} از لیست {title} خارج شد.{tail}", mention=target)
            return True

        # افزودن به لیست
        if key == "mute":
            hit, rest = utils.mute_command_hit(text, fa)
        else:
            hit, rest = (utils.command_hit(text, fa) or (ctx.reply_id and text == fa)), ""
        if hit:
            if ctx.rank < need:
                return await _deny(ctx)
            target = await ctx.target()
            if not target:
                return await _need_target(ctx)
            reason = await ctx.protection_reason(target)
            if reason and key != "exempt":
                await ctx.reply(reason)
                return True
            name = await ctx.target_link(target)

            if key == "mute":
                minutes = utils.parse_mute_minutes(rest) if rest else None
                mute_seconds = (minutes * 60) if minutes else 0
                g.setdefault("mute", {})[target] = \
                    (time.time() + mute_seconds) if mute_seconds else 0
                g.setdefault("mute_started", {})[target] = time.time()
                g.setdefault("mute_messages", {})[target] = []
                g.setdefault("mute_reason", {})[target] = "manual"
                db.save("groups")
                server_ok = False
                server_error = ""
                skipped = []
                try:
                    result = await ctx.api.mute_member(ctx.guid, target, minutes=minutes)
                    server_ok = True
                    if isinstance(result, dict):
                        skipped = list(result.get("skipped") or [])
                except Exception as exc:
                    server_error = str(exc) or type(exc).__name__
                g.setdefault("mute_server_applied", {})[target] = bool(server_ok)
                g.setdefault("mute_permission_last", {})[target] = time.time()
                db.save("groups")
                when = f" به مدت {utils.format_mute_duration(minutes)}" if minutes else " دائمی"
                if server_ok:
                    extra = _skipped_note(skipped)
                else:
                    extra = (f"\n⚠️ سکوت کامل روی سرور اعمال نشد: {server_error}\n"
                             "تا تأیید سرور، ربات پیام‌های این کاربر را حذف می‌کند.")
                await ctx.ok(f"کاربر {name} سکوت شد{when}. 🔇{extra}", mention=target)
            else:
                if target not in g[key]:
                    g[key].append(target)
                db.save("groups")
                await ctx.ok(f"کاربر {name} به لیست {title} اضافه شد.", mention=target)
            return True

    # ------------------------------------------------ بن همه (فقط مالک و سازنده)
    if text == "بن همه":
        if ctx.rank < ranks.OWNER:
            return await _deny(ctx, who="مالک و سازنده")
        return await _ban_everyone(ctx)

    if text == "حذف بن همه":
        if ctx.rank < ranks.OWNER:
            return await _deny(ctx, who="مالک و سازنده")
        return await _unban_everyone(ctx)

    # ------------------------------------------------ بن / حذف بن
    if utils.command_hit(text, "بن") or text == "اخراج":
        if ctx.rank < ranks.ADMIN:
            return await _deny(ctx)
        target = await ctx.target()
        if not target:
            return await _need_target(ctx)
        reason = await ctx.protection_reason(target)
        if reason:
            await ctx.reply(reason)
            return True
        name = await ctx.target_link(target)
        await ctx.api.ban(ctx.guid, target)
        # «کیل» یک لیست موقتِ حذف خودکار است؛ بعد از اجرای آن، کاربر دیگر
        # نباید با ورود دوباره دوباره به‌صورت خودکار حذف شود.
        if target in g.get("kill", []):
            g["kill"].remove(target)
            db.save("groups")
        await ctx.ok(f"کاربر {name} از گروه حذف (بن) شد. 🚫", mention=target)
        return True

    if utils.command_hit(text, "حذف بن") or utils.command_hit(text, "آن بن"):
        if ctx.rank < ranks.ADMIN:
            return await _deny(ctx)
        target = await ctx.target()
        if not target:
            return await _need_target(ctx)
        await ctx.api.unban(ctx.guid, target)
        # رفع بن در روبیکا لزوماً کاربر را دوباره عضو نمی‌کند؛ پس بعد از رفع بن
        # تلاش می‌کنیم همان کاربر را دوباره به گروه اضافه کنیم.
        added = False
        for _attempt in range(2):
            try:
                res = await ctx.api.add_member(ctx.guid, target)
                if isinstance(res, dict):
                    added = bool(res.get("ok", True)) and str(res.get("status", "OK")).upper() not in {"ERROR", "FAILED"}
                else:
                    added = bool(res) or res is None
                if added:
                    break
            except Exception:
                if _attempt == 0:
                    await asyncio.sleep(0.5)
        name = await ctx.target_link(target)
        if added:
            await ctx.ok(f"بنِ کاربر {name} برداشته شد و کاربر دوباره به گروه اضافه شد. ✅", mention=target)
        else:
            await ctx.ok(f"بنِ کاربر {name} برداشته شد، اما افزودن دوباره از سمت سرور انجام نشد. ⚠️", mention=target)
        return True

    if text == "پاکسازی لیست سیاه":
        if ctx.rank < ranks.OWNER:
            return await _deny(ctx)
        res = await ctx.api.banned_members(ctx.guid)
        from core import api as api_mod
        count = 0
        for member in api_mod.members_of(res):
            guid = api_mod.guid_of(member)
            if guid:
                await ctx.api.unban(ctx.guid, guid)
                count += 1
        await ctx.ok(f"{utils.fa_digits(count)} کاربر از لیست سیاه خارج شدند.")
        return True

    # ------------------------------------------------ لیست‌های کلمه‌ای
    for fa, (key, title, need) in WORD_LISTS.items():
        if text == f"پاکسازی لیست {fa}":
            if ctx.rank < need:
                return await _deny(ctx)
            g[key] = []
            db.save("groups")
            await ctx.ok(f"لیست {title} پاکسازی شد.")
            return True

        m = re.match(rf"^لیست {fa}(?: (\d+))?$", text)
        if m:
            if ctx.rank < need:
                return await _deny(ctx)
            await _show(ctx, g[key], title, m.group(1), raw=True)
            return True

        m = re.match(rf"^حذف {fa} (.+)$", text)
        if m:
            if ctx.rank < need:
                return await _deny(ctx)
            word = m.group(1).strip()
            if word in g[key]:
                g[key].remove(word)
                db.save("groups")
                await ctx.ok(f"«{word}» از لیست {title} حذف شد.")
            else:
                await ctx.err(f"«{word}» در لیست {title} نبود.")
            return True

        m = re.match(rf"^{fa} (.+)$", text)
        if m:
            if ctx.rank < need:
                return await _deny(ctx)
            word = m.group(1).strip()
            if word not in g[key]:
                g[key].append(word)
                db.save("groups")
            await ctx.ok(f"«{word}» به لیست {title} اضافه شد.")
            return True

    # ------------------------------------------------ اخطار
    if text == "حذف تمام اخطار ها" or text == "حذف تمام اخطارها":
        if ctx.rank < ranks.SPECIAL:
            return await _deny(ctx, "ادمین ویژه و مالک")
        g["warns"] = {}
        db.save("groups")
        await ctx.ok("اخطار تمام کاربران حذف شد.")
        return True

    if utils.command_hit(text, "حذف اخطار"):
        if ctx.rank < ranks.ADMIN:
            return await _deny(ctx)
        target = await ctx.target()
        if not target:
            return await _need_target(ctx)
        g.get("warns", {}).pop(target, None)
        db.save("groups")
        name = await ctx.target_link(target)
        await ctx.ok(f"اخطارهای کاربر {name} حذف شد.")
        return True

    # فقط خود دستور «اخطار» یا «اخطار <GUID> [دلیل]» معتبر است؛
    # وجود کلمه اخطار داخل یک جمله هرگز نباید دستور را اجرا کند.
    warning_cmd = (text == "اخطار" or bool(re.match(r"^اخطار\s+(?:-?\d+|@[A-Za-z][A-Za-z0-9_]{2,})(?:\s+.+)?$", text, re.S)))
    if warning_cmd:
        if ctx.rank < ranks.ADMIN:
            return await _deny(ctx)
        target = await ctx.target()
        if not target:
            return await _need_target(ctx)
        reason = await ctx.protection_reason(target)
        if reason:
            await ctx.reply(reason)
            return True
        if "\n" in ctx.raw_text:
            why = ctx.raw_text.split("\n", 1)[1].strip()
        else:
            rest = text[5:].strip()
            token = utils.extract_guid(rest)
            why = rest.replace(token, "", 1).strip() if token else rest
        count = g.setdefault("warns", {}).get(target, 0) + 1
        g["warns"][target] = count
        db.save("groups")
        name = await ctx.target_link(target)
        maximum = int(g.get("max_warn") or 5)

        if count >= maximum:
            g["warns"].pop(target, None)
            db.save("groups")
            await ctx.api.ban(ctx.guid, target)
            await ctx.reply(f"🚫 کاربر {name} به سقف اخطار رسید و حذف شد.", mention=target)
        else:
            body = f"⚠️ به کاربر {name} اخطار داده شد.\n" \
                   f"تعداد: {utils.fa_digits(count)} از {utils.fa_digits(maximum)}"
            if why:
                body += f"\n📌 دلیل: {why}"
            await ctx.reply(body, mention=target)
        return True

    if text in ("اخطارم", "اخطارش"):
        target = ctx.author if text == "اخطارم" else await ctx.target()
        if not target:
            return await _need_target(ctx)
        count = g.get("warns", {}).get(target, 0)
        name = await ctx.target_link(target)
        await ctx.reply(f"⚠️ اخطارهای {name}: {utils.fa_digits(count)} "
                        f"از {utils.fa_digits(g.get('max_warn', 5))}")
        return True

    return False


# ------------------------------------------------------------ نمایش
async def _show(ctx, items, title, page, raw=False):
    items = list(items)
    if not items:
        await ctx.reply(f"📋 لیست {title} خالی است.")
        return
    chunk, page, total = utils.paginate(items, int(page or 1))
    lines = [f"📋 لیست {title} "
             f"({utils.fa_digits(len(items))} مورد) — "
             f"صفحه {utils.fa_digits(page)}/{utils.fa_digits(total)}\n"]
    for i, item in enumerate(chunk, 1 + (page - 1) * utils.PAGE_SIZE):
        if raw:
            lines.append(f"{utils.fa_digits(i)}. {item}")
        else:
            name = await ctx.target_link(item)
            lines.append(f"{utils.fa_digits(i)}. {name}\n    `{item}`")
    if total > 1:
        lines.append(f"\nبرای صفحه بعد: لیست {title} {utils.fa_digits(page + 1)}")
    await ctx.reply("\n".join(lines))


async def _deny(ctx, who="ادمین‌ها، ادمین‌های ویژه و مالک"):
    await ctx.err(f"این دستور فقط برای {who} قابل اجراست.")
    return True


async def _need_target(ctx):
    await ctx.err("روی پیام کاربر ریپلای بزنید یا شناسه‌ی او را بعد از دستور بنویسید.")
    return True
