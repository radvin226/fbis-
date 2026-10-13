# -*- coding: utf-8 -*-
"""قابلیت‌های پیشرفته FBI: قفل متادیتا، ریپ، ری‌اکت و ابزارهای تکمیلی."""
from __future__ import annotations

import asyncio
import logging
import re
import time
from collections import defaultdict, deque

import config

log = logging.getLogger("fbi.advanced")
from core import api as api_mod, ranks, utils
from core.db import db


REACTION_ALIASES = {
    "❤️": "❤️", "❤": "❤", "🧡": "🧡", "💛": "💛", "💚": "💚", "💙": "💙",
    "💜": "💜", "🖤": "🖤", "🤍": "🤍", "🤎": "🤎", "👍": "👍", "👎": "👎",
    "😂": "😂", "🤣": "🤣", "😎": "😎", "😍": "😍", "🤔": "🤔", "😢": "😢", "🫠": "🫠",
    "😭": "😭", "😡": "😡", "😮": "😮", "🔥": "🔥", "👏": "👏", "🙏": "🙏",
    "✅": "✅", "❌": "❌", "💯": "💯", "🎉": "🎉", "🤩": "🤩", "😐": "😐",
}


def _has_role(ctx, level=ranks.SPECIAL):
    if ctx.rank >= level:
        return True
    return False


def _bucket(g):
    return g.setdefault("advanced_replies", [])


def _reply_rules(g):
    return g.setdefault("reply_rules", [])


def _quick_replies(g):
    return g.setdefault("quick_replies", {})


def _normalize_trigger(text):
    return utils.normalize(str(text or "")).strip().lower()


def _metadata_snapshot(info):
    return {
        "title": str(api_mod.deep(info, "group", "group_title", default=None)
                      or api_mod.deep(info, "chat", "title", default=None) or ""),
        "description": str(api_mod.deep(info, "group", "description", default=None)
                           or api_mod.deep(info, "chat", "description", default=None) or ""),
        "username": str(api_mod.deep(info, "group", "username", default=None)
                         or api_mod.deep(info, "chat", "username", default=None) or ""),
    }


async def _get_snapshot(ctx):
    try:
        info = await ctx.api.group_info(ctx.guid)
        return _metadata_snapshot(info)
    except Exception:
        return None


async def set_metadata_lock(ctx, enabled: bool):
    if enabled:
        snap = await _get_snapshot(ctx)
        if snap is None:
            await ctx.err("اطلاعات متادیتای گروه از سرور دریافت نشد؛ قفل متادیتا فعال نشد.")
            return True
        ctx.g["metadata_lock_snapshot"] = snap
        ctx.g["metadata_lock"] = True
        ctx.g["metadata_last_check"] = time.time()
        db.save("groups")
        await ctx.ok("قفل متادیتا فعال شد؛ نام، توضیحات و آیدی عمومی گروه از این به بعد محافظت می‌شوند.")
        return True
    ctx.g["metadata_lock"] = False
    db.save("groups")
    await ctx.ok("قفل متادیتا غیرفعال شد.")
    return True


async def enforce_metadata_lock(guid, g, api):
    if not g.get("active") or not g.get("metadata_lock"):
        return False
    snap = g.get("metadata_lock_snapshot") or {}
    if not snap:
        return False
    try:
        info = await api.group_info(guid)
    except Exception:
        return False
    current = _metadata_snapshot(info)
    changed = False
    if snap.get("title") and current.get("title") != snap.get("title"):
        try:
            await api.call("set_chat_title", api._chat_arg(guid), snap["title"])
            changed = True
        except Exception:
            pass
    if current.get("description") != snap.get("description"):
        try:
            await api.call("set_chat_description", api._chat_arg(guid), snap.get("description", ""))
            changed = True
        except Exception:
            pass
    if current.get("username") != snap.get("username"):
        try:
            await api.call("set_chat_username", api._chat_arg(guid), snap.get("username", ""))
            changed = True
        except Exception:
            pass
    if changed:
        g["metadata_last_restore"] = time.time()
        db.save("groups")
    g["metadata_last_check"] = time.time()
    return changed


def _extract_reaction_emoji(text):
    # Keep the complete emoji sequence after the command; do not split ZWJ
    # or variation selectors, because SPLUS expects the exact Unicode emoji.
    value = str(text or "").strip()
    return value.replace("️", "️")


