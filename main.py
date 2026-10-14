# -*- coding: utf-8 -*-
"""FBI SPLUS 4.0 - SplusLib user-account group manager."""
from __future__ import annotations

import asyncio
import logging
import os
import re
import sys
import time
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import config
from core import api as api_mod
from core import msgstyle, ranks, splash, utils, subscription
from core.api import API
from core.context import Ctx, make_update
from core.db import db
from modules import admin, beauty, fun, group_tools, lists, locks, media, misc, stats, timers, v2_features, sassy_ai, guide, reports, advanced
import webpanel

try:
    from spluslib import SplusClient
except ImportError as exc:
    raise SystemExit("spluslib نصب نیست؛ ابتدا requirements.txt را نصب کنید.") from exc

config.ensure_dirs()
os.environ["PATH"] = config.FFMPEG_BIN_DIR + os.pathsep + os.environ.get("PATH", "")

LOG_FILE = os.path.join(config.BASE_DIR, "fbi.log")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
    handlers=[logging.StreamHandler(sys.stdout), logging.FileHandler(LOG_FILE, encoding="utf-8")],
)
log = logging.getLogger("fbi")

app = SplusClient(config.SESSION_NAME, device_model=config.DEVICE_MODEL)
api = API(app)
ME = {"guid": None, "name": config.BOT_NAME}
MODULES = (beauty, reports, admin, locks, guide, lists, group_tools, stats, timers, fun, media, v2_features, sassy_ai, advanced, misc)
_ADMIN_SYNC = {}
_ADMIN_SYNC_TTL = 15.0
_SUDO_PROMOTE_CACHE = {}
_LAST_CALL_SCAN = 0.0


async def _ensure_group_open_state(guid, g):
    until = float(g.get("closed_until") or 0)
    if not until:
        return False
    now = time.time()
    if until > now:
        return True
    try:
        ok = await api.open_group_permissions(guid)
    except Exception:
        ok = False
    if ok:
        g["closed_until"] = 0
        g["closed"] = False
        g["wave_lock_notified_at"] = 0
        g["opened_at"] = now
        db.save("groups")
        return False
    # Server is still closed if the restore operation failed; keep the local
    # state closed so moderation does not assume the group is open.
    g["closed"] = True
    db.save("groups")
    return True


