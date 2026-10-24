# -*- coding: utf-8 -*-
"""FBI V2 feature pack: history cleanup, subordinate groups, music controls."""
import os
import re
import time
import asyncio
import config
from core import ranks, utils, api as api_mod
from core.db import db
from modules import media
from modules.group_tools import pinned_set, is_pinned_message


def _music(g):
    return g.setdefault("music_state", {
        "queue": [], "current": None, "volume": 100,
        "speed": 1.0, "muted": False, "position": 0,
        "target": None, "recent": [], "repeat": False
    })

def _message_time(msg):
    """Unix seconds of a Splus message, or None when the server gave no usable time."""
    raw = api_mod._val(msg, "timestamp", "time", "date", default=None)
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    if value <= 0:
        return None
    return value / 1000.0 if value > 10 ** 11 else value


async def _history_scan(ctx, target, since=0, limit=3000):
    """Best-effort server-side scan using recent/interval messages + local history."""
    ids = []
    seen = set()
    for item in db.data.get("history", {}).get(ctx.guid, []):
        if item.get("u") == target and float(item.get("t") or 0) >= since and item.get("m"):
            mid = str(item["m"])
            if mid not in seen:
                seen.add(mid); ids.append(mid)
    middle = ctx.message_id
    for _ in range(40):
        if len(ids) >= limit:
            break
        res = await ctx.api.get_last_messages(ctx.guid, middle)
        messages = api_mod._val(res, "messages", default=[]) or []
        if not messages:
            break
        progressed = False
        for msg in messages:
            mid = api_mod._val(msg, "id", "message_id")
            author = api_mod.sender_guid_of(msg)
            ts = api_mod._val(msg, "timestamp", "time", "date") or 0
            try: ts = float(ts)
            except Exception: ts = 0
            if mid and author == target and (not since or not ts or ts >= since):
                mid = str(mid)
                if mid not in seen:
                    seen.add(mid); ids.append(mid); progressed = True
        last = api_mod._val(messages[-1], "id", "message_id")
        if not last or str(last) == str(middle):
            break
        middle = last
        if not progressed and len(messages) < 2:
            break
    return ids[-limit:]

