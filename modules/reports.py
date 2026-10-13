# -*- coding: utf-8 -*-
"""گزارش پیام برای کاربران عادی و رسیدگی ادمین با ریپلای روی گزارش."""
from __future__ import annotations

import time

import config
from core import api as api_mod, ranks, utils
from core.db import db

REPORT_ACTIONS = {
    "دیدم": "seen",
    "مشاهده شد": "seen",
    "سکوت": "mute",
    "بن": "ban",
    "حذف": "delete",
    "اخطار": "warn",
    "لغو گزارش": "ignore",
    "بی‌خیال": "ignore",
    "بیخیال": "ignore",
}


def _reports(g):
    value = g.setdefault("reports", {})
    return value if isinstance(value, dict) else {}


def _reported_text(message):
    if message is None:
        return ""
    return str(api_mod._val(message, "raw_text", "message", "text", "caption", default="") or "").strip()


def _find_message(rows, message_id):
    wanted = str(message_id)
    for row in rows or []:
        mid = api_mod._val(row, "id", "message_id", "msg_id", default=None)
        if mid is not None and str(mid) == wanted:
            return row
    return None


async def _load_replied_message(ctx):
    if not ctx.reply_id:
        return None
    try:
        result = await ctx.api.get_messages(ctx.guid, [ctx.reply_id])
        rows = api_mod.members_of(result) or api_mod._val(result, "messages", default=[]) or []
        return _find_message(rows, ctx.reply_id) or (rows[0] if rows else None)
    except Exception:
        return None


async def _target_from_report(ctx):
    item = _reports(ctx.g).get(str(ctx.reply_id)) if ctx.reply_id else None
    if not isinstance(item, dict):
        return None
    target = str(item.get("target_guid") or "")
    if not target:
        return None
    return target, item


async def _save_status(ctx, report_id, status, actor=None):
    item = _reports(ctx.g).get(str(report_id))
    if not isinstance(item, dict):
        return
    item["status"] = status
    item["handled_at"] = time.time()
    if actor:
        item["handled_by"] = str(actor)
    db.save("groups")


def _report_header(kind):
    return f"🚨 گزارش {'پیام لینک‌دار' if kind == 'link' else 'پیام'}"


async def create_report(ctx) -> bool:
    if ctx.rank != ranks.USER:
        return False
    if ctx.text != "گزارش":
        return False
    if not ctx.reply_id:
        await ctx.err("برای گزارش، روی پیام کاربر ریپلای بزنید و بنویسید «گزارش».")
        return True

    message = await _load_replied_message(ctx)
    target = ctx.reply_sender_id or (api_mod.sender_guid_of(message) if message is not None else None)
    if not target or str(target) == str(ctx.me):
        await ctx.err("صاحب پیام گزارش‌شده پیدا نشد.")
        return True
    target = str(target)
    report_id = str(time.time_ns())
    body = _reported_text(message)
    if not body:
        body = "[پیام بدون متن / رسانه]"
    body = body[:1200]
    kind = "link" if utils.has_link(body) else "message"

    reporter_link = await ctx.target_link(ctx.author)
    target_link = await ctx.target_link(target)
    report_text = (
        f"{_report_header(kind)}\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"👤 گزارش‌دهنده: {reporter_link}\n"
        f"🎯 کاربر: {target_link}\n"
        f"🆔 آیدی: `{target}`\n\n"
        "💬 متن پیام:\n"
        f"> {body.replace(chr(10), chr(10) + '> ')[:1800]}\n\n"
        "👑 ادمین گرامی، روی همین پیام ریپلای کنید و یکی از این‌ها را بفرستید:\n"
        "`دیدم` • `حذف` • `اخطار` • `سکوت` • `بن` • `لغو گزارش`"
    )
    result = await ctx.reply(report_text, reply=False)
    message_id = ctx.api.result_message_id(result)
    if not message_id:
        await ctx.err("گزارش ساخته نشد؛ پیام گزارش قابل ثبت نیست.")
        return True

    _reports(ctx.g)[str(message_id)] = {
        "target_guid": target,
        "target_message_id": str(ctx.reply_id),
        "reporter_guid": str(ctx.author),
        "created_at": time.time(),
        "status": "pending",
        "kind": kind,
    }
    # فقط ۲۰۰ گزارش آخر هر گروه نگه داشته می‌شود.
    items = list(_reports(ctx.g).items())
    if len(items) > 200:
        _reports(ctx.g).clear()
        _reports(ctx.g).update(items[-200:])
    db.save("groups")
    return True


