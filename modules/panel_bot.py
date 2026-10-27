# -*- coding: utf-8 -*-
"""پنل مالکان: بات جداگانه روی API رسمی بات سروش‌پلاس.

مالک گروه در PV این بات پیام می‌دهد، گروه‌های خودش را در دکمه‌های شیشه‌ای می‌بیند و
قفل‌ها و لیست بن را مدیریت می‌کند. هر دسترسی دوباره روی داده‌های ذخیره‌شده بررسی می‌شود،
پس دست‌کاری callback_data نمی‌تواند گروه کسی دیگر را باز کند.
"""
from __future__ import annotations

import asyncio
import time

import config
from core import subscription, utils
from core.botapi import BotApi, BotApiError
from core.db import db
from modules.locks import TITLES

LOCK_KEYS = tuple(TITLES.keys())
BAN_LIMIT = 20
TITLE_MAX = 40
BACK_HOME = {"text": "🔙 گروه‌های من", "callback_data": "home"}


def _fa(value):
    return utils.fa_digits(value)


def _is_owner_of(g, uid):
    uid = str(uid or "")
    if not uid:
        return False
    if subscription.is_sudo(uid):
        return True
    return any(g.get(key) is not None and str(g.get(key)) == uid for key in ("owner", "live_owner"))


def owned_groups(uid):
    rows = []
    for gid, g in (db.data.get("groups") or {}).items():
        if _is_owner_of(g, uid):
            rows.append((str(gid), g))
    rows.sort(key=lambda row: str(row[1].get("title") or row[0]).lower())
    return rows


def _title(gid, g):
    return str(g.get("title") or gid)


def _short(text):
    text = str(text)
    return text if len(text) <= TITLE_MAX else text[:TITLE_MAX - 1] + "…"


def home_view(groups):
    refresh = {"text": "🔄 به‌روزرسانی", "callback_data": "home"}
    if not groups:
        return (
            "هیچ گروهی که مالک آن باشید پیدا نشد.\nاگر اشتباه می‌کنید، با سازندهٔ FBI تماس بگیرید.",
            {"inline_keyboard": [[refresh]]},
        )
    rows = [[{"text": _short(_title(gid, g)), "callback_data": f"g:{gid}"}] for gid, g in groups[:80]]
    rows.append([refresh])
    return "🕵️ پنل مدیریت FBI\nگروه‌های شما:", {"inline_keyboard": rows}


def group_view(gid, g):
    locks = g.get("locks") or {}
    bans = g.get("ban_record") or {}
    text = (
        f"⚙️ «{_title(gid, g)}»\n🆔 {gid}\n"
        f"🔒 قفل فعال: {_fa(sum(1 for key in LOCK_KEYS if locks.get(key)))} از {_fa(len(LOCK_KEYS))}\n"
        f"🚫 بن ثبت‌شده: {_fa(len(bans))}"
    )
    markup = {"inline_keyboard": [
        [{"text": "🔒 قفل‌ها", "callback_data": f"L:{gid}"},
         {"text": "🚫 لیست بن", "callback_data": f"B:{gid}"}],
        [BACK_HOME],
    ]}
    return text, markup


def locks_view(gid, g):
    locks = g.get("locks") or {}
    on = sum(1 for key in LOCK_KEYS if locks.get(key))
    text = (
        f"🔒 قفل‌های «{_title(gid, g)}»\n"
        f"قفل فعال: {_fa(on)} از {_fa(len(LOCK_KEYS))}\n"
        "🔴 قفل است • 🟢 باز است\nبرای تغییر، روی هر مورد بزنید."
    )
    rows = []
    for start in range(0, len(LOCK_KEYS), 2):
        row = []
        for idx in range(start, min(start + 2, len(LOCK_KEYS))):
            key = LOCK_KEYS[idx]
            mark = "🔴" if locks.get(key) else "🟢"
            row.append({"text": f"{mark} {TITLES[key]}", "callback_data": f"T:{gid}:{idx}"})
        rows.append(row)
    rows.append([{"text": "🔙 بازگشت", "callback_data": f"g:{gid}"}])
    return text, {"inline_keyboard": rows}