async def _reply_message_id(ctx):
    value = getattr(ctx, "reply_id", None)
    if value is not None and str(value).strip():
        return value
    original = getattr(getattr(ctx, "update", None), "original_update", None)
    for key in ("reply_to_msg_id", "reply_to_message_id", "reply_message_id"):
        try:
            val = getattr(original, key)
        except Exception:
            val = None
        if val is not None:
            return val
    for key in ("reply_to", "reply", "reply_info", "reply_to_message"):
        try:
            obj = getattr(original, key)
        except Exception:
            obj = None
        if obj is None:
            continue
        for nested in ("reply_to_msg_id", "reply_to_message_id", "msg_id", "message_id", "id"):
            try:
                val = getattr(obj, nested)
            except Exception:
                val = None
            if val is not None:
                return val
    return None


async def _apply_reaction(ctx, reaction, remove=False):
    message_id = await _reply_message_id(ctx)
    if message_id is None:
        await ctx.err("روی پیام موردنظر ریپلای بزنید. مثال: ری اکت 🫠")
        return True

    reaction = _extract_reaction_emoji(reaction)
    if not reaction:
        await ctx.err("اموجی ری‌اکت را وارد کنید. مثال: ری اکت 🫠")
        return True

    # The public SplusLib API explicitly documents react_message(chat_id,
    # message_id, reaction). Use that first; if a particular installed build
    # does not expose the wrapper, transparently use its underlying client.
    value = None if remove else reaction
    client = getattr(ctx.api, "client", None)
    errors = []
    try:
        public = getattr(ctx.api, "react", None)
        if callable(public):
            result = await public(ctx.guid, message_id, value)
        else:
            result = await ctx.api.call("react_message", ctx.api._chat_arg(ctx.guid),
                                        api_mod._as_int(message_id, message_id), value)
        if result is not None or public is not None:
            await ctx.ok(("ری‌اکت حذف شد." if remove else f"ری‌اکت {reaction} ارسال شد."))
            return True
    except Exception as exc:
        errors.append(exc)

    raw = getattr(client, "_client", None) if client is not None else None
    raw_method = getattr(raw, "react_message", None) if raw is not None else None
    if callable(raw_method):
        try:
            result = raw_method(ctx.api._chat_arg(ctx.guid), api_mod._as_int(message_id, message_id), value)
            if hasattr(result, "__await__"):
                result = await result
            await ctx.ok(("ری‌اکت حذف شد." if remove else f"ری‌اکت {reaction} ارسال شد."))
            return True
        except Exception as exc:
            errors.append(exc)

    detail = errors[-1] if errors else "unknown error"
    await ctx.err(f"ارسال ری‌اکت ناموفق بود: {type(detail).__name__}: {detail}")
    return True


async def _add_anchor_reply(ctx, response):
    message_id = await _reply_message_id(ctx)
    # IMPORTANT: in some SplusLib updates ``reply_sender_id`` can describe the
    # sender of the current update/bot envelope rather than the original
    # replied-to message. For anchor replies the source of truth must therefore
    # be the actual replied message object. Only use reply_sender_id as a final
    # fallback after excluding the current user and FBI itself.
    target = None

    # Resolve the exact replied-to message first. This is more reliable than
    # relying on reply_sender_id, and prevents rules from accidentally binding
    # to FBI BOT.
    if message_id:
        original = getattr(getattr(ctx, "update", None), "original_update", None)
        getter = getattr(original, "get_reply_message", None) if original is not None else None
        if callable(getter):
            try:
                replied = getter()
                if hasattr(replied, "__await__"):
                    replied = await replied
                target = api_mod.sender_guid_of(replied)
                if replied is not None:
                    try:
                        await ctx.api.cache_reply_message(ctx.guid, message_id, replied)
                    except Exception:
                        pass
            except Exception as exc:
                log.debug("anchor reply getter failed: %s", exc)

    if message_id and not target:
        try:
            replied = await ctx.api.get_reply_message(ctx.guid, message_id)
            target = api_mod.sender_guid_of(replied) if replied is not None else None
        except Exception:
            target = None

    if message_id and not target:
        try:
            target = await ctx.target()
        except Exception:
            target = None

    # Last-resort metadata fallback, but never bind a rule to FBI BOT/current
    # sender. The replied message lookup above always has priority.
    if message_id and not target:
        candidate = getattr(ctx, "reply_sender_id", None)
        forbidden = {str(getattr(ctx, "me", "")), str(getattr(config, "BOT_GUID", ""))}
        if candidate is not None and str(candidate) not in forbidden:
            target = str(candidate)

    if not message_id or not target:
        await ctx.err("روی پیام همان کاربر ریپلای بزنید و دقیقاً بنویسید: ریپ سگتم")
        return True
    response = str(response or "").strip()
    if not response:
        await ctx.err("متن پاسخ خالی است. مثال: ریپ سگتم")
        return True
    target = str(target)
    rules = _reply_rules(ctx.g)
    rules[:] = [r for r in rules if not (str(r.get("message_id")) == str(message_id) and str(r.get("user_id")) == target)]
    rules.append({
        "message_id": str(message_id),
        "user_id": target,
        "response": response[:1000],
        "created_at": time.time(),
        "creator": str(ctx.author or ""),
    })
    if len(rules) > 500:
        del rules[:-500]
    db.save("groups")
    await ctx.ok(f"ریپ ثبت شد؛ اگر {await ctx.target_link(target)} به همان پیام ریپلای بزند، پاسخ «{response[:80]}» ارسال می‌شود.", mention=target)
    return True