async def repair_saved_mute_permissions_on_boot():
    """Immediately rebuild all saved per-user mute permissions from the DB.

    The DB is the source of truth after a restart. For every active group with
    saved mute entries, the server-side permission is re-applied directly; this
    does not ban users and does not wait for the periodic restore loop.
    """
    repaired = 0
    checked_groups = 0
    for guid, g in list(db.data.get("groups", {}).items()):
        if not g.get("active") or not g.get("mute"):
            continue
        checked_groups += 1
        fake = Ctx(
            app, api,
            SimpleNamespace(
                object_guid=str(guid), author_guid=ME.get("guid"), message_id="",
                text="", is_group=True, is_private=False, is_channel=False
            ),
            ME.get("guid")
        )
        fake.g = g
        fake.rank = ranks.BOT
        for uid, until in list(g.get("mute", {}).items()):
            uid = str(uid)
            try:
                protection = await fake.protection_reason(uid)
            except Exception:
                protection = None
            if protection and ("مقام" in protection or "سودو" in protection or "معاف" in protection):
                continue
            try:
                if until not in (0, True):
                    remaining = float(until) - time.time()
                    if remaining <= 0:
                        continue
                    minutes = max(1, int((remaining + 59) // 60))
                else:
                    minutes = None
                if await api.ensure_mute_permission(str(guid), uid, minutes=minutes):
                    g.setdefault("mute_server_applied", {})[uid] = True
                    g.setdefault("mute_permission_last", {})[uid] = time.time()
                    repaired += 1
            except Exception as exc:
                g.setdefault("mute_server_applied", {})[uid] = False
                g.setdefault("mute_permission_last", {})[uid] = time.time()
                log.debug("boot mute permission repair failed chat=%s user=%s: %r", guid, uid, exc)
    if checked_groups:
        db.save("groups")
    log.info("DB mute boot repair: %d permissions re-applied in %d groups", repaired, checked_groups)
    return {"groups": checked_groups, "repaired": repaired}


async def restore_all_active_mutes_once(force=False):
    """Resync saved mute permissions for every active group without banning users.
    On a fresh process start the first pass is forced, so saved mutes are restored
    immediately instead of waiting for the normal 60-second cadence.
    """
    now = time.monotonic()
    interval = max(15, int(getattr(config, "MUTE_RESTORE_INTERVAL_SECONDS", 60)))
    results = {"groups": 0, "restored": 0, "expired": 0, "protected": 0}
    for guid, g in list(db.data.get("groups", {}).items()):
        if not g.get("active") or not (g.get("mute") or g.get("unmute_pending")):
            continue
        last = float(g.get("mute_restore_last") or 0)
        if not force and now - last < interval:
            continue
        g["mute_restore_last"] = now
        fake = Ctx(
            app, api,
            SimpleNamespace(
                object_guid=str(guid), author_guid=ME.get("guid"), message_id="",
                text="", is_group=True, is_private=False, is_channel=False
            ),
            ME.get("guid")
        )
        fake.g = g
        fake.rank = ranks.BOT
        try:
            result = await lists.restore_active_mutes(fake)
            results["groups"] += 1
            results["restored"] += int(result.get("restored", 0))
            results["expired"] += int(result.get("expired", 0))
            results["protected"] += int(result.get("removed_protected", 0))
        except Exception as exc:
            log.debug("mute restore failed chat=%s: %r", guid, exc)
    if results["groups"]:
        db.save("groups")
    return results


async def restore_closed_groups_once(force=False):
    """Re-apply server default permissions for groups that FBI has closed.

    Manual ``گروه بسته`` is indefinite (closed_until=0); timed wave locks use
    ``closed_until``. On restart we must restore the actual server permission,
    not merely rely on local message deletion.
    """
    now = time.monotonic()
    interval = max(10, int(getattr(config, "GROUP_PERMISSION_RESTORE_INTERVAL_SECONDS", 30)))
    restored = expired = 0
    for guid, g in list(db.data.get("groups", {}).items()):
        if not g.get("active"):
            continue
        closed_until = float(g.get("closed_until") or 0)
        closed = bool(g.get("closed"))
        if closed_until and closed_until <= time.time():
            try:
                ok = await api.open_group_permissions(guid)
                if ok:
                    g["closed"] = False
                    g["closed_until"] = 0
                    g["opened_at"] = time.time()
                    expired += 1
            except Exception:
                pass
            continue
        locked = any((g.get("locks") or {}).values())
        if not closed and not locked:
            continue
        last = float(g.get("group_permission_restore_last") or 0)
        if not force and now - last < interval:
            continue
        g["group_permission_restore_last"] = now
        try:
            synced = await (api.close_group_permissions(guid) if closed else api.sync_group_permissions(guid))
            if synced:
                restored += 1
        except Exception as exc:
            log.debug("group permission restore failed chat=%s: %r", guid, exc)
    if restored or expired:
        db.save("groups")
    return {"restored": restored, "expired": expired}


async def auto_join_active_calls_once():
    global _LAST_CALL_SCAN
    if not getattr(config, "AUTO_JOIN_ACTIVE_CALLS", True):
        return
    now = time.monotonic()
    interval = max(10, int(getattr(config, "AUTO_JOIN_CALL_SCAN_SECONDS", 30)))
    if now - _LAST_CALL_SCAN < interval:
        return
    _LAST_CALL_SCAN = now
    try:
        result = await api.call("get_active_group_calls")
    except Exception:
        return
    rows = api._active_call_rows(result)
    for row in rows:
        cid = api_mod._val(row, "chat_id", "peer_id", "group_id", default=None)
        if cid is None:
            continue
        cid = str(cid)
        g = db.group(cid)
        if not g.get("active") or g.get("manual_disabled"):
            continue
        try:
            ok, info = await api.voice_join(cid)
            if ok:
                g.setdefault("voice_state", {})["active"] = True
                g.setdefault("voice_state", {})["slug"] = str((info or {}).get("slug") or "")
                db.save("groups")
        except Exception as exc:
            log.debug("auto join call failed chat=%s: %s", cid, exc)


async def db_flush_loop():
    """Write deferred database changes at most once per interval, from the event loop.

    Running the writer in a background thread would race with the handlers that mutate
    the same dicts while json.dump walks them, so the write stays on the loop thread.
    """
    import os as _os
    interval = max(0.5, float(_os.getenv("FBI_DB_FLUSH_SECONDS", "1.5")))
    while True:
        await asyncio.sleep(interval)
        try:
            db.flush()
        except Exception as exc:
            log.debug("db flush failed: %s", exc)


async def group_unlock_loop():
    while True:
        try:
            for guid, g in list(db.groups().items()):
                if g.get("closed_until") and float(g.get("closed_until") or 0) <= time.time():
                    await _ensure_group_open_state(guid, g)
                if g.get("active") and g.get("metadata_lock"):
                    try:
                        await advanced.enforce_metadata_lock(guid, g, api)
                    except Exception as exc:
                        log.debug("metadata lock loop failed chat=%s: %s", guid, exc)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.debug("group unlock loop failed: %s", exc)
        await asyncio.sleep(10)


def _get(obj, *keys, default=None):
    return api_mod._val(obj, *keys, default=default)


def _event_chat_id(event):
    return _get(event, "chat_id", "peer_id", default=None)


def _event_user_ids(event):
    result = []
    for key in ("user_id", "member_id", "added_user_id", "removed_user_id", "participant_id"):
        value = _get(event, key, default=None)
        if value is not None:
            result.append(str(value))
    for key in ("user_ids", "member_ids", "added_users", "removed_users"):
        value = _get(event, key, default=None)
        if value:
            if not isinstance(value, (list, tuple, set)):
                value = [value]
            result.extend(str(x) for x in value if x is not None)
    return list(dict.fromkeys(result))


def _event_is_join(event):
    for key in ("user_joined", "user_added", "joined", "join", "added"):
        if _get(event, key, default=False) is True:
            return True
    blob = str(event).lower()
    return any(x in blob for x in ("joined", "userjoined", "chatactionjoin"))


def _event_is_leave(event):
    for key in ("user_left", "user_removed", "left", "removed"):
        if _get(event, key, default=False) is True:
            return True
    blob = str(event).lower()
    return any(x in blob for x in ("left", "removed", "userleft"))


async def sync_admins(ctx, force=False):
    now = time.monotonic()
    old = _ADMIN_SYNC.get(ctx.guid, 0)
    if not force and now - old < _ADMIN_SYNC_TTL:
        return False
    try:
        await admin.sync_real_admins(ctx)
        _ADMIN_SYNC[ctx.guid] = now
        return True
    except Exception as exc:
        log.debug("sync admins failed chat=%s: %s", ctx.guid, exc)
        return False


async def ensure_sudo_full_admin(ctx):
    """در هر گروه، هر دو سازنده را با دسترسی ادمین FULL نگه می‌دارد."""
    if not ctx.is_group or not ctx.guid:
        return 0
    now = time.monotonic()
    ttl = max(10, int(getattr(config, "SUDO_PROMOTE_TTL", 60)))
    promoted = 0
    creator_ids = {str(x) for x in getattr(config, "CREATOR_IDS", getattr(config, "SUDOS", [])) if str(x).strip()}
    for sid in creator_ids:
        key = (str(ctx.guid), sid)
        if now - float(_SUDO_PROMOTE_CACHE.get(key, 0)) < ttl:
            continue
        _SUDO_PROMOTE_CACHE[key] = now
        try:
            await ctx.api.set_admin(ctx.guid, sid, access="full")
            ctx.g.setdefault("admins", [])
            # سازنده‌ها علاوه بر rank سراسری، در دیتابیس گروه هم به‌عنوان
            # FULL ADMIN ثبت می‌شوند تا sync ادمین‌ها آن‌ها را حذف نکند.
            if sid != str(ctx.g.get("owner")) and sid not in {str(x) for x in ctx.g["admins"]}:
                ctx.g["admins"].append(sid)
            ctx.g.setdefault("full_admins", [])
            if sid not in {str(x) for x in ctx.g["full_admins"]}:
                ctx.g["full_admins"].append(sid)
            ctx.g["sudo_last_promoted_at"] = time.time()
            promoted += 1
            log.info("SUDO full-admin ensured chat=%s user=%s", ctx.guid, sid)
        except Exception as exc:
            log.debug("SUDO full-admin promotion failed chat=%s user=%s: %s", ctx.guid, sid, exc)
    if promoted:
        db.save("groups")
    return promoted


async def bio_link_precheck(ctx):
    if not config.BIO_LINK_MODERATION or not ctx.g.get("bio_link_mute", True) or not ctx.author:
        return False
    if await ctx.protection_reason(ctx.author):
        return False
    # Once a user is already muted for a bio link, do not re-issue the mute
    # RPC on every message; the normal mute filter below will remove messages.
    if lists.is_muted(ctx.g, ctx.author):
        return False
    # Check the live/full-user profile on each new sender message so changing
    # a bio to a URL is detected immediately. The API layer deduplicates
    # concurrent requests per user.
    bio = await api.user_bio(ctx.author, force=True, chat_hint=ctx.guid)
    if not utils.has_link(bio):
        return False
    bio_seconds = getattr(config, "BIO_LINK_MUTE_SECONDS", None)
    if bio_seconds is None:
        legacy_minutes = getattr(config, "BIO_LINK_MUTE_MINUTES", None)
        bio_seconds = None if legacy_minutes is None else int(legacy_minutes) * 60
    now = time.time()
    ctx.g.setdefault("mute", {})[ctx.author] = now + int(bio_seconds) if bio_seconds else 0
    ctx.g.setdefault("mute_started", {})[ctx.author] = now
    ctx.g.setdefault("mute_messages", {})[ctx.author] = []
    ctx.g.setdefault("mute_reason", {})[ctx.author] = "bio_link"
    ctx.g["bio_link_hits"] = int(ctx.g.get("bio_link_hits") or 0) + 1
    db.save("groups")
    server_ok = False
    try:
        await api.mute_member(ctx.guid, ctx.author, seconds=bio_seconds)
        server_ok = True
    except Exception:
        pass
    if config.BIO_LINK_DELETE_MESSAGE and ctx.message_id:
        try:
            await api.delete(ctx.guid, [ctx.message_id])
            db.bump(ctx.guid, ctx.author, "deleted")
        except Exception:
            pass
    if config.BIO_LINK_NOTIFY:
        name = await ctx.target_link(ctx.author)
        duration = "دائمی" if not bio_seconds else utils.human_duration(int(bio_seconds))
        await api.send(ctx.guid, f"🛡️ {name}\n🚫 در بیوگرافی شما لینک شناسایی شد.\n🔇 محدودیت: {duration}\n{'✅' if server_ok else '⚠️'}", reply_to=ctx.message_id)
    return True


async def pre_checks(ctx):
    try:
        await lists.cleanup_expired_mutes(ctx)
    except Exception:
        pass
    protected = await ctx.protection_reason(ctx.author)
    if not protected:
        try:
            if await bio_link_precheck(ctx):
                return True
        except Exception as exc:
            log.debug("bio precheck failed: %s", exc)

    if lists.is_killed(ctx.g, ctx.author):
        await api.delete(ctx.guid, [ctx.message_id])
        await api.ban(ctx.guid, ctx.author)
        if ctx.author in ctx.g.get("kill", []):
            ctx.g["kill"].remove(ctx.author)
        return True

    if lists.is_muted(ctx.g, ctx.author):
        await api.delete(ctx.guid, [ctx.message_id])
        ctx.g.setdefault("mute_messages", {}).setdefault(ctx.author, []).append(ctx.message_id)
        # Keep retrying the real server permission when an earlier mute RPC
        # failed. This closes the window where the user could briefly post until
        # the next periodic restore pass. The retry itself never bans the user.
        server_applied = bool(ctx.g.setdefault("mute_server_applied", {}).get(ctx.author))
        last_try = float(ctx.g.setdefault("mute_permission_last", {}).get(ctx.author) or 0)
        retry_after = max(5, int(getattr(config, "MUTE_PERMISSION_RETRY_SECONDS", 15)))
        if not server_applied and time.time() - last_try >= retry_after:
            until = ctx.g.get("mute", {}).get(ctx.author, 0)
            minutes = None
            if until not in (0, True):
                minutes = max(1, int((float(until) - time.time() + 59) // 60))
            try:
                server_applied = bool(await api.ensure_mute_permission(ctx.guid, ctx.author, minutes=minutes))
            except Exception:
                server_applied = False
            ctx.g.setdefault("mute_server_applied", {})[ctx.author] = server_applied
            ctx.g.setdefault("mute_permission_last", {})[ctx.author] = time.time()
        db.save("groups")
        return True

    await _ensure_group_open_state(ctx.guid, ctx.g)
    if ctx.g.get("closed") and ctx.rank == ranks.USER:
        await api.delete(ctx.guid, [ctx.message_id])
        return True
    if ctx.g.get("allowed_message_active") and ctx.g.get("allowed_message") and ctx.rank == ranks.USER:
        if ctx.text != utils.normalize(ctx.g["allowed_message"]):
            await api.delete(ctx.guid, [ctx.message_id])
            return True

    hit = locks.detect(ctx)
    if hit and not protected:
        wave_triggered = False
        try:
            wave_triggered = locks.register_abuse_wave(ctx, hit)
        except Exception:
            wave_triggered = False
        if wave_triggered:
            now = time.time()
            ctx.g["closed_until"] = now + int(getattr(config, "SPAM_WAVE_LOCK_SECONDS", 3600))
            ctx.g["closed"] = True
            ctx.g["wave_lock_notified_at"] = now
            db.save("groups")
            server_closed = False
            try:
                server_closed = await api.close_group_permissions(ctx.guid)
            except Exception:
                server_closed = False
            title = {"link":"لینک", "username":"آیدی", "hyperlink":"هایپرلینک", "spam":"اسپم", "badword":"کلمه نامناسب"}.get(hit, hit)
            wave_text = (
                "🚨 موج فعالیت مشکوک شناسایی شد\n"
                "🔒 گروه به مدت ۱ ساعت بسته شد.\n"
                f"📌 علت: افزایش متوالی «{title}»\n"
                + ("✅ محدودیت سروری اعمال شد." if server_closed else "⚠️ محدودیت سروری در دسترس نبود و کنترل پیام‌ها محلی ادامه دارد.")
            )
            await ctx.status_update("wave_lock", wave_text)
        await locks.punish(ctx, hit)
        return True
    if hit and protected:
        return False

    if lists.is_blocked(ctx.g, ctx.author):
        await api.delete(ctx.guid, [ctx.message_id])
        return True
    return False


def count_message(ctx):
    if not ctx.author:
        return
    db.bump(ctx.guid, ctx.author, "total")
    if any(getattr(ctx.update, key, False) for key in ("photo", "video", "gif", "voice", "music", "file", "sticker")):
        db.bump(ctx.guid, ctx.author, "media")
    elif ctx.raw_text:
        db.bump(ctx.guid, ctx.author, "text")
    db.history_add(ctx.guid, {"t": int(time.time()), "u": ctx.author, "m": ctx.message_id, "x": ctx.raw_text[:200]})


async def on_chat_action(event):
    chat_id = _event_chat_id(event)
    if chat_id is None:
        return
    chat_id = str(chat_id)
    g = db.group(chat_id)
    target_ids = _event_user_ids(event)
    target = target_ids[0] if target_ids else None
    try:
        current_bot_admin = await api.is_admin(chat_id, ME["guid"], fresh=True)
        g["_bot_admin"] = bool(current_bot_admin)
        if current_bot_admin and not g.get("active") and not g.get("manual_disabled"):
            fake = Ctx(app, api, SimpleNamespace(object_guid=chat_id, author_guid=ME["guid"], message_id="", text="", is_group=True, is_private=False, is_channel=False), ME["guid"])
            fake.rank = ranks.BOT
            await admin.activate(fake)
    except Exception as exc:
        log.debug("chat action admin check failed: %s", exc)
    if g.get("active"):
        try:
            fake = Ctx(app, api, SimpleNamespace(object_guid=chat_id, author_guid=target, message_id="", text="", is_group=True, is_private=False, is_channel=False), ME["guid"])
            await admin.sync_real_admins(fake)
        except Exception:
            pass
    try:
        await advanced.on_chat_action(event, api, ME.get("guid"))
    except Exception as exc:
        log.debug("advanced chat-action enforcement failed: %s", exc)
    if target and str(target) in {str(x) for x in getattr(config, "SUDOS", [])}:
        # A SUDO identity gets full admin rights whenever it is observed in a
        # group event; this never changes the real group creator/owner.
        try:
            await api.set_admin(chat_id, target)
            g.setdefault("admins", [])
            if str(target) not in {str(x) for x in g["admins"]}:
                g["admins"].append(str(target))
            db.save("groups")
            log.info("SUDO full-admin ensured from chat-action chat=%s user=%s", chat_id, target)
        except Exception as exc:
            log.debug("SUDO chat-action promotion failed chat=%s user=%s: %s", chat_id, target, exc)
    if target and _event_is_join(event):
        db.bump(chat_id, target, "joined")
        if g.get("welcome"):
            info = await api.group_info(chat_id)
            title = api_mod.deep(info, "group", "group_title", default="گروه") or "گروه"
            temp = fake if 'fake' in locals() else Ctx(app, api, SimpleNamespace(object_guid=chat_id, author_guid=target, message_id="", text="", is_group=True, is_private=False, is_channel=False), ME["guid"])
            name = await temp.target_link(target)
            raw_ts = _get(event, "date", "timestamp", "time", default=None)
            try:
                join_ts = float(raw_ts) if raw_ts is not None else time.time()
                if join_ts > 10**11:
                    join_ts /= 1000.0
            except Exception:
                join_ts = time.time()
            join_date = utils.stamp(join_ts)
            default_welcome = (
                "🌹 {name} به گروه {group} خوش آمدید.\n"
                "📅 تاریخ عضویت: {join_date}"
            )
            body = (g.get("welcome_text") or default_welcome)
            body = body.replace("{name}", name).replace("{group}", str(title)).replace("{join_date}", join_date)
            await api.send(chat_id, msgstyle.block(g, "📢 خوش‌آمدگویی", body, plain_title=False))
    if target and _event_is_leave(event):
        db.bump(chat_id, target, "left")
        if g.get("goodbye"):
            temp = fake if 'fake' in locals() else Ctx(app, api, SimpleNamespace(object_guid=chat_id, author_guid=target, message_id="", text="", is_group=True, is_private=False, is_channel=False), ME["guid"])
            name = await temp.target_link(target)
            body = (g.get("goodbye_text") or "{name} از گروه رفت. به امید دیدار 🍃").replace("{name}", name)
            await api.send(chat_id, msgstyle.block(g, "👋 خداحافظی", body, plain_title=False))
    db.save(("groups", "stats"))


PV_HEADER = "╭─「 🕵️ FBI SPLUS 」─╮"
PV_FOOTER = "╰────────────────────╯"


OWNER_HELP = """```text
🕵️ FBI — پنل سازنده

/owner
لغو اشتراک
تمدید اشتراک [آیدی گروه]
تبلیغات
اطلاع رسانی [متن]

عملیات گروهی
• برکناری [ریپلای/آیدی]
• لیست ادمین / لیست ویژه / مقامم
• زیرپرچم من / لغو زیرپرچم

سازندگان FBI:
• `56597421`
• `69871358`

اشتراک سازنده:
• هدیه اشتراک سراسری [روز]
• هدیه اشتراک تکی [آیدی گروه] [روز]
• لغو اشتراک

تمدید اشتراک:
1) تمدید اشتراک [آیدی گروه]
2) FBI نام و مالک گروه را نمایش می‌دهد.
3) فقط تعداد روز را بفرستید؛ مثال: 30

هزینه ۱ ماهه: 50000 تومان
```"""


def _private_owner(uid):
    return subscription.is_sudo(uid)


async def _owner_panel(ctx):
    groups = db.data["groups"]
    total = len(groups)
    active = 0
    subordinate = 0
    for guid, g in groups.items():
        if g.get("subordinate"):
            subordinate += 1
        if g.get("active") and (g.get("subordinate") or subscription.allowed(str(guid))):
            active += 1
    off = max(0, total - active)
    status = (
        "```text\n"
        "📊 وضعیت فعلی FBI\n\n"
        f"گروه‌های ثبت‌شده: {utils.fa_digits(total)}\n"
        f"گروه‌های فعال: {utils.fa_digits(active)}\n"
        f"گروه‌های غیرفعال: {utils.fa_digits(off)}\n"
        f"زیرپرچم: {utils.fa_digits(subordinate)}\n"
        "```"
    )
    creator_link = ""
    creator_link_error = None
    try:
        creator_link = webpanel.issue_creator_link(str(ctx.author))
    except Exception as exc:
        creator_link_error = exc
        log.warning("could not issue creator web panel link: %r", exc)
    web_text = (
        "\n\n🌐 پنل وب سازنده\n" + creator_link +
        "\n\n🔐 این لینک خصوصی و موقت است؛ برای بازکردن پنل از دستگاه دیگر باید آدرس عمومی سرور/تونل تنظیم شده باشد."
        if creator_link else (
            "\n\n🌐 تونل عمومی هنوز وصل نیست یا لینک عمومی آماده نشده است. "
            "چند ثانیه صبر کنید و دوباره `/owner` را بفرستید."
            if creator_link_error and not webpanel._base_url()
            else "\n\n⚠️ ساخت لینک پنل وب ممکن نشد؛ تنظیمات پنل و شناسهٔ سازنده را بررسی کنید."
        )
    )
    await ctx.reply(OWNER_HELP + "\n\n" + status + web_text, reply=False)


async def _owner_renew_start(ctx, raw_guid):
    gid = str(raw_guid).strip()
    if not re.fullmatch(r"-?\d+", gid):
        await ctx.err("آیدی گروه باید عددی باشد؛ مثال: -1001234567890")
        return True
    try:
        preview = await subscription.group_preview(api, gid)
    except Exception as exc:
        log.debug("owner group preview failed: %s", exc)
        preview = None
    if not preview:
        await ctx.err("گروه شناسایی نشد.")
        return True
    owner = preview.get("owner")
    owner_text = f"ID: `{owner}`" if owner else "شناسایی نشد"
    if owner:
        try:
            info = await api.user_info(owner)
            name = api_mod.name_of(info) if info else "کاربر"
            owner_text = await ctx.target_link(owner, fallback_name=name)
        except Exception:
            pass
    subscription.mark_renew_pending(ctx.author, gid)
    current = subscription.expiry_text(preview.get("group") or {})
    await ctx.reply(
        "✅ گروه شناسایی شد.\n\n"
        f"🏷 نام گروه: {preview.get('title', 'گروه')}\n"
        f"🆔 آیدی گروه: `{gid}`\n"
        f"👑 مالک: {owner_text}\n"
        f"📆 وضعیت فعلی: {current}\n\n"
        "⏳ حالا تعداد روز اشتراک را فقط به‌صورت عدد بفرستید.\n"
        "مثال: `30`\n\n"
        "برای انصراف: `لغو`",
        reply=False,
    )
    return True


async def _owner_gift_finish(ctx):
    item = subscription.pending_gift(ctx.author)
    if not item:
        return False
    if str(ctx.text).strip() == "لغو":
        subscription.clear_gift_pending(ctx.author)
        await ctx.reply("عملیات هدیه لغو شد. هیچ تغییری اعمال نشد.", reply=False)
        return True
    days = subscription._as_days(ctx.text)
    if days is None:
        await ctx.err("تعداد روز معتبر نیست. عددی بین ۱ تا ۳۶۵۰ بفرستید.")
        return True
    try:
        if item.get("mode") == "all":
            changed, sent, failed, skipped = await subscription.gift_all(api, days)
            await ctx.reply(
                "🎁 هدیه اشتراک سراسری انجام شد.\n\n"
                f"⏳ مدت: {utils.fa_digits(days)} روز\n"
                f"📦 گروه‌های فعال‌شده: {utils.fa_digits(changed)}\n"
                f"📨 پیام موفق: {utils.fa_digits(sent)}\n"
                f"❌ ناموفق: {utils.fa_digits(failed)}\n"
                f"⚠️ ردشده: {utils.fa_digits(skipped)}",
                reply=False,
            )
        else:
            gid = str(item.get("guid"))
            preview, until = await subscription.gift_one(api, gid, days)
            left = max(0, int(until - time.time()))
            owner = preview.get("owner")
            owner_text = f"`{owner}`" if owner else "شناسایی نشد"
            await ctx.reply(
                "🎁 هدیه اشتراک ثبت شد.\n\n"
                f"🏷 گروه: {preview.get('title', 'گروه')}\n"
                f"🆔 آیدی: `{gid}`\n"
                f"👑 مالک: {owner_text}\n"
                f"⏳ مدت: {utils.fa_digits(days)} روز\n"
                f"📅 اعتبار: {utils.human_duration(left)}",
                reply=False,
            )
    except Exception as exc:
        log.exception("owner gift failed: %s", exc)
        await ctx.err(f"هدیه اشتراک انجام نشد: {type(exc).__name__}: {exc}")
    finally:
        subscription.clear_gift_pending(ctx.author)
    return True


async def _owner_renew_finish(ctx, days):
    item = subscription.pending(ctx.author)
    if not item:
        return False
    if str(ctx.text).strip() == "لغو":
        subscription.clear_pending(ctx.author)
        await ctx.reply("لغو شد. هیچ اشتراکی تغییر نکرد.", reply=False)
        return True
    parsed = subscription._as_days(days)
    if parsed is None:
        await ctx.err("تعداد روز معتبر نیست. عددی بین 1 تا 3650 بفرستید.")
        return True
    gid = str(item["guid"])
    try:
        await subscription.group_preview(api, gid)
        until = await subscription.renew(api, gid, parsed)
    except Exception as exc:
        subscription.clear_pending(ctx.author)
        log.exception("owner subscription renew failed: %s", exc)
        await ctx.err(f"تمدید اشتراک انجام نشد: {type(exc).__name__}: {exc}")
        return True
    subscription.clear_pending(ctx.author)
    left = max(0, int(until - time.time()))
    await ctx.reply(
        "✅ اشتراک با موفقیت تمدید شد.\n\n"
        f"🆔 گروه: `{gid}`\n"
        f"⏳ مدت: {utils.fa_digits(parsed)} روز\n"
        f"📅 اعتبار باقی‌مانده: {utils.human_duration(left)}\n"
        "🤖 FBI دوباره در گروه فعال شد.",
        reply=False,
    )
    return True



# --------------------- سازنده: تبلیغات و اطلاع‌رسانی ---------------------
AD_PENDING: dict[str, dict] = {}


def _ad_clear(uid):
    item = AD_PENDING.pop(str(uid), None)
    if item:
        path = item.get("photo")
        if path and os.path.exists(path):
            try:
                os.remove(path)
            except OSError:
                pass


async def _ad_targets():
    """Return known PV recipients and active group recipients."""
    users = []
    for uid, row in db.data.get("users", {}).items():
        sid = str(uid)
        if not sid.lstrip("-").isdigit():
            continue
        if sid in {str(x) for x in config.SUDOS} or sid == str(ME.get("guid")):
            continue
        users.append(sid)
    groups = [str(gid) for gid, gg in db.data.get("groups", {}).items()
              if gg.get("active") and not gg.get("manual_disabled")]
    return list(dict.fromkeys(users)), list(dict.fromkeys(groups))


async def _ad_send(ctx, *, include_pv=False, include_groups=False):
    item = AD_PENDING.get(str(ctx.author)) or {}
    text = str(item.get("text") or "").strip()
    photo = item.get("photo")
    if not text and not photo:
        await ctx.err("تبلیغی برای ارسال آماده نشده است.")
        _ad_clear(ctx.author)
        return True

    users, groups = await _ad_targets()
    body = "📢 تبلیغات FBI\n\n" + (text or "")
    sent_pv = sent_groups = failed = 0

    if include_pv:
        for uid in users:
            try:
                if photo:
                    result = await api.send_photo(uid, photo, caption=body)
                else:
                    result = await api.send(uid, body)
                if result is not None:
                    sent_pv += 1
                else:
                    failed += 1
            except Exception:
                failed += 1

    if include_groups:
        for gid in groups:
            try:
                if photo:
                    result = await api.send_photo(gid, photo, caption=body)
                else:
                    result = await api.send(gid, body)
                if result is not None:
                    sent_groups += 1
                else:
                    failed += 1
            except Exception:
                failed += 1

    _ad_clear(ctx.author)
    await ctx.reply(
        "✅ تبلیغات ارسال شد.\n\n"
        f"👤 PV: {utils.fa_digits(sent_pv)}\n"
        f"👥 گروه: {utils.fa_digits(sent_groups)}\n"
        f"❌ ناموفق: {utils.fa_digits(failed)}" if failed else
        "✅ تبلیغات ارسال شد.",
        reply=False,
    )
    return True


async def _owner_ad_flow(ctx):
    if not _private_owner(ctx.author):
        return False
    uid = str(ctx.author)
    item = AD_PENDING.get(uid)
    text = ctx.text.strip()

    if text in ("تبلیغات لغو", "لغو تبلیغات"):
        _ad_clear(uid)
        await ctx.reply("عملیات تبلیغات لغو شد.", reply=False)
        return True

    if text == "تبلیغات" and not item:
        AD_PENDING[uid] = {"stage": "text", "created": time.time(), "text": "", "photo": None}
        await ctx.reply("📢 متن تبلیغ را بفرستید.", reply=False)
        return True

    if not item:
        return False
    if time.time() - float(item.get("created") or 0) > 900:
        _ad_clear(uid)
        await ctx.err("زمان این عملیات تمام شده است؛ دوباره «تبلیغات» را بفرستید.")
        return True

    stage = item.get("stage")
    if stage == "text":
        if not ctx.raw_text.strip() or text == "تبلیغات":
            await ctx.err("متن تبلیغ خالی است. متن تبلیغ را بفرستید.")
            return True
        item["text"] = ctx.raw_text.strip()
        item["stage"] = "photo"
        await ctx.reply(
            "✅ متن ذخیره شد.\n\n"
            "حالا روی عکس تبلیغ ریپلای کنید و فقط «تبلیغات» را بفرستید.\n"
            "بعد مقصد را انتخاب می‌کنید: `پیوی`، `گروه` یا `هردو`.",
            reply=False,
        )
        return True

    if stage == "photo":
        if text != "تبلیغات" or not ctx.reply_id:
            await ctx.err("روی عکس ریپلای کنید و «تبلیغات» را بفرستید.")
            return True
        path = await api.download_message_file(ctx.guid, ctx.reply_id, config.TEMP_DIR, default_ext="jpg")
        if not path or not os.path.isfile(path):
            await ctx.err("عکس ریپلای‌شده قابل دریافت نیست.")
            return True
        item["photo"] = path
        item["stage"] = "target"
        await ctx.reply("📤 مقصد را بفرستید:\n\n`پیوی`\n`گروه`\n`هردو`", reply=False)
        return True

    if stage == "target":
        if text not in ("پیوی", "گروه", "هردو"):
            await ctx.err("فقط «پیوی»، «گروه» یا «هردو» معتبر است.")
            return True
        return await _ad_send(ctx,
                               include_pv=text in ("پیوی", "هردو"),
                               include_groups=text in ("گروه", "هردو"))
    return False


async def _owner_announcement(ctx):
    if not _private_owner(ctx.author):
        return False
    m = re.fullmatch(r"(?:اطلاع رسانی|اطلاع‌رسانی)\s+(.+)", ctx.raw_text.strip(), re.S)
    if not m:
        return False
    users, groups = await _ad_targets()
    body = "📢 اطلاع‌رسانی FBI\n\n" + m.group(1).strip()
    sent_pv = sent_groups = failed = 0
    for uid in users:
        try:
            if await api.send(uid, body) is not None:
                sent_pv += 1
            else:
                failed += 1
        except Exception:
            failed += 1
    for gid in groups:
        try:
            if await api.send(gid, body) is not None:
                sent_groups += 1
            else:
                failed += 1
        except Exception:
            failed += 1
    await ctx.reply(
        "✅ اطلاع‌رسانی سراسری ارسال شد.\n\n"
        f"👤 PV: {utils.fa_digits(sent_pv)}\n"
        f"👥 گروه: {utils.fa_digits(sent_groups)}\n"
        f"❌ ناموفق: {utils.fa_digits(failed)}" if failed else
        "✅ اطلاع‌رسانی سراسری ارسال شد.",
        reply=False,
    )
    return True


_FORCE_JOIN_CACHE = {}

async def force_join_ok(ctx):
    if not getattr(config, "FORCE_JOIN_ENABLED", False) or _private_owner(ctx.author):
        return True
    uid = str(ctx.author or "")
    if not uid:
        return False
    now = time.monotonic()
    cached = _FORCE_JOIN_CACHE.get(uid)
    ttl = max(0.0, float(getattr(config, "FORCE_JOIN_CACHE_SECONDS", 5)))
    if cached and now - float(cached.get("t") or 0) < ttl:
        return bool(cached.get("ok"))
    ok = False
    try:
        ok = await api.is_channel_member(config.FORCE_JOIN_CHANNEL_ID, uid, fresh=True)
        if not ok:
            # fallback to the public channel username when the numeric channel
            # reference cannot be resolved correctly by a particular Splus build.
            ok = await api.is_channel_member("@FBIbotdd", uid, fresh=True)
    except Exception as exc:
        log.debug("force join check failed user=%s: %r", uid, exc)
    _FORCE_JOIN_CACHE[uid] = {"t": now, "ok": bool(ok)}
    if not ok:
        await ctx.reply(
            "🔒 دسترسی شما به FBI هنوز فعال نشده است.\n\n"
            "👥 برای استفاده از ربات ابتدا در کانال زیر عضو شوید.\n"
            f"🔗 [عضویت در کانال FBI]({config.FORCE_JOIN_CHANNEL_LINK})\n\n"
            "✅ بعد از عضویت، دوباره پیام یا لینک خود را بفرستید.\n"
            "❌ اگر عضو نباشید، ربات پاسخ شما را پردازش نمی‌کند."
        )
    return bool(ok)


async def handle_private(ctx):
    if not await force_join_ok(ctx):
        return
    if _private_owner(ctx.author):
        if await _owner_ad_flow(ctx):
            return
        if await _owner_announcement(ctx):
            return
        if await _owner_gift_finish(ctx):
            return
        if await _owner_renew_finish(ctx, ctx.text):
            return
        # The PV panel menu runs after pending SUDO conversations, so values
        # such as a subscription duration are never mistaken for a group index.
        if await webpanel.handle_private(ctx):
            return
        if ctx.text == "/owner":
            await _owner_panel(ctx)
            return
        if ctx.text == "هدیه اشتراک سراسری":
            subscription.mark_gift_all_pending(ctx.author)
            await ctx.reply(
                "🎁 هدیه اشتراک سراسری\n\nتعداد روز اشتراک هدیه را بفرستید.\nمثال: `30`\n\nبرای انصراف: `لغو`",
                reply=False,
            )
            return
        m = re.fullmatch(r"هدیه اشتراک سراسری\s+(\d{1,5})", ctx.text)
        if m:
            days = subscription._as_days(m.group(1))
            if days is None:
                await ctx.err("تعداد روز معتبر نیست.")
            else:
                changed, sent, failed, skipped = await subscription.gift_all(api, days)
                await ctx.reply(
                    "🎁 هدیه اشتراک سراسری انجام شد.\n\n"
                    f"⏳ مدت: {utils.fa_digits(days)} روز\n"
                    f"📦 گروه‌ها: {utils.fa_digits(changed)}\n"
                    f"📨 موفق: {utils.fa_digits(sent)}\n"
                    f"❌ ناموفق: {utils.fa_digits(failed)}",
                    reply=False,
                )
            return
        m = re.fullmatch(r"هدیه اشتراک تکی(?:\s+(-?\d+))?", ctx.text)
        if m and m.group(1):
            gid = m.group(1)
            preview = await subscription.group_preview(api, gid)
            if not preview:
                await ctx.err("گروه شناسایی نشد.")
                return
            subscription.mark_gift_one_pending(ctx.author, gid)
            owner = preview.get("owner")
            owner_text = f"`{owner}`" if owner else "شناسایی نشد"
            await ctx.reply(
                "✅ گروه برای هدیه شناسایی شد.\n\n"
                f"🏷 نام: {preview.get('title', 'گروه')}\n"
                f"🆔 آیدی: `{gid}`\n"
                f"👑 مالک: {owner_text}\n\n"
                "⏳ تعداد روز را بفرستید. مثال: `30`\n"
                "برای انصراف: `لغو`",
                reply=False,
            )
            return
        if ctx.text == "هدیه اشتراک تکی":
            await ctx.reply("فرمت: `هدیه اشتراک تکی -1001234567890`", reply=False)
            return
        if ctx.text == "لغو اشتراک":
            changed, sent, failed = await subscription.cancel_all(api)
            await ctx.reply(
                "✅ لغو اشتراک سراسری انجام شد.\n\n"
                f"📦 گروه‌های تغییرکرده: {utils.fa_digits(changed)}\n"
                f"📨 پیام ارسال‌شده: {utils.fa_digits(sent)}\n"
                f"❌ ناموفق: {utils.fa_digits(failed)}\n"
                "⭐ گروه‌های زیرپرچم از لغو سراسری مستثنی ماندند.",
                reply=False,
            )
            return
        m = re.fullmatch(r"تمدید اشتراک\s+(-?\d+)", ctx.text)
        if m:
            await _owner_renew_start(ctx, m.group(1))
            return
    else:
        if await webpanel.handle_private(ctx):
            return

    # نقل قول و اسپویلر در PV هم کار می‌کنند؛ بقیهٔ پاسخ‌های PV زیر همین بخش است.
    if await beauty.handle_private(ctx):
        return
    user_name = await ctx.sender_name()
    links = utils.extract_join_links(ctx.raw_text)
    if config.AUTO_JOIN_PV_LINKS and links:
        results = []
        for link in links[:3]:
            try:
                await api.join_group(link)
                results.append((link, True))
            except Exception:
                results.append((link, False))
        lines = [f"👤 {user_name}", "", "🔗 لینک دریافت شد."]
        lines.extend(("✅ جوین شد" if ok else "❌ جوین نشد") + f" — `{link}`" for link, ok in results)
        lines += ["", "لینک دعوت Splus را همینجا بفرستید."]
        await ctx.reply(msgstyle.card("FBI SPLUS", "\n".join(lines), "🕵️"))
        return
    await ctx.reply(msgstyle.card("FBI SPLUS", "\n".join([
        f"👤 {user_name}",
        "",
        "درود 🌹",
        "من FBI هستم.",
        "لینک دعوت گروه را بفرستید تا در صورت معتبر بودن جوین شوم.",
        "برای فعال‌سازی، FBI را ادمین کامل گروه کنید و «فعال» را بفرستید.",
    ]), "🕵️"))


async def auto_activate_once():
    if not ME.get("guid"):
        return
    chats = []
    for name, kwargs in (("get_chats", {"limit": config.AUTO_ACTIVATION_MAX_CHATS}), ("get_groups", {})):
        fn = getattr(app, name, None)
        if not callable(fn):
            continue
        try:
            value = fn(**kwargs)
            if asyncio.iscoroutine(value):
                value = await value
            if isinstance(value, dict):
                value = value.get("chats") or value.get("groups") or value.get("results") or value.get("data") or []
            if isinstance(value, (list, tuple)):
                chats.extend(value)
        except Exception:
            continue
    seen = set()
    for chat in chats:
        cid = api_mod.guid_of(chat) or _get(chat, "chat_id", "peer_id", default=None)
        if cid is None or str(cid) in seen:
            continue
        seen.add(str(cid))
        key = str(cid)
        g = db.group(key)
        try:
            fake = Ctx(app, api, SimpleNamespace(object_guid=key, author_guid=ME["guid"], message_id="", text="", is_group=True, is_private=False, is_channel=False), ME["guid"])
            fake.rank = ranks.BOT
            # حتی اگر گروه قبلاً فعال شده باشد، سازنده‌ها باید FULL بمانند.
            await ensure_sudo_full_admin(fake)
            if g.get("active") or g.get("manual_disabled"):
                continue
            if not await api.is_admin(key, ME["guid"], fresh=True):
                continue
            await admin.activate(fake)
        except Exception as exc:
            log.debug("auto activate %s failed: %s", key, exc)


async def auto_activation_loop():
    while True:
        try:
            await auto_activate_once()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.debug("auto activation loop failed: %s", exc)
        try:
            await auto_join_active_calls_once()
        except Exception:
            pass
        try:
            await restore_all_active_mutes_once()
        except Exception:
            pass
        try:
            await restore_closed_groups_once()
        except Exception:
            pass
        try:
            await stats.finalize_pending_loyalty_surveys(app, api)
        except Exception:
            pass
        await asyncio.sleep(max(5, int(config.AUTO_ACTIVATION_POLL_SECONDS)))


async def dispatch(message, *, edited=False, internal=False):
    if not internal and bool(_get(message, "is_me", "out", default=False)):
        return
    # Seed resolved Splus entities from the incoming message before any
    # role/member/send call touches the raw -100... chat id.
    if not internal:
        try:
            await api.prime_message(message)
        except Exception as exc:
            log.debug("message entity priming failed: %s", exc)
    update = make_update(message, edited=edited)
    ctx = Ctx(app, api, update, ME["guid"])
    # SplusLib exposes ``is_private`` on every Message.  Keep this explicit
    # flag authoritative so incoming DMs always reach handle_private().
    if _get(message, "is_private", default=None) is True:
        ctx.is_private = True
        ctx.is_group = False
    if not ctx.guid:
        return
    if ctx.is_private:
        await handle_private(ctx)
        return
    if not ctx.is_group:
        return

    # SUDO is promoted whenever seen, independent of local FBI rank storage.
    await ensure_sudo_full_admin(ctx)

    # اشتراک قبل از هر پردازش گروهی بررسی می‌شود؛ زیرپرچم از این محدودیت مستثنی است.
    if not subscription.allowed(ctx.guid):
        if ctx.text in admin.ACTIVATE_WORDS:
            await ctx.err("اشتراک این گروه فعال نیست. برای تمدید، سازنده FBI باید اشتراک گروه را تمدید کند.")
        return

    # If the group is already active, "فعال" is only a status check. It must
    # never rewrite owner/admin state or recreate activation messages.
    if ctx.g.get("active") and ctx.text in admin.ACTIVATE_WORDS:
        await ctx.reply("🤖 من فعالم ✅")
        return

    # Activation remains explicit. If inactive, only a real owner/admin (or SUDO)
    # may activate; the activator is never promoted to owner.
    if not ctx.g.get("active"):
        if ctx.text in admin.ACTIVATE_WORDS:
            activated = await admin.activate(ctx, explicit=True)
            if activated:
                # Recompute rank after activation so stale owner state from an
                # older database cannot turn the activator into OWNER.
                ctx.rank = ranks.rank(ctx.guid, ctx.author)
                try:
                    await ctx.refresh_rank()
                except Exception:
                    pass
        return

    await sync_admins(ctx, force=False)
    await ctx.refresh_rank()

    try:
        if await stats.handle_loyalty_vote(ctx):
            db.save(("groups", "stats"))
            return
    except Exception as exc:
        log.debug("loyalty vote handler failed: %s", exc)

    # Dedicated routing for statistics so exact stats commands cannot be
    # intercepted by other modules or command-disable rules.
    if ctx.text in (
        "آمار گروه", "امار گروه", "آمار گپ", "امار گپ", "آمار هفتگی", "امار هفتگی", "فعالیت",
        "اسکن همه", "آمار", "امار", "آمارم", "امارم",
        "آمارش", "امارش"
    ):
        if await stats.handle(ctx):
            db.save(("groups", "stats", "history", "users"))
            return

    # Edited messages are moderation events, not a second normal message.
    # Count/store them exactly once, run only the moderation pre-checks, then
    # stop so an edited command is never executed as a fresh command.
    if edited:
        if ctx.author:
            db.bump(ctx.guid, ctx.author, "edited")
            db.history_add(ctx.guid, {
                "m": str(ctx.message_id),
                "u": str(ctx.author),
                "t": time.time(),
                "text": ctx.raw_text,
                "e": True,
                "reply": ctx.reply_id,
            })
        if await pre_checks(ctx):
            db.save(("groups", "stats", "history", "users"))
            return
        try:
            if await advanced.moderate(ctx):
                db.save(("groups", "stats", "history", "users"))
                return
        except Exception as exc:
            log.debug("advanced edited moderation failed: %s", exc)
        db.save(("groups", "stats", "history", "users"))
        return

    count_message(ctx)
    if await pre_checks(ctx):
        db.save(("groups", "stats", "history", "users"))
        return

    try:
        await stats.record_clean_message(ctx)
    except Exception as exc:
        log.debug("loyalty tracker failed: %s", exc)

    try:
        if await advanced.moderate(ctx):
            db.save(("groups", "stats", "history", "users"))
            return
    except Exception as exc:
        log.debug("advanced moderation failed: %s", exc)

    try:
        if await advanced.fire_reply_rules(ctx):
            db.save(("groups", "stats", "history", "users"))
            return
    except Exception as exc:
        log.debug("advanced reply rule failed: %s", exc)

    for module in MODULES:
        try:
            if await module.handle(ctx):
                db.save(("groups", "stats", "history", "users"))
                return
        except Exception as exc:
            log.exception("module %s failed", module.__name__)
            db.log("module", f"{module.__name__}: {type(exc).__name__}: {exc}")
    if not edited:
        await misc.speaker(ctx)
    db.save(("groups", "stats", "history", "users"))


@app.on_message(incoming=True)
async def on_message(message):
    try:
        await dispatch(message)
    except Exception as exc:
        log.exception("message dispatch error: %s", exc)


@app.on_callback()
async def on_callback(event):
    try:
        if await fun.handle_callback(event, api):
            return
    except Exception as exc:
        log.exception("callback handler failed: %s", exc)
        try:
            await event.answer("خطا در پردازش دکمه.", alert=True)
        except Exception:
            pass


@app.on_edited(incoming=True)
async def on_edited(message):
    try:
        await dispatch(message, edited=True)
    except Exception as exc:
        log.exception("edited dispatch error: %s", exc)


@app.on_chat_action()
async def on_chat(event):
    try:
        await on_chat_action(event)
    except Exception as exc:
        log.debug("chat action failed: %s", exc)


async def boot():
    global _BOT_TASK
    me = await api.me()
    ME["guid"] = str(api_mod.guid_of(me) or config.BOT_GUID)
    ME["name"] = api_mod.name_of(me)
    config.BOT_GUID = ME["guid"]
    try:
        import spluslib
        log.info("SplusLib version: %s", getattr(spluslib, "__version__", "unknown"))
    except Exception:
        pass
    log.info("وارد حساب شدیم: %s (%s)", ME["name"], ME["guid"])
    print(f"  ✅ اتصال برقرار شد — {ME['name']} | {ME['guid']}")
    log.info("SUDOS: %s شناسه", len(config.SUDOS))
    await webpanel.start(app, api, ME["guid"], dispatch)
    if getattr(config, "FORCE_JOIN_ENABLED", False) and getattr(config, "FORCE_JOIN_REQUIRE_BOT_ADMIN", True):
        try:
            channel_admin = await api.is_channel_admin(config.FORCE_JOIN_CHANNEL_ID, ME["guid"])
            if channel_admin:
                log.info("force-join channel admin verified: %s", config.FORCE_JOIN_CHANNEL_ID)
            else:
                log.warning("FBI account is not verified as admin in force-join channel %s", config.FORCE_JOIN_CHANNEL_ID)
        except Exception as exc:
            log.warning("force-join channel admin check failed: %r", exc)
    try:
        await repair_saved_mute_permissions_on_boot()
    except Exception as exc:
        log.debug("initial DB mute permission repair failed: %r", exc)
    try:
        await restore_all_active_mutes_once(force=True)
    except Exception as exc:
        log.debug("initial mute restore failed: %r", exc)
    try:
        await restore_closed_groups_once(force=True)
    except Exception as exc:
        log.debug("initial group permission restore failed: %r", exc)
    import atexit as _atexit
    db.defer_writes = True
    _atexit.register(db.flush)
    asyncio.create_task(db_flush_loop())
    _BOT_TASK = asyncio.create_task(auto_activation_loop())
    asyncio.create_task(timers.run_timers(dispatch))
    asyncio.create_task(group_unlock_loop())


def normalize_phone(value: str) -> str:
    value = (value or "").strip()
    trans = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")
    value = value.translate(trans).replace(" ", "").replace("-", "")
    if value.startswith("00"):
        value = "+" + value[2:]
    elif value.startswith("09"):
        value = "+98" + value[1:]
    elif value.startswith("98"):
        value = "+" + value
    return value


def login_phone():
    phone = normalize_phone(config.PHONE_NUMBER)
    if not phone:
        print("\n  📱 شماره سروش‌پلاس را وارد کنید (مثال: +989123456789)")
        phone = normalize_phone(input("  شماره: "))
    if not re.fullmatch(r"\+[1-9]\d{9,14}", phone):
        raise SystemExit("شماره تلفن نامعتبر است؛ نمونه: +989123456789")
    return phone


async def run():
    splash.show(bot_name=config.BOT_NAME, maker=config.MAKER, version=config.VERSION, modules=len(MODULES), session_name=config.SESSION_NAME, sudos=len(config.SUDOS))
    phone = login_phone()
    print(f"  📲 شماره ورود: {phone}")
    await app.start(phone, password=(config.PASSWORD or None))
    try:
        await boot()
        await app.run_until_disconnected()
    finally:
        task = globals().get("_BOT_TASK")
        if task and not task.done():
            task.cancel()
        await webpanel.stop()
        try:
            await app.stop()
        except Exception:
            pass
        db.flush()


if __name__ == "__main__":
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        db.flush()
        print("\nخاموش شد. اطلاعات ذخیره شد ✅")