def bans_view(gid, g):
    record = g.get("ban_record") or {}
    ordered = sorted(record.items(), key=lambda item: -float(item[1] or 0))
    lines = [f"🚫 لیست بن «{_title(gid, g)}»", f"تعداد: {_fa(len(record))}"]
    if not record:
        lines.append("فهرست بن خالی است.")
        lines.append("(فقط بن‌هایی نمایش داده می‌شوند که از طریق ربات انجام شده‌اند.)")
    rows = []
    for uid, ts in ordered[:BAN_LIMIT]:
        when = time.strftime("%Y-%m-%d %H:%M", time.localtime(float(ts or 0)))
        lines.append(f"• {uid} — {_fa(when)}")
        rows.append([{"text": f"♻️ رفع بن {uid}", "callback_data": f"U:{gid}:{uid}"}])
    if len(ordered) > BAN_LIMIT:
        lines.append(f"… و {_fa(len(ordered) - BAN_LIMIT)} مورد دیگر")
    rows.append([{"text": "🔙 بازگشت", "callback_data": f"g:{gid}"}])
    return "\n".join(lines), {"inline_keyboard": rows}


class PanelBot:
    def __init__(self, client, splus_api, *, log):
        self.bot = client
        self.splus = splus_api
        self.log = log
        self.offset = None
        self.me = None

    # ------------------------------------------------------------ دسترسی
    def _group_for(self, gid, uid):
        g = (db.data.get("groups") or {}).get(str(gid))
        if g is None or not _is_owner_of(g, uid):
            return None
        return g

    # ------------------------------------------------------------ ارسال
    async def _ack(self, cq_id, text=None, alert=False):
        try:
            await self.bot.answer_callback_query(cq_id, text=text, show_alert=alert)
        except BotApiError as exc:
            self.log.debug("panel ack failed: %s", exc)

    async def _send_or_edit(self, chat_id, message_id, text, markup):
        if message_id:
            try:
                await self.bot.edit_message_text(chat_id, message_id, text, reply_markup=markup)
                return
            except BotApiError as exc:
                self.log.debug("panel edit failed, sending new message: %s", exc)
        await self.bot.send_message(chat_id, text, reply_markup=markup)

    async def _show_home(self, chat_id, uid, message_id=None):
        text, markup = home_view(owned_groups(uid))
        await self._send_or_edit(chat_id, message_id, text, markup)

    # ------------------------------------------------------------ عملیات
    async def _unban(self, gid, g, uid):
        if str(uid) not in (g.get("ban_record") or {}):
            return False
        try:
            await self.splus.unban(gid, uid)
        except Exception as exc:
            self.log.warning("panel unban failed chat=%s user=%s: %s", gid, uid, exc)
            return False
        g.setdefault("ban_record", {}).pop(str(uid), None)
        db.save("groups")
        return True

    # ------------------------------------------------------------ به‌روزرسانی‌ها
    async def handle_message(self, msg):
        chat = msg.get("chat") or {}
        if chat.get("type") != "private":
            return
        uid = str((msg.get("from") or {}).get("id") or "")
        if not uid:
            return
        await self._show_home(chat.get("id") or uid, uid)

    async def handle_callback(self, cq):
        cq_id = cq.get("id")
        frm = cq.get("from") or {}
        uid = str(frm.get("id") or "")
        msg = cq.get("message") or {}
        chat_id = (msg.get("chat") or {}).get("id") or frm.get("id")
        message_id = msg.get("message_id")
        parts = str(cq.get("data") or "").split(":")
        action = parts[0]
        try:
            if action == "home":
                await self._ack(cq_id)
                await self._show_home(chat_id, uid, message_id)
                return
            gid = parts[1] if len(parts) > 1 else ""
            g = self._group_for(gid, uid)
            if g is None:
                await self._ack(cq_id, "این گروه برای شما قابل مدیریت نیست.", alert=True)
                return
            if action == "g":
                await self._ack(cq_id)
                await self._send_or_edit(chat_id, message_id, *group_view(gid, g))
            elif action == "L":
                await self._ack(cq_id)
                await self._send_or_edit(chat_id, message_id, *locks_view(gid, g))
            elif action == "T":
                idx = int(parts[2])
                if not 0 <= idx < len(LOCK_KEYS):
                    raise ValueError("lock index out of range")
                key = LOCK_KEYS[idx]
                locks = g.setdefault("locks", {})
                locks[key] = not bool(locks.get(key))
                db.save("groups")
                await self._ack(cq_id, "ثبت شد ✅")
                await self._send_or_edit(chat_id, message_id, *locks_view(gid, g))
            elif action == "B":
                await self._ack(cq_id)
                await self._send_or_edit(chat_id, message_id, *bans_view(gid, g))
            elif action == "U":
                ok = await self._unban(gid, g, parts[2])
                await self._ack(cq_id, "بن برداشته شد ✅" if ok else "رفع بن انجام نشد.", alert=not ok)
                await self._send_or_edit(chat_id, message_id, *bans_view(gid, g))
            else:
                await self._ack(cq_id, "دستور نامعتبر است.")
        except (IndexError, ValueError):
            await self._ack(cq_id, "دستور نامعتبر است.")

    async def handle_update(self, upd):
        if upd.get("callback_query"):
            await self.handle_callback(upd["callback_query"])
        elif upd.get("message"):
            await self.handle_message(upd["message"])

    async def poll_once(self):
        updates = await self.bot.get_updates(offset=self.offset, timeout=25, limit=100) or []
        for upd in updates:
            if upd.get("update_id") is not None:
                self.offset = int(upd["update_id"]) + 1
            try:
                await self.handle_update(upd)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.log.warning("panel update failed: %s", exc)
        return len(updates)

    async def run(self):
        try:
            self.me = await self.bot.get_me()
            await self.bot.delete_webhook()
        except Exception as exc:
            self.log.warning("panel bot could not start: %s", exc)
            return False
        self.log.info("panel bot started: @%s", (self.me or {}).get("username", "?"))
        backoff = 3
        while True:
            try:
                await self.poll_once()
                backoff = 3
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.log.warning("panel getUpdates failed: %s", exc)
                await asyncio.sleep(backoff)
                backoff = min(60, backoff * 2)