async def _add_text_reply(ctx, trigger, response):
    trigger = _normalize_trigger(trigger)
    response = str(response or "").strip()
    if not trigger or not response:
        await ctx.err("فرمت: ریپ متن => پاسخ")
        return True
    rules = _bucket(ctx.g)
    rules[:] = [r for r in rules if _normalize_trigger(r.get("trigger")) != trigger]
    rules.append({"trigger": trigger, "response": response[:1000], "created_at": time.time(), "creator": str(ctx.author or "")})
    if len(rules) > 1000:
        del rules[:-1000]
    db.save("groups")
    await ctx.ok(f"ریپ متنی برای «{trigger}» ثبت شد.")
    return True


def _same_identity(left, right):
    """Compare SPLUS user ids robustly across int/string/object-shaped updates."""
    if left is None or right is None:
        return False
    a = str(left).strip()
    b = str(right).strip()
    if not a or not b:
        return False
    if a == b:
        return True
    try:
        return int(a) == int(b)
    except Exception:
        return False


async def fire_reply_rules(ctx):
    if not ctx.is_group or not ctx.author:
        return False

    # Do not trust only Context.reply_id: some SplusLib builds expose the
    # reply reference on the raw Message as reply_to_msg_id / reply_to.
    # Resolve the exact current replied-to message first. This fixes the case
    # where a user really replies to the anchored message but the normalized
    # Context lost the reply id.
    current_reply_id = await _reply_message_id(ctx)
    if current_reply_id is None:
        try:
            original = getattr(getattr(ctx, "update", None), "original_update", None)
            getter = getattr(original, "get_reply_message", None) if original is not None else None
            if callable(getter):
                replied = getter()
                if hasattr(replied, "__await__"):
                    replied = await replied
                if replied is not None:
                    current_reply_id = getattr(replied, "id", None) or getattr(replied, "message_id", None)
        except Exception as exc:
            log.debug("reply trigger resolver failed: %s", exc)

    if current_reply_id is not None:
        for rule in list(_reply_rules(ctx.g)):
            if not _same_identity(rule.get("message_id"), current_reply_id):
                continue
            # The response is restricted to the exact user that was the target
            # when the rule was created. Never fire for another replier.
            if not _same_identity(rule.get("user_id"), ctx.author):
                continue
            response = str(rule.get("response", "") or "").strip()
            if not response:
                return True
            try:
                # Reply DIRECTLY on the original anchored Message object.
                # This is stronger than forwarding reply_to through a generic
                # send wrapper because SplusLib's Message.reply() itself binds
                # the new message to that exact message.
                sent = False
                original = getattr(getattr(ctx, "update", None), "original_update", None)
                getter = getattr(original, "get_reply_message", None) if original is not None else None
                if callable(getter):
                    anchor = getter()
                    if hasattr(anchor, "__await__"):
                        anchor = await anchor
                    anchor_reply = getattr(anchor, "reply", None) if anchor is not None else None
                    if callable(anchor_reply):
                        result = anchor_reply(response)
                        if hasattr(result, "__await__"):
                            await result
                        sent = True

                # Compatibility fallback for builds whose Message object does
                # not expose .reply(). Keep the stored anchor id explicit.
                if not sent:
                    await ctx.api.send(ctx.guid, response, reply_to=current_reply_id)
            except Exception as exc:
                log.debug("anchor reply send failed: %s", exc)
            return True

    if ctx.raw_text:
        trigger = _normalize_trigger(ctx.raw_text)
        for rule in list(_bucket(ctx.g)):
            if _normalize_trigger(rule.get("trigger")) == trigger:
                try:
                    await ctx.reply(rule.get("response", ""), reply=True)
                except Exception as exc:
                    log.debug("text reply send failed: %s", exc)
                return True
    return False