async def handle(ctx):
    text = ctx.text
    g = ctx.g

    # creator-only subordinate group
    if text == "زیرپرچم من":
        if ctx.author not in config.SUDOS:
            await ctx.err("این دستور فقط برای سازنده FBI است.")
            return True
        g["subordinate"] = True
        g["subordinate_owner"] = str(ctx.author)
        g["subscription_active"] = True
        g["subscription_until"] = 0
        g["subscription_revoked_at"] = 0
        g["subscription_notice_sent_at"] = 0
        g["active"] = True
        g["manual_disabled"] = False
        g["opened_at"] = g.get("opened_at") or time.time()
        db.save("groups")
        await ctx.ok("این گروه با موفقیت زیرپرچم من ثبت شد. ⚡")
        return True

    if text == "لغو زیرپرچم":
        if ctx.author not in config.SUDOS:
            await ctx.err("این دستور فقط برای سازنده FBI است.")
            return True
        g["subordinate"] = False
        # بعد از لغو زیرپرچم، اگر اشتراک سراسری گروه قبلاً لغو شده باشد، FBI دوباره فعال نشود.
        if not g.get("subscription_active", True):
            g["active"] = False
        db.save("groups")
        await ctx.ok("گروه از زیرپرچم خارج شد.")
        return True

    # remove all observed/history messages of muted user, best effort
    if text in ("پاکسازی پیام‌های سکوت", "پاکسازی پیام های سکوت"):
        if ctx.rank < ranks.ADMIN:
            await ctx.err("این دستور فقط برای ادمین است.")
            return True
        target = await ctx.target()
        if not target:
            await ctx.err("روی پیام کاربر ریپلای بزنید یا شناسه او را بنویسید.")
            return True
        since = float(g.get("opened_at") or 0)
        ids = await _history_scan(ctx, target, since, 3000)
        if ids:
            await ctx.api.delete(ctx.guid, ids)
        await ctx.ok(f"تلاش برای پاکسازی {utils.fa_digits(len(ids))} پیام انجام شد.")
        return True

    # ------------------------------------------------ حذف همه پیام‌ها (دو مرحله‌ای، فقط مالک)
    if utils.compact(text) == utils.compact("حذف همه پیام‌ها"):
        if ctx.rank < ranks.OWNER:
            await ctx.err("فقط مالک گروه می‌تواند همه پیام‌ها را حذف کند.")
            return True
        since = float(g.get("opened_at") or 0)
        if not since:
            await ctx.err("زمان باز شدن این گروه ثبت نشده است. یک‌بار «گروه باز» را بفرستید تا ساعت باز شدن ثبت شود؛ بعد از آن حذف از همان ساعت انجام می‌شود. بسته یا باز بودن فعلی گروه روی این کار اثری ندارد.")
            return True
        g["purge_all_pending"] = {"at": time.time(), "since": since}
        db.save("groups")
        await ctx.reply(
            "⚠️ همه پیام‌های این گروه از زمان باز شدن ("
            + time.strftime("%Y-%m-%d %H:%M", time.localtime(since))
            + ") تا همین لحظه حذف می‌شوند.\n"
            "برای تأیید، تا ۲ دقیقه دیگر «تأیید حذف همه پیام‌ها» را بفرستید."
        )
        return True

    if ctx.rank >= ranks.OWNER and utils.compact(text).startswith(utils.compact("حذف همه پیام")):
        await ctx.reply("متن دقیق دستور «حذف همه پیام‌ها» است. برای تأیید، تا ۲ دقیقه بعد «تأیید حذف همه پیام‌ها» را بفرستید.")
        return True

    if utils.compact(text) == utils.compact("تأیید حذف همه پیام‌ها"):
        if ctx.rank < ranks.OWNER:
            await ctx.err("فقط مالک گروه می‌تواند همه پیام‌ها را حذف کند.")
            return True
        if not ctx.api.delete_supported():
            await ctx.err("در این نسخه SplusLib متد حذف پیام پیدا نشد؛ هیچ پیامی حذف نشد.")
            db.log("purge", "delete_messages missing in client; purge refused")
            return True
        pending = g.pop("purge_all_pending", None) or {}
        db.save("groups")
        if not pending or time.time() - float(pending.get("at") or 0) > 120:
            await ctx.err("درخواست فعالی برای حذف همه پیام‌ها نیست یا مهلت تأیید گذشته است. دوباره «حذف همه پیام‌ها» را بفرستید.")
            return True
        since = float(pending.get("since") or 0)
        until = time.time()
        try:
            rows = await ctx.api.scan_all_messages(
                ctx.guid, max_messages=getattr(config, "SCAN_ALL_MAX_MESSAGES", 100000))
        except Exception as exc:
            await ctx.err(f"خواندن تاریخچه انجام نشد: {type(exc).__name__}: {exc}")
            return True
        ids, unknown_time, scanned = [], 0, len(rows or [])
        pinned = await pinned_set(ctx)  # سنجاق‌شده‌ها هرگز در حذف همه پیام‌ها نمی‌روند
        for msg in rows or []:
            mid = api_mod._val(msg, "id", "message_id", default=None)
            ts = _message_time(msg)
            if mid is None or str(mid) in pinned or is_pinned_message(msg):
                continue
            if ts is None:
                unknown_time += 1  # never delete on a guess about the time
                continue
            if since <= ts <= until:
                ids.append(str(mid))
        db.log("purge", f"scanned={scanned} window={len(ids)} unknown_time={unknown_time} since={since:.0f}")
        if not ids:
            await ctx.err(
                f"هیچ پیامی در بازه پیدا نشد. پیام‌های خوانده‌شده: {utils.fa_digits(scanned)}، "
                f"بدون زمان معتبر: {utils.fa_digits(unknown_time)}. "
                "اگر تعداد خوانده‌شده صفر است، خواندن تاریخچه روی این گروه پاسخ نداده است."
            )
            return True
        for start in range(0, len(ids), 100):
            await ctx.api.delete(ctx.guid, ids[start:start + 100])
            await asyncio.sleep(0.5)
        note = f"، {utils.fa_digits(unknown_time)} پیام بدون زمان معتبر دست‌نخورده ماند" if unknown_time else ""
        await ctx.ok(
            f"درخواست حذف {utils.fa_digits(len(ids))} پیام از {utils.fa_digits(scanned)} پیام خوانده‌شده ارسال شد{note}. "
            "نتیجه‌ی تک‌تک پیام‌ها از سرور گزارش نمی‌شود؛ اگر پیامی باقی ماند، لاگ را بررسی کنید."
        )
        return True

    # music state controls
    if text in ("لیست آهنگ", "لیست موزیک", "پلی لیست"):
        m = _music(g)
        q = m["queue"]
        if not q:
            await ctx.reply("🎵 صف پخش خالی است.")
        else:
            await ctx.reply("🎵 صف پخش:\n" + "\n".join(f"{i+1}. {x}" for i,x in enumerate(q[:50])))
        return True

    if text == "آهنگ بعدی" or text == "بعدی":
        m = _music(g)
        if not m.get("queue"):
            await ctx.err("🎵 پلی‌لیست آهنگ بعدی ندارد.")
            return True
        item = m["queue"].pop(0)
        path = media._item_path(item)
        title = media._item_title(item)
        if not path or not os.path.isfile(path):
            await ctx.err("❌ فایل آهنگ بعدی پیدا نشد.")
            db.save("groups")
            return True
        old = m.get("current_path")
        if old:
            m.setdefault("recent", []).append({"path": old, "title": m.get("current") or media._item_title(old)})
            m["recent"] = m["recent"][-50:]
        await ctx.api.voice_discard(ctx.guid, await ctx.api.active_voice_id(ctx.guid))
        ok = await media.play_path(ctx, path, title)
        if ok:
            m["current"] = title; m["current_path"] = os.path.abspath(path)
            db.save("groups")
        return True

    if text == "آهنگ قبلی" or text == "قبلی":
        m = _music(g)
        hist = m.get("recent") or []
        if not hist:
            await ctx.err("🎵 آهنگ قبلی در پلی‌لیست وجود ندارد.")
            return True
        item = hist.pop()
        path = media._item_path(item)
        title = media._item_title(item)
        if not path or not os.path.isfile(path):
            await ctx.err("❌ فایل آهنگ قبلی پیدا نشد.")
            db.save("groups")
            return True
        current = m.get("current_path")
        if current:
            m.setdefault("queue", []).insert(0, {"path": current, "title": m.get("current") or media._item_title(current)})
        await ctx.api.voice_discard(ctx.guid, await ctx.api.active_voice_id(ctx.guid))
        ok = await media.play_path(ctx, path, title)
        if ok:
            m["current"] = title; m["current_path"] = os.path.abspath(path)
            db.save("groups")
        return True

    if text in ("بیصدا", "بی صدا"):
        if ctx.rank < ranks.SPECIAL:
            await ctx.err("فقط ادمین ویژه و مالک.")
            return True
        _music(g)["muted"] = True; db.save("groups"); await ctx.ok("صدای پخش بی‌صدا شد. 🔇"); return True
    if text in ("با صدا", "صدادار"):
        if ctx.rank < ranks.SPECIAL:
            await ctx.err("فقط ادمین ویژه و مالک.")
            return True
        _music(g)["muted"] = False; db.save("groups"); await ctx.ok("صدا فعال شد. 🔊"); return True

    m = re.match(r"^تنظیم صدا\s+(\d{1,3})$", text)
    if m:
        if ctx.rank < ranks.SPECIAL: await ctx.err("فقط ادمین ویژه و مالک."); return True
        v=max(0,min(100,int(m.group(1)))); _music(g)["volume"]=v; db.save("groups")
        await ctx.ok(f"صدای پخش روی {utils.fa_digits(v)}٪ تنظیم شد."); return True

    if text in ("کم", "زیاد"):
        if ctx.rank < ranks.SPECIAL:
            await ctx.err("فقط ادمین ویژه و مالک."); return True
        m = _music(g)
        delta = -15 if text == "کم" else 15
        m["volume"] = max(0, min(100, int(m.get("volume", 100)) + delta))
        m["muted"] = (m["volume"] == 0)
        db.save("groups")
        await ctx.ok(f"صدا روی {utils.fa_digits(m['volume'])}٪ تنظیم شد.")
        return True

    if text in ("تکرار فعال", "تکرار غیرفعال"):
        if ctx.rank < ranks.SPECIAL:
            await ctx.err("فقط ادمین ویژه و مالک."); return True
        _music(g)["repeat"] = text == "تکرار فعال"
        db.save("groups")
        await ctx.ok("تکرار خودکار فعال شد." if text == "تکرار فعال" else "تکرار خودکار غیرفعال شد.")
        return True

    if text == "پخش از اول":
        if ctx.rank < ranks.SPECIAL:
            await ctx.err("فقط ادمین ویژه و مالک."); return True
        _music(g)["position"] = 0
        db.save("groups")
        await ctx.ok("پخش از ثانیه اول تنظیم شد.")
        return True

    m = re.match(r"^(جلو|عقب)\s+(\d+)$", text)
    if m:
        if ctx.rank < ranks.SPECIAL:
            await ctx.err("فقط ادمین ویژه و مالک."); return True
        delta = int(m.group(2)) * (1 if m.group(1) == "جلو" else -1)
        mm = _music(g); mm["position"] = max(0, int(mm.get("position", 0)) + delta)
        db.save("groups")
        await ctx.ok(f"موقعیت پخش {utils.fa_digits(abs(delta))} ثانیه {'جلو' if delta>0 else 'عقب'} رفت.")
        return True

    m = re.match(r"^تنظیم سرعت\s+(0?\.\d+|[1-3](?:\.\d+)?)$", text)
    if m:
        if ctx.rank < ranks.SPECIAL: await ctx.err("فقط ادمین ویژه و مالک."); return True
        s=float(m.group(1));
        if not .25 <= s <= 3: await ctx.err("سرعت باید بین ۰٫۲۵ تا ۳ باشد."); return True
        _music(g)["speed"]=s; db.save("groups")
        await ctx.ok(f"سرعت پخش روی {s:g}x تنظیم شد. برای آهنگ بعدی اعمال می‌شود."); return True

    if text in ("تارگت", "حذف تارگت", "لغو تارگت"):
        if ctx.rank < ranks.SPECIAL:
            await ctx.err("فقط ادمین ویژه و مالک."); return True
        _music(g)["target"] = None
        db.save("groups")
        await ctx.ok("تارگت پخش حذف شد.")
        return True

    m = re.match(r"^(تارگت|target)\s+(@?\S+)$", text, re.I)
    if m:
        if ctx.rank < ranks.SPECIAL: await ctx.err("فقط ادمین ویژه و مالک."); return True
        _music(g)["target"]=m.group(2); db.save("groups"); await ctx.ok(f"تارگت پخش روی {m.group(2)} تنظیم شد."); return True

    if text in ("وضعیت پخش", "وضعیت موزیک"):
        m=_music(g)
        await ctx.reply(f"🎵 آهنگ فعلی: {m.get('current') or 'ندارد'}\n🔊 صدا: {m['volume']}٪\n⚡ سرعت: {m['speed']}x\n📍 موقعیت: {m['position']} ثانیه\n🎯 تارگت: {m.get('target') or 'گروه'}")
        return True

    return False