_TASKS = []
RESTART_DELAY = 15


async def _supervise(token, splus_api, log):
    """بات پنل را نگه می‌دارد: اگر غیرمنتظره متوقف شود، بعد از چند ثانیه دوباره راه می‌افتد."""
    while True:
        bot = PanelBot(BotApi(token), splus_api, log=log)
        try:
            if await bot.run() is False:
                return  # توکن یا شروع اولیه مشکل دارد؛ تکرار فایده ندارد
            log.warning("panel bot loop ended unexpectedly; restarting in %ss", RESTART_DELAY)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # فقط نوع خطا ثبت می‌شود تا توکن (اگر در پیام خطا باشد) در لاگ نیفتد.
            log.warning("panel bot crashed (%s); restarting in %ss", type(exc).__name__, RESTART_DELAY)
        await asyncio.sleep(RESTART_DELAY)


def start(splus_api, log):
    token = str(getattr(config, "PANEL_BOT_TOKEN", "") or "").strip()
    if not token:
        log.info("panel bot is disabled (FBI_PANEL_BOT_TOKEN is empty)")
        return None
    task = asyncio.create_task(_supervise(token, splus_api, log))
    _TASKS.append(task)  # نگه داشتن مرجع: بدون آن تسک ممکن است در میانهٔ کار از بین برود
    return task
