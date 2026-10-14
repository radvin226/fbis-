# -*- coding: utf-8 -*-
"""دستورهای زیبا‌سازی پیام: نقل قول، اسپویلر، کارت اعلان و سبک پیام هر گروه."""
from __future__ import annotations

import re

from core import api as api_mod
from core import msgstyle, ranks, utils
from core.db import db

QUOTE_LIMIT = 1500
SPOILER_LIMIT = 1500
ANNOUNCE_TITLE_LIMIT = 80
ANNOUNCE_BODY_LIMIT = 2500

QUOTE_RE = re.compile(r"نقل قول(?:\s+(.+))?", re.S)
SPOILER_RE = re.compile(r"اسپویلر(?:\s+(.+))?", re.S)
# «کارت عنوان [عنوان] | [متن]»؛ عنوان اختیاری است.
ANNOUNCE_HEAD_RE = re.compile(r"کارت عنوان(?=\s|\||$)")
ANNOUNCE_RE = re.compile(r"کارت عنوان(?:\s+(.*?))?\s*\|\s*(.+)", re.S)
STYLE_RE = re.compile(r"سبک پیام(?:\s+(کارتی|ساده))?")

GROUP_PREFIXES = ("نقل قول", "اسپویلر", "کارت عنوان", "سبک پیام")
PRIVATE_PREFIXES = ("نقل قول", "اسپویلر")


def _tail(ctx, head: str, normalized_tail: str) -> str:
    """متن بعد از دستور؛ ترجیحاً از متن اصلی تا ارقام فارسی و نیم‌فاصله‌ها دست‌نخورده بمانند."""
    raw = (ctx.raw_text or "").strip()
    if raw.startswith(head):
        rest = raw[len(head):]
        if not rest or rest[0].isspace():
            return rest.strip()
    return normalized_tail


def _unsafe(text: str) -> bool:
    """Quoted or hidden text must not smuggle links or usernames past the locks."""
    return utils.has_link(text) or utils.has_username(text)


async def _replied(ctx):
    """متن و نام نویسندهٔ پیامی که دستور روی آن ریپلای شده؛ در نبود آن ("", None)."""
    if not ctx.reply_id:
        return "", None
    msg = None
    try:
        res = await ctx.api.get_messages(ctx.guid, [ctx.reply_id])
        rows = api_mod._val(res, "messages", default=[]) or []
        msg = rows[0] if rows else None
    except Exception:
        msg = None
    if msg is None:
        try:
            msg = await ctx.api.get_reply_message(ctx.guid, ctx.reply_id)
        except Exception:
            msg = None
    if msg is None:
        return "", None
    text = api_mod._val(msg, "text", "raw_text", "message", default="") or ""
    return str(text), api_mod.name_of(msg)


async def _quote(ctx) -> bool:
    m = QUOTE_RE.fullmatch(ctx.text)
    if not m:
        return False
    body, author = _tail(ctx, "نقل قول", m.group(1) or "").strip(), None
    if not body:
        body, author = await _replied(ctx)
        body = body.strip()
    if not body:
        await ctx.err("برای نقل قول، متن را بعد از دستور بنویسید یا روی یک پیام متنی ریپلای کنید.")
        return True
    body = msgstyle.clip(body, QUOTE_LIMIT)
    if _unsafe(body):
        await ctx.err("نقل قول پیام‌های حاوی لینک یا یوزرنیم مجاز نیست.")
        return True
    await ctx.api.send_quote(
        ctx.guid, body,
        header=msgstyle.quote_header(author),
        reply_to=ctx.message_id or None,
    )
    return True


async def _spoiler(ctx) -> bool:
    m = SPOILER_RE.fullmatch(ctx.text)
    if not m:
        return False
    hidden, author = _tail(ctx, "اسپویلر", m.group(1) or "").strip(), None
    if not hidden:
        hidden, author = await _replied(ctx)
        hidden = hidden.strip()
    if not hidden:
        await ctx.err("برای اسپویلر، متن را بعد از دستور بنویسید یا روی یک پیام متنی ریپلای کنید.")
        return True
    hidden = msgstyle.clip(hidden, SPOILER_LIMIT)
    if _unsafe(hidden):
        await ctx.err("اسپویلر برای پیام‌های حاوی لینک یا یوزرنیم مجاز نیست.")
        return True
    await ctx.api.send_spoiler(
        ctx.guid, msgstyle.spoiler_prefix(author), hidden,
        reply_to=ctx.message_id or None,
    )
    return True


async def _announce(ctx) -> bool:
    if not ANNOUNCE_HEAD_RE.match(ctx.text):
        return False
    if not ctx.is_group:
        return False
    if ctx.rank < ranks.ADMIN:
        await ctx.err("ساخت کارت اعلان فقط برای ادمین‌های گروه است.")
        return True
    m = ANNOUNCE_RE.fullmatch(ctx.text)
    if not m:
        await ctx.err("فرمت: کارت عنوان [عنوان] | [متن]")
        return True
    title_src, body_src = m.group(1) or "", m.group(2) or ""
    raw = (ctx.raw_text or "").strip()
    if raw.startswith("کارت عنوان") and "|" in raw:
        title_src, _, body_src = raw[len("کارت عنوان"):].partition("|")
    title = msgstyle.clip(title_src.strip(), ANNOUNCE_TITLE_LIMIT) or "اطلاعیه"
    body = msgstyle.clip(body_src.strip(), ANNOUNCE_BODY_LIMIT)
    if not body:
        await ctx.err("فرمت: کارت عنوان [عنوان] | [متن]")
        return True
    await ctx.reply(msgstyle.card(title, body, "📢"), reply=False)
    return True


async def _style(ctx) -> bool:
    m = STYLE_RE.fullmatch(ctx.text)
    if not m or not ctx.is_group:
        return False
    if ctx.rank < ranks.ADMIN:
        await ctx.err("تغییر سبک پیام‌ها فقط برای ادمین‌های گروه است.")
        return True
    choice = m.group(1)
    if choice:
        ctx.g["msg_style"] = msgstyle.MODES[choice]
        db.save("groups")
        await ctx.ok(f"سبک پیام‌های این گروه روی «{choice}» تنظیم شد.")
        return True
    current = msgstyle.MODE_LABEL[msgstyle.mode_of(ctx.g)]
    await ctx.reply(msgstyle.card(
        "سبک پیام",
        f"وضعیت فعلی: {current}\nتغییر به کارتی: سبک پیام کارتی\nتغییر به ساده: سبک پیام ساده",
        "🎨",
    ), reply=False)
    return True


async def handle(ctx) -> bool:
    if not ctx.text.startswith(GROUP_PREFIXES):
        return False
    return (
        await _quote(ctx)
        or await _spoiler(ctx)
        or await _announce(ctx)
        or await _style(ctx)
    )


async def handle_private(ctx) -> bool:
    """در PV فقط نقل قول و اسپویلر فعال است؛ بقیه دستورها گروهی هستند."""
    if not ctx.text.startswith(PRIVATE_PREFIXES):
        return False
    return await _quote(ctx) or await _spoiler(ctx)
