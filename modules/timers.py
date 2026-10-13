# -*- coding: utf-8 -*-
"""سیستم تایمر: اجرای خودکار دستورات در ساعت مشخص یا به‌صورت دوره‌ای."""

import re
import time
import asyncio
import logging

from core import ranks, utils
from core.db import db

log = logging.getLogger("fbi.timers")


class FakeUpdate:
    """آپدیت ساختگی برای اجرای دستور توسط خود ربات."""

    def __init__(self, guid, author, text):
        self.object_guid = guid
        self.author_guid = author
        self.text = text
        self.message_id = None
        self.reply_message_id = None
        self.is_forward = False
        self.is_group = True
        self.is_private = False
        self.is_me = True


# ------------------------------------------------------------ دستورات
async def handle(ctx) -> bool:
    text = ctx.text
    raw = ctx.raw_text.strip()
    g = ctx.g

    if text == "لیست تایمر":
        if ctx.rank < ranks.SPECIAL:
            return await _deny(ctx)
        items = g.get("timers", [])
        if not items:
            await ctx.reply("⏰ هیچ تایمری تنظیم نشده است.")
            return True
        lines = ["⏰ لیست تایمرها:\n"]
        for i, item in enumerate(items, 1):
            when = (f"ساعت {item['at']}" if item["type"] == "clock"
                    else f"هر {utils.human_duration(item['at'])}")
            limit = (f" — {utils.fa_digits(item['limit'])} بار"
                     if item.get("limit") else " — بی‌نهایت")
            lines.append(f"{utils.fa_digits(i)}. {when}{limit}\n    ↳ {item['cmd']}")
        lines.append("\nحذف: حذف تایمر ۱")
        await ctx.reply("\n".join(lines))
        return True

    m = re.match(r"^حذف تایمر (\d+)$", text)
    if m:
        if ctx.rank < ranks.OWNER:
            return await _deny(ctx, "مالک گروه")
        index = int(m.group(1)) - 1
        items = g.get("timers", [])
        if 0 <= index < len(items):
            removed = items.pop(index)
            db.save("groups")
            await ctx.ok(f"تایمر «{removed['cmd']}» حذف شد.")
        else:
            await ctx.err("شماره‌ی تایمر نامعتبر است.")
        return True

    if text == "پاکسازی لیست تایمر":
        if ctx.rank < ranks.OWNER:
            return await _deny(ctx, "مالک گروه")
        g["timers"] = []
        db.save("groups")
        await ctx.ok("تمام تایمرها حذف شدند.")
        return True

    # ---- ساخت تایمر: «تایمر 23\nگروه بسته»
    m = re.match(r"^تایمر\s+([^\n]+)\n+(.+)$", utils.normalize(raw), re.S)
    if not m:
        return False
    if ctx.rank < ranks.OWNER:
        return await _deny(ctx, "مالک گروه")

    spec = m.group(1).strip()
    command = m.group(2).strip()

    limit = 0
    if "-" in spec:
        spec, _, tail = spec.partition("-")
        spec, tail = spec.strip(), tail.strip()
        if tail.isdigit():
            limit = int(tail)

    clock = re.match(r"^(\d{1,2})(?::(\d{1,2}))?$", spec)
    if clock and int(clock.group(1)) < 24 and not re.search(r"[^\d:]", spec):
        hour = int(clock.group(1))
        minute = int(clock.group(2) or 0)
        item = {"type": "clock", "at": f"{hour:02d}:{minute:02d}",
                "cmd": command, "limit": limit, "count": 0, "last": ""}
        when = f"هر روز ساعت {utils.fa_digits(item['at'])}"
    else:
        seconds = utils.parse_duration(spec)
        if not seconds:
            await ctx.err("زمان تایمر نامعتبر است. مثال: تایمر ۲۳ یا تایمر ۳۰ دقیقه")
            return True
        item = {"type": "interval", "at": seconds, "cmd": command,
                "limit": limit, "count": 0, "last": time.time()}
        when = f"هر {utils.human_duration(seconds)}"

    g.setdefault("timers", []).append(item)
    db.save("groups")
    await ctx.ok(f"تایمر ثبت شد ⏰\n{when}\n↳ {command}" +
                 (f"\nتعداد اجرا: {utils.fa_digits(limit)} بار" if limit else ""))
    return True


# ------------------------------------------------------------ حلقه‌ی اجرا
async def run_timers(dispatch):
    """هر ۲۰ ثانیه تایمرهای سررسیده را اجرا می‌کند."""
    while True:
        try:
            await _tick(dispatch)
        except Exception as exc:
            log.warning("timer scheduler: %s", exc)
            db.log("timers", str(exc))
        await asyncio.sleep(20)


async def _tick(dispatch):
    now = time.time()
    clock = time.strftime("%H:%M")
    today = time.strftime("%Y-%m-%d")
    dirty = False

    for guid, g in list(db.groups().items()):
        if not g.get("active"):
            continue
        for item in list(g.get("timers", [])):
            due = False
            if item["type"] == "clock":
                if item["at"] == clock and item.get("last") != today:
                    item["last"] = today
                    due = True
            else:
                if now - float(item.get("last") or 0) >= float(item["at"]):
                    item["last"] = now
                    due = True

            if not due:
                continue

            item["count"] = item.get("count", 0) + 1
            dirty = True
            author = g.get("owner") if g.get("owner_verified") else None
            try:
                if author:
                    await dispatch(FakeUpdate(guid, author, item["cmd"]), internal=True)
                else:
                    db.log("timers", f"{guid}: owner not verified; timer skipped")
            except Exception as exc:
                db.log("timers", f"{guid}: {exc}")

            if item.get("limit") and item["count"] >= item["limit"]:
                g["timers"].remove(item)

    if dirty:
        db.save("groups")


async def _deny(ctx, who="ادمین ویژه و مالک"):
    await ctx.err(f"این دستور فقط برای {who} قابل اجراست.")
    return True