async def _apply_mute(ctx, target):
    reason = await ctx.protection_reason(target)
    if reason:
        return False, reason
    minutes = getattr(config, "REPORT_MUTE_MINUTES", None)
    g = ctx.g
    now = time.time()
    seconds = int(minutes) * 60 if minutes else 0
    g.setdefault("mute", {})[target] = now + seconds if seconds else 0
    g.setdefault("mute_started", {})[target] = now
    g.setdefault("mute_messages", {})[target] = []
    g.setdefault("mute_reason", {})[target] = "report"
    server_ok = False
    try:
        result = await ctx.api.mute_member(ctx.guid, target, minutes=minutes)
        server_ok = bool(result and (result.get("ok") if isinstance(result, dict) else True))
    except Exception:
        server_ok = False
    g.setdefault("mute_server_applied", {})[target] = bool(server_ok)
    g.setdefault("mute_permission_last", {})[target] = time.time()
    db.save("groups")
    return True, server_ok


async def _apply_warn(ctx, target):
    reason = await ctx.protection_reason(target)
    if reason:
        return False, reason
    warns = ctx.g.setdefault("warns", {})
    warns[target] = int(warns.get(target, 0)) + 1
    db.bump(ctx.guid, target, "warn", 1)
    db.save("groups")
    return True, warns[target]


async def handle_report_action(ctx) -> bool:
    if not ctx.is_group or ctx.rank < ranks.ADMIN or not ctx.reply_id:
        return False
    action = REPORT_ACTIONS.get(ctx.text)
    if not action:
        return False
    loaded = await _target_from_report(ctx)
    if not loaded:
        return False
    target, item = loaded
    name = await ctx.target_link(target)

    if action == "seen":
        await _save_status(ctx, ctx.reply_id, "seen", ctx.author)
        await ctx.reply("✅ گزارش دیده شد.")
        return True

    if action == "ignore":
        await _save_status(ctx, ctx.reply_id, "ignored", ctx.author)
        await ctx.reply("☑️ گزارش لغو شد.")
        return True

    if action in {"mute", "ban", "delete", "warn"}:
        protection = await ctx.protection_reason(target)
        if protection:
            await ctx.err(protection)
            await _save_status(ctx, ctx.reply_id, "protected", ctx.author)
            return True

    if action == "mute":
        ok, detail = await _apply_mute(ctx, target)
        if not ok:
            await ctx.err(str(detail))
            return True
        await _save_status(ctx, ctx.reply_id, "muted", ctx.author)
        suffix = "" if detail is True else "\n⚠️ سکوت سروری تأیید نشد."
        await ctx.ok(f"{name} به‌دلیل گزارش سکوت شد. 🔇{suffix}", mention=target)
        return True

    if action == "ban":
        try:
            await ctx.api.ban(ctx.guid, target)
        except Exception as exc:
            await ctx.err(f"بن انجام نشد: {type(exc).__name__}")
            return True
        await _save_status(ctx, ctx.reply_id, "banned", ctx.author)
        await ctx.ok(f"{name} طبق گزارش بن شد. 🚫", mention=target)
        return True

    if action == "delete":
        try:
            await ctx.api.delete(ctx.guid, [item.get("target_message_id")])
        except Exception:
            await ctx.err("حذف پیام انجام نشد.")
            return True
        await _save_status(ctx, ctx.reply_id, "deleted", ctx.author)
        await ctx.ok(f"پیام {name} حذف شد. 🗑️", mention=target)
        return True

    if action == "warn":
        ok, detail = await _apply_warn(ctx, target)
        if not ok:
            await ctx.err(str(detail))
            return True
        await _save_status(ctx, ctx.reply_id, "warned", ctx.author)
        await ctx.ok(f"{name} اخطار گرفت. ⚠️ تعداد اخطار: {utils.fa_digits(detail)}", mention=target)
        return True

    return False


async def handle(ctx) -> bool:
    # اول رسیدگی ادمین به یک گزارش ثبت‌شده.
    if await handle_report_action(ctx):
        return True
    return await create_report(ctx)