async def moderate(ctx):
    """پیش‌چک ابزارهای ضدتکرار/ضدفlood/slow mode و قفل ویرایش."""
    if not ctx.is_group or not ctx.author or ctx.rank > ranks.USER:
        return False
    g = ctx.g
    now = time.time()
    aid = str(ctx.author)

    if getattr(ctx, "is_edited", False) and g.get("advanced", {}).get("edited_lock"):
        try:
            await ctx.api.delete(ctx.guid, [ctx.message_id])
        except Exception:
            pass
        return True
    if getattr(ctx, "is_edited", False) and g.get("advanced", {}).get("edited_warn"):
        try:
            await ctx.api.send(ctx.guid, f"⚠️ {await ctx.target_link(ctx.author)} پیام خود را ویرایش کرد.", reply_to=ctx.message_id)
        except Exception:
            pass
        return False

    if g.get("advanced", {}).get("duplicate_lock") and ctx.raw_text:
        last = g.setdefault("advanced", {}).setdefault("last_messages", {})
        key = aid
        prev = last.get(key) or {}
        if _normalize_trigger(prev.get("text")) == _normalize_trigger(ctx.raw_text) and now - float(prev.get("at") or 0) <= 20:
            await ctx.api.delete(ctx.guid, [ctx.message_id])
            return True
        last[key] = {"text": ctx.raw_text[:500], "at": now}

    adv = g.setdefault("advanced", {})
    hist = adv.setdefault("flood", {})
    arr = hist.setdefault(aid, [])
    arr.append(now)
    cutoff = now - max(2, int(adv.get("flood_window", 8)))
    arr[:] = [x for x in arr if x >= cutoff]
    if adv.get("flood_lock") and len(arr) > max(2, int(adv.get("flood_threshold", 5))):
        try:
            await ctx.api.delete(ctx.guid, [ctx.message_id])
        except Exception:
            pass
        return True
    slow = int(adv.get("slow_mode_seconds", 0) or 0)
    if slow > 0:
        last_at = float(adv.setdefault("slow_last", {}).get(aid) or 0)
        if last_at and now - last_at < slow:
            try:
                await ctx.api.delete(ctx.guid, [ctx.message_id])
            except Exception:
                pass
            return True
        adv.setdefault("slow_last", {})[aid] = now
    # Keep state bounded.
    if len(arr) > 50:
        del arr[:-50]
    return False


async def handle(ctx) -> bool:
    text = ctx.text
    adv = ctx.g.setdefault("advanced", {})

    if text in ("قفل متادیتا", "متادیتا قفل"):
        if not _has_role(ctx): return await ctx.err("فقط ادمین ویژه و مالک.") or True
        return await set_metadata_lock(ctx, True)
    if text in ("باز کردن متادیتا", "متادیتا باز", "حذف قفل متادیتا"):
        if not _has_role(ctx): return await ctx.err("فقط ادمین ویژه و مالک.") or True
        return await set_metadata_lock(ctx, False)
    if text in ("وضعیت متادیتا", "متادیتا"):
        snap = ctx.g.get("metadata_lock_snapshot") or {}
        await ctx.reply(
            "🔒 قفل متادیتا: " + ("فعال ✅" if ctx.g.get("metadata_lock") else "خاموش ❌") +
            f"\n🏷 نام: {snap.get('title') or '—'}\n📝 توضیحات: {'ذخیره شده' if 'description' in snap else '—'}\n🔗 آیدی عمومی: {snap.get('username') or 'خصوصی'}"
        )
        return True
    if text in ("قفل نام گروه روشن", "قفل عنوان روشن", "قفل نام گروه"):
        if not _has_role(ctx): return await ctx.err("فقط ادمین ویژه و مالک.") or True
        adv["metadata_title_lock"] = True
        db.save("groups")
        await ctx.ok("قفل نام گروه فعال شد.")
        return True
    if text in ("قفل نام گروه خاموش", "قفل عنوان خاموش", "باز کردن قفل نام گروه"):
        if not _has_role(ctx): return await ctx.err("فقط ادمین ویژه و مالک.") or True
        adv["metadata_title_lock"] = False
        db.save("groups")
        await ctx.ok("قفل نام گروه خاموش شد.")
        return True

    if (text.startswith("ری‌اکت ") or text.startswith("ریاکت ") or text.startswith("ری اکت ")
            or text.startswith("ری‌اکت") or text.startswith("ریاکت") or text.startswith("ری اکت")
            or text.startswith("ریپ‌ری‌اکت ")):
        # Reacting to a message is a normal messaging action, not an admin
        # moderation action; every group member may use it.
        raw = (text.replace("ریپ‌ری‌اکت", "", 1)
                 .replace("ری‌اکت", "", 1)
                 .replace("ریاکت", "", 1)
                 .replace("ری اکت", "", 1).strip())
        return await _apply_reaction(ctx, raw, remove=False)
    if (text.startswith("حذف ری‌اکت ") or text.startswith("حذف ریاکت ") or
            text.startswith("حذف ری اکت ")):
        raw = re.sub(r"^(حذف ری‌اکت|حذف ریاکت|حذف ری اکت)\s*", "", text).strip()
        return await _apply_reaction(ctx, raw, remove=True)

    # ریپ روی پیام: `ریپ پاسخ خودکار`.
    if (ctx.reply_id and re.fullmatch(r"ریپ(?:پ)?\s+.+", text, re.S)):
        if ctx.rank < ranks.SPECIAL:
            return await ctx.err("فقط ادمین ویژه و مالک.") or True
        payload = re.sub(r"^ریپ(?:پ)?\s+", "", text, count=1).strip()
        return await _add_anchor_reply(ctx, payload)

    m = re.fullmatch(r"ریپ\s+(.+?)\s*=>\s*(.+)", text, re.S)
    if m:
        if ctx.rank < ranks.SPECIAL:
            return await ctx.err("فقط ادمین ویژه و مالک.") or True
        return await _add_text_reply(ctx, m.group(1), m.group(2))

    m = re.fullmatch(r"حذف ریپ\s+(.+)", text, re.S)
    if m:
        if ctx.rank < ranks.SPECIAL:
            return await ctx.err("فقط ادمین ویژه و مالک.") or True
        trigger = _normalize_trigger(m.group(1))
        before = len(_bucket(ctx.g))
        ctx.g["advanced_replies"] = [r for r in _bucket(ctx.g) if _normalize_trigger(r.get("trigger")) != trigger]
        db.save("groups")
        await ctx.ok("ریپ حذف شد." if before != len(ctx.g["advanced_replies"]) else "ریپی با این متن پیدا نشد.")
        return True

    if text in ("لیست ریپ", "ریپ ها", "ریپها"):
        rows = []
        for r in _bucket(ctx.g)[-50:]:
            rows.append(f"• {r.get('trigger')} → {r.get('response')}")
        for r in _reply_rules(ctx.g)[-50:]:
            rows.append(f"• ریپلای {r.get('message_id')} → {r.get('response')}")
        await ctx.reply("🧩 لیست ریپ‌ها:\n" + ("\n".join(rows) if rows else "هنوز ریپی ثبت نشده."))
        return True

    m = re.fullmatch(r"پاسخ سریع\s+([^=|]+?)\s*=>\s*(.+)", text, re.S)
    if m:
        if ctx.rank < ranks.SPECIAL:
            return await ctx.err("فقط ادمین ویژه و مالک.") or True
        key = _normalize_trigger(m.group(1))
        value = m.group(2).strip()[:1000]
        _quick_replies(ctx.g)[key] = value
        db.save("groups")
        await ctx.ok(f"پاسخ سریع «{key}» ثبت شد.")
        return True

    m = re.fullmatch(r"حذف پاسخ سریع\s+(.+)", text, re.S)
    if m:
        if ctx.rank < ranks.SPECIAL:
            return await ctx.err("فقط ادمین ویژه و مالک.") or True
        key = _normalize_trigger(m.group(1))
        existed = key in _quick_replies(ctx.g)
        _quick_replies(ctx.g).pop(key, None)
        db.save("groups")
        await ctx.ok("پاسخ سریع حذف شد." if existed else "پاسخ سریعی با این نام پیدا نشد.")
        return True

    if text in ("لیست پاسخ سریع", "پاسخ های سریع", "پاسخ‌های سریع"):
        items = _quick_replies(ctx.g)
        lines = [f"• {k} → {v}" for k, v in list(items.items())[:100]]
        await ctx.reply("⚡ پاسخ‌های سریع:\n" + ("\n".join(lines) if lines else "چیزی ثبت نشده."))
        return True

    if text.startswith("پاسخ سریع "):
        key = _normalize_trigger(text[len("پاسخ سریع "):])
        value = _quick_replies(ctx.g).get(key)
        if value is not None:
            await ctx.reply(value)
            return True

    toggles = {
        "ضدتکرار روشن": ("duplicate_lock", True), "ضدتکرار خاموش": ("duplicate_lock", False),
        "ضدفلود روشن": ("flood_lock", True), "ضدفلود خاموش": ("flood_lock", False),
        "قفل ویرایش روشن": ("edited_lock", True), "قفل ویرایش خاموش": ("edited_lock", False),
        "هشدار ویرایش روشن": ("edited_warn", True), "هشدار ویرایش خاموش": ("edited_warn", False),
    }
    if text in toggles:
        if ctx.rank < ranks.OWNER:
            return await ctx.err("فقط مالک گروه.") or True
        key, val = toggles[text]
        adv[key] = val
        db.save("groups")
        await ctx.ok(f"{text.split()[0]} {'فعال' if val else 'خاموش'} شد.")
        return True

    m = re.fullmatch(r"تنظیم ضدفلود (\d+)\s+(\d+)", text)
    if m:
        if ctx.rank < ranks.OWNER: return await ctx.err("فقط مالک گروه.") or True
        adv["flood_threshold"] = max(3, min(30, int(m.group(1))))
        adv["flood_window"] = max(3, min(60, int(m.group(2))))
        db.save("groups")
        await ctx.ok(f"ضدفلود: {adv['flood_threshold']} پیام در {adv['flood_window']} ثانیه.")
        return True

    m = re.fullmatch(r"اسلومود (\d+)", text)
    if m:
        if ctx.rank < ranks.OWNER: return await ctx.err("فقط مالک گروه.") or True
        adv["slow_mode_seconds"] = max(0, min(3600, int(m.group(1))))
        adv["slow_last"] = {}
        db.save("groups")
        await ctx.ok("اسلومود " + ("خاموش شد." if adv["slow_mode_seconds"] == 0 else f"روی {adv['slow_mode_seconds']} ثانیه تنظیم شد."))
        return True

    if text == "آمار پیشرفته":
        await ctx.reply(
            "🧰 ابزارهای پیشرفته\n"
            f"🔒 متادیتا: {'فعال' if ctx.g.get('metadata_lock') else 'خاموش'}\n"
            f"🧩 ریپ متنی: {len(_bucket(ctx.g))}\n"
            f"↩️ ریپ روی پیام: {len(_reply_rules(ctx.g))}\n"
            f"🚫 ضدتکرار: {'فعال' if adv.get('duplicate_lock') else 'خاموش'}\n"
            f"🌊 ضدفلود: {'فعال' if adv.get('flood_lock') else 'خاموش'}\n"
            f"✏️ قفل ویرایش: {'فعال' if adv.get('edited_lock') else 'خاموش'}\n"
            f"⚠️ هشدار ویرایش: {'فعال' if adv.get('edited_warn') else 'خاموش'}\n"
            f"⚡ پاسخ سریع: {len(_quick_replies(ctx.g))}\n"
            f"🐢 اسلومود: {adv.get('slow_mode_seconds', 0)} ثانیه"
        )
        return True

    return False


async def on_chat_action(event, api, me_guid):
    chat_id = api_mod._val(event, "chat_id", "object_guid", "peer_id", default=None)
    if chat_id is None:
        return False
    guid = str(chat_id)
    g = db.group(guid)
    if not g.get("active") or not g.get("metadata_lock"):
        return False
    # Chat-action events are the cheapest signal that metadata may have changed.
    try:
        return await enforce_metadata_lock(guid, g, api)
    except Exception:
        return False
