# -*- coding: utf-8 -*-
"""آمار فرد، گروه، فعالیت و گزارش‌ها با پروفایل قابل کلیک."""
from __future__ import annotations

import os
import re
import asyncio
import copy
import time

import config
from core import api as api_mod
from core import ranks, utils
from core.db import db


async def handle(ctx) -> bool:
    text = ctx.text
    g = ctx.g
    from core import utils as _u
    if _u.normalize(text) in (_u.normalize("لیست برترین‌ها"), _u.normalize("لیست برترین ها")):
        from core.db import db as _db
        rows = _db.top_users(ctx.guid, 10)
        if not rows:
            await ctx.reply("هنوز پیامی برای رتبه‌بندی ثبت نشده است.")
            return True
        lines = ["🏆 پرفعال‌ترین اعضای گروه:"]
        for i, (uid, total) in enumerate(rows, 1):
            name = await ctx.target_link(uid)
            lines.append(f"{_u.fa_digits(i)}. {name} — {_u.fa_digits(total)} پیام")
        await ctx.reply("\n".join(lines))
        return True
    if text in ("آمارم", "امارم"):
        await _user_stat(ctx, ctx.author)
        return True
    if text in ("آمار", "امار"):
        target = await ctx.target() or ctx.author
        if not target:
            await ctx.err("روی پیام کاربر ریپلای بزنید.")
            return True
        await _user_stat(ctx, target)
        return True
    if text in ("آمارش", "امارش"):
        target = await ctx.target()
        if not target:
            await ctx.err("روی پیام کاربر ریپلای بزنید.")
            return True
        await _user_stat(ctx, target)
        return True
    if text in ("آمار گروه", "امار گروه", "آمار گپ", "امار گپ"):
        if ctx.rank < ranks.ADMIN:
            return await _deny(ctx)
        await _group_stat(ctx, 0, "آمار کلی گروه")
        return True
    if text in ("آمار هفتگی", "امار هفتگی"):
        if ctx.rank < ranks.ADMIN:
            return await _deny(ctx)
        await _group_stat(ctx, 7, "آمار هفتگی گروه")
        return True
    if text == "اسکن همه":
        if ctx.rank < ranks.ADMIN:
            return await _deny(ctx)
        await _scan_all(ctx)
        return True
    if text == "فعالیت":
        if ctx.rank < ranks.ADMIN:
            return await _deny(ctx)
        await _activity(ctx)
        return True
    if text in ("گزارشات", "گزارش"):
        if ctx.rank < ranks.ADMIN:
            return await _deny(ctx)
        await _reports(ctx)
        return True
    # Last: «آمار <username>». Placed after the group and weekly commands, otherwise
    # «آمار گروه» and «آمار هفتگی» were read as usernames (4+ letters).
    m = re.match(r"^(?:آمار|امار) (\S{4,})$", text)
    if m:
        await _user_stat(ctx, m.group(1))
        return True
    return False


async def _scan_all(ctx):
    try:
        rows = await ctx.api.scan_all_messages(ctx.guid, max_messages=getattr(config, "SCAN_ALL_MAX_MESSAGES", 100000))
    except Exception as exc:
        await ctx.err(f"اسکن تاریخچه انجام نشد: {type(exc).__name__}: {exc}")
        return
    old = db.data.get("stats", {}).get(ctx.guid, {}) or {}
    now = time.time()
    today = time.strftime("%Y-%m-%d", time.localtime(now))
    week_start = time.strftime("%Y-%m-%d", time.localtime(now - 6 * 86400))
    rebuilt = {}
    seen_ids = []
    for msg in rows:
        mid = api_mod._val(msg, "id", "message_id", default=None)
        sender = api_mod.sender_guid_of(msg)
        if mid is None or sender is None:
            continue
        sender = str(sender)
        raw_ts = api_mod._val(msg, "timestamp", "date", "time", default=0)
        try:
            ts = float(raw_ts or 0)
            if ts > 10**11:
                ts /= 1000.0
            if ts <= 0:
                ts = now
        except Exception:
            ts = now
        day = time.strftime("%Y-%m-%d", time.localtime(ts))
        st = rebuilt.setdefault(sender, {
            "total": 0, "today": 0, "day": today, "week": {},
            "deleted": int((old.get(sender) or {}).get("deleted", 0)),
            "edited": int((old.get(sender) or {}).get("edited", 0)),
            "media": 0, "text": 0, "joined": int((old.get(sender) or {}).get("joined", 0)),
            "left": int((old.get(sender) or {}).get("left", 0)), "warn": int((old.get(sender) or {}).get("warn", 0)),
            "last": 0,
            "clean_today": 0, "clean_day": today,
            "loyalty_level": int((old.get(sender) or {}).get("loyalty_level", 0)),
            "loyalty_granted_at": float((old.get(sender) or {}).get("loyalty_granted_at", 0) or 0),
            "loyalty_last_prompt_day": str((old.get(sender) or {}).get("loyalty_last_prompt_day", "") or ""),
            "loyalty_reason": str((old.get(sender) or {}).get("loyalty_reason", "") or ""),
            "loyalty_exempt": bool((old.get(sender) or {}).get("loyalty_exempt", False)),
        })
        st["total"] += 1
        if day == today:
            st["today"] += 1
        if day >= week_start:
            st["week"][day] = int(st["week"].get(day, 0)) + 1
        if ts > st["last"]:
            st["last"] = ts
        raw_text = str(api_mod._val(msg, "raw_text", "message", "text", default="") or "").strip()
        is_media = any(bool(api_mod._val(msg, a, default=False)) for a in ("photo", "video", "gif", "voice", "audio", "music", "file", "document", "sticker"))
        if is_media:
            st["media"] += 1
        elif raw_text:
            st["text"] += 1
        # Rebuild the daily clean-message counter used by loyalty.
        if day == today and raw_text:
            phone_re = getattr(utils, "PHONE_RE", None)
            has_phone = bool(phone_re.search(raw_text)) if phone_re is not None else False
            forwarded = bool(api_mod._val(msg, "is_forward", "forward", default=False))
            if (not utils.has_link(raw_text) and not utils.has_username(raw_text)
                    and not has_phone and not forwarded):
                st["clean_today"] += 1
        seen_ids.append(str(mid))
    db.data["stats"][ctx.guid] = rebuilt
    g = ctx.g
    cap = int(getattr(config, "SCAN_ALL_MAX_MESSAGES", 100000))
    g["scanned_message_ids"] = seen_ids[-cap:]
    g["scan_all_last"] = now
    db.save(("groups", "stats"))
    await ctx.reply(
        "🔎 اسکن تاریخچه کامل شد.\n\n"
        f"💬 پیام‌های پیدا‌شده: {utils.fa_num(len(seen_ids))}\n"
        f"👤 کاربران دارای آمار: {utils.fa_num(len(rebuilt))}\n"
        f"📅 پیام‌های امروز: {utils.fa_num(sum(int(x.get('today', 0)) for x in rebuilt.values()))}\n"
        f"🕐 آخرین اسکن: {utils.stamp(now)}"
    )


async def _user_stat(ctx, guid):
    if isinstance(guid, str) and guid.startswith("@"):
        resolved = await ctx.api.resolve_user_guid(guid, chat_hint=ctx.guid if ctx.is_group else None)
        guid = resolved or guid
    if not guid:
        await ctx.err("شناسه کاربر پیدا نشد.")
        return
    guid = str(guid)
    st = db.stat(ctx.guid, guid)
    user = db.user(guid)
    info = await ctx.api.user_info(guid, chat_hint=ctx.guid, force=True)
    name = api_mod.name_of(info) if info else user.get("name") or "کاربر"
    username = api_mod._val(info, "username", "user_name", default=None) if info else user.get("username", "")
    bio = api_mod._val(info, "bio", "about", default=None) if info else user.get("bio", "")
    if name != "کاربر":
        user["name"] = name
    if username:
        user["username"] = username
    if bio is not None:
        user["bio"] = bio
    db.save("users")
    link = await ctx.target_link(guid, fallback_name=name)
    level = ranks.name(ranks.rank(ctx.guid, guid))
    warns = ctx.g.get("warns", {}).get(guid, 0)
    week = sum(st.get("week", {}).values())
    caption = "\n".join([
        f"📊 پروفایل و آمار {link}", "",
        f"🆔 `{guid}`",
        f"👤 نام کاربری: @{str(username).lstrip('@')}" if username else "👤 نام کاربری: —",
        f"📝 بیو: {bio or '—'}",
        f"🎖 مقام: {level}",
        f"🏷 لقب: {user.get('nick') or '—'}",
        f"🌿 اصل: {user.get('title') or '—'}", "",
        f"💬 کل پیام‌ها: {utils.fa_num(st.get('total', 0))}",
        f"📅 امروز: {utils.fa_num(st.get('today', 0))}",
        f"🗓 ۷ روز اخیر: {utils.fa_num(week)}",
        f"🗑 حذف‌شده: {utils.fa_num(st.get('deleted', 0))}",
        f"✏️ ویرایش‌شده: {utils.fa_num(st.get('edited', 0))}",
        f"🖼 رسانه: {utils.fa_num(st.get('media', 0))}",
        f"🌿 درجه وفاداری: {'وفادار ⭐' if int(st.get('loyalty_level', 0) or 0) > 0 else 'عادی'}",
        f"🧼 پیام پاک امروز: {utils.fa_num(st.get('clean_today', 0))} / {utils.fa_num(getattr(config, 'LOYALTY_DAILY_THRESHOLD', 500))}",
        f"🛡 معافیت وفاداری: {'فعال ✅' if st.get('loyalty_exempt') else 'ندارد'}",
        f"⚠️ اخطار: {utils.fa_digits(warns)} از {utils.fa_digits(ctx.g.get('max_warn', 5))}",
        f"⏰ آخرین فعالیت: {utils.stamp(st.get('last')) if st.get('last') else '—'}",
    ])
    avatar = await _avatar(ctx, guid)
    if avatar:
        try:
            await ctx.api.send_photo(ctx.guid, avatar, caption, ctx.message_id)
        finally:
            try:
                os.remove(avatar)
            except OSError:
                pass
    else:
        await ctx.reply(caption)


async def _avatar(ctx, guid):
    path = os.path.join(config.TEMP_DIR, f"profile_{str(guid)[:20]}.jpg")
    res = await ctx.api.download_avatar(guid, path, chat_hint=ctx.guid)
    return path if res and os.path.isfile(path) and os.path.getsize(path) > 100 else None


def _loyalty_clean_message(ctx) -> bool:
    """Strict, lock-independent definition of a clean message for loyalty."""
    if not ctx.author or ctx.rank != ranks.USER:
        return False
    text = str(getattr(ctx, "raw_text", "") or "").strip()
    if not text:
        return False
    if utils.has_link(text) or utils.has_username(text):
        return False
    phone_re = getattr(utils, "PHONE_RE", None)
    if phone_re is not None:
        try:
            if phone_re.search(text):
                return False
        except Exception:
            pass
    if bool(getattr(ctx.update, "is_forward", False) or getattr(ctx.update, "forward", False)):
        return False
    return True


def _survey_keyboard():
    # Reply keyboard fallback for SplusLib builds that don't expose native polls.
    try:
        from spluslib._base import Button
        return [[Button(text="بله ✅"), Button(text="خیر ❌")]]
    except Exception:
        return None


async def _start_loyalty_survey(ctx, user_guid):
    user_guid = str(user_guid)
    g = db.group(ctx.guid)
    active = g.setdefault("loyalty_surveys", {})
    if user_guid in active:
        return
    try:
        info = await ctx.api.user_info(user_guid, chat_hint=ctx.guid, force=True)
        name = api_mod.name_of(info) if info else "کاربر"
    except Exception:
        name = "کاربر"
    question = (
        f"🌿 آیا {name} به‌دلیل فعالیت سالم و ثبت بیش از "
        f"{utils.fa_num(getattr(config, 'LOYALTY_DAILY_THRESHOLD', 500))} پیام پاک امروز، معاف شود؟"
    )
    now = time.time()
    entry = {
        "user_guid": user_guid, "question": question, "created_at": now,
        "expires_at": now + int(getattr(config, "LOYALTY_SURVEY_SECONDS", 600)),
        "message_id": None, "poll_id": None, "mode": "native", "votes": {},
    }
    active[user_guid] = entry
    db.save("groups")
    try:
        result = await ctx.api.send_poll(
            ctx.guid, question, ["بله ✅", "خیر ❌"],
            quiz=False, is_anonymous=False, multiple_choice=False,
            close_period=int(getattr(config, "LOYALTY_POLL_CLOSE_SECONDS", 600)),
            reply_to=ctx.message_id or None,
        )
        if not isinstance(result, dict) or not result.get("native"):
            raise RuntimeError("loyalty survey was not sent as a native poll")
        entry["message_id"] = ctx.api.result_message_id(result)
        entry["poll_id"] = result.get("poll_id")
        if not entry["message_id"]:
            raise RuntimeError("native loyalty poll has no message id")
    except Exception as exc:
        active.pop(user_guid, None)
        st = db.stat(ctx.guid, user_guid)
        st["loyalty_last_prompt_day"] = ""
        db.save(("groups", "stats"))
        db.log("loyalty", f"native poll send failed chat={ctx.guid} user={user_guid}: {type(exc).__name__}: {exc}")
        try:
            await ctx.err("ارسال نظرسنجی وفاداری واقعی ناموفق بود؛ دسترسی ارسال Poll را بررسی کنید.")
        except Exception:
            pass
        return False
    db.save("groups")
    return True


async def finalize_loyalty_survey(ctx, user_guid, entry=None):
    user_guid = str(user_guid)
    g = db.group(ctx.guid)
    active = g.setdefault("loyalty_surveys", {})
    item = entry or active.get(user_guid)
    if not item:
        return False
    if time.time() < float(item.get("expires_at") or 0):
        return False
    counts = None
    if item.get("mode") == "native" and item.get("message_id"):
        try:
            counts = await ctx.api.get_poll_results(ctx.guid, item["message_id"])
        except Exception as exc:
            db.log("loyalty", f"poll readback failed chat={ctx.guid} user={user_guid}: {exc}")
    if counts is None:
        votes = item.get("votes") or {}
        yes = sum(1 for v in votes.values() if v == "yes")
        no = sum(1 for v in votes.values() if v == "no")
        counts = {"yes": yes, "no": no, "total": yes + no, "counts": [yes, no]}
    yes = int(counts.get("yes", 0))
    no = int(counts.get("no", 0))
    passed = yes > no
    st = db.stat(ctx.guid, user_guid)
    st["loyalty_level"] = max(1, int(st.get("loyalty_level", 0) or 0))
    st["loyalty_exempt"] = bool(passed)
    st["loyalty_reason"] = "۵۰۰+ پیام پاک و رأی مثبت" if passed else "۵۰۰+ پیام پاک؛ رأی مثبت بیشتر نشد"
    st["loyalty_granted_at"] = float(st.get("loyalty_granted_at") or time.time())
    if passed:
        exempt = [str(x) for x in (g.get("exempt") or [])]
        if user_guid not in exempt:
            exempt.append(user_guid)
        g["exempt"] = exempt
    active.pop(user_guid, None)
    db.save(("groups", "stats"))
    try:
        link = await ctx.target_link(user_guid, fallback_name="کاربر")
    except Exception:
        link = f"`{user_guid}`"
    if passed:
        text = f"🌿 رأی‌گیری وفاداری برای {link} تمام شد.\n✅ بله: {utils.fa_num(yes)}\n❌ خیر: {utils.fa_num(no)}\n🛡 معافیت فعال شد."
    else:
        text = f"🌿 رأی‌گیری وفاداری برای {link} تمام شد.\n✅ بله: {utils.fa_num(yes)}\n❌ خیر: {utils.fa_num(no)}\nℹ️ معافیت فعال نشد."
    await ctx.reply(text)
    return True


async def handle_loyalty_vote(ctx) -> bool:
    if not ctx.author or not ctx.is_group:
        return False
    g = db.group(ctx.guid)
    text = str(ctx.text or "").strip()
    normalized = utils.normalize(text).replace("✅", "").replace("❌", "").strip()
    choice = "yes" if normalized == utils.normalize("بله") else "no" if normalized == utils.normalize("خیر") else None
    if choice is None:
        return False
    now = time.time()
    for target, item in list((g.get("loyalty_surveys") or {}).items()):
        if now >= float(item.get("expires_at") or 0):
            continue
        votes = item.setdefault("votes", {})
        voter = str(ctx.author)
        if voter in votes:
            await ctx.reply("ℹ️ رأی شما قبلاً ثبت شده است.")
            return True
        votes[voter] = choice
        db.save("groups")
        await ctx.reply("✅ رأی شما ثبت شد؛ نتیجه بعد از پایان ۱۰ دقیقه اعلام می‌شود.")
        return True
    return False


async def finalize_pending_loyalty_surveys(app, api):
    now = time.time()
    done = 0
    for guid, g in list(db.data.get("groups", {}).items()):
        if not g.get("active"):
            continue
        active = g.get("loyalty_surveys") or {}
        if not active:
            continue
        fake = __import__("core.context", fromlist=["Ctx", "make_update"]).Ctx(
            app, api,
            __import__("types").SimpleNamespace(
                object_guid=str(guid), author_guid=None, message_id="", text="",
                is_group=True, is_private=False, is_channel=False
            ),
            getattr(config, "BOT_GUID", "62989530")
        )
        fake.g = g
        fake.rank = ranks.BOT
        for uid, entry in list(active.items()):
            if now >= float(entry.get("expires_at") or 0):
                try:
                    if await finalize_loyalty_survey(fake, uid, entry):
                        done += 1
                except Exception as exc:
                    db.log("loyalty", f"finalize failed chat={guid} user={uid}: {type(exc).__name__}: {exc}")
    return done


async def record_clean_message(ctx):
    """Track clean daily messages and create exactly one daily loyalty survey."""
    if not _loyalty_clean_message(ctx):
        return False
    st = db.stat(ctx.guid, str(ctx.author))
    today = db._today()
    if st.get("clean_day") != today:
        st["clean_day"] = today
        st["clean_today"] = 0
    st["clean_today"] = int(st.get("clean_today", 0) or 0) + 1
    threshold = int(getattr(config, "LOYALTY_DAILY_THRESHOLD", 500))
    prompt_day = str(st.get("loyalty_last_prompt_day") or "")
    active = db.group(ctx.guid).setdefault("loyalty_surveys", {})
    should_prompt = (
        st["clean_today"] > threshold and prompt_day != today and
        not bool(st.get("loyalty_exempt")) and str(ctx.author) not in active
    )
    if should_prompt:
        st["loyalty_level"] = max(1, int(st.get("loyalty_level", 0) or 0))
        st["loyalty_granted_at"] = time.time()
        st["loyalty_last_prompt_day"] = today
        st["loyalty_reason"] = "بیش از ۵۰۰ پیام پاک در روز"
        db.save(("stats", "groups"))
        asyncio.create_task(_start_loyalty_survey(ctx, str(ctx.author)))
    else:
        db.save("stats")
    return should_prompt


def _as_count(value) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


async def _group_stat(ctx, days, title):
    """Group summary: identity, invite link, photo, member counts, message totals, top five.

    days=0: all-time totals, ranking by all-time messages (counted since the bot was activated).
    days=1: today. days=7: the last seven days. The ranking follows the period.
    """
    today = time.strftime("%Y-%m-%d")
    stats = {k: v for k, v in db.stat(ctx.guid).items() if isinstance(v, dict)}

    # Roles: refreshes the stored owner and admins from the live group.
    snap = None
    try:
        snap = await ctx.api._role_snapshot(ctx.guid, fresh=True)
        manual_owner = bool(ctx.g.get("owner_verified") and ctx.g.get("owner_source") == "manual_owner"
                            and ctx.g.get("owner"))
        if not manual_owner:
            if snap.get("owner") is not None and snap.get("owner_verified"):
                ctx.g["owner"] = str(snap["owner"])
                ctx.g["owner_verified"] = True
                ctx.g["owner_source"] = "live_creator"
            elif snap.get("role_scan_ok"):
                ctx.g["owner"] = None
                ctx.g["owner_verified"] = False
                ctx.g["owner_source"] = ""
        if snap.get("role_scan_ok"):
            ctx.g["admins"] = [x for x in snap.get("admins", []) if str(x) != str(ctx.g.get("owner"))]
        db.save("groups")
    except Exception:
        snap = None

    # Live group data: title, members, online count, invite link.
    live = None
    try:
        live = await ctx.api.group_info(ctx.guid)
    except Exception:
        live = None
    if not isinstance(live, dict):
        live = None

    def live_value(*keys):
        if not live:
            return None
        return api_mod.deep(live, *keys, default=None)

    live_title = live_value("group", "group_title") or live_value("chat", "title")
    if live_title and str(live_title).strip():
        ctx.g["title"] = str(live_title).strip()
    group_title = str(ctx.g.get("title") or "گروه")

    members = _as_count(live_value("group", "count_members")) or _as_count(live_value("chat", "participants_count"))
    if not members and snap:
        members = _as_count(snap.get("member_count")) or len(snap.get("members") or [])
    if not members:
        try:
            rows = api_mod.members_of(await ctx.api.call(
                "get_chat_members", ctx.api._chat_arg(ctx.guid), limit=5000, filter_admins=False))
            members = len(rows)
        except Exception:
            members = 0
    online = _as_count(live_value("group", "count_online")) or _as_count(live_value("chat", "online_count"))

    link = str((live or {}).get("invite_link") or "")
    if not link:
        try:
            link = str((await ctx.api.group_link(ctx.guid)).get("link") or "")
        except Exception:
            link = ""

    # Group photo. The file is overwritten on each call, so the temp folder stays small.
    photo = None
    try:
        path = os.path.join(config.TEMP_DIR, f"group_{str(ctx.guid)[:20]}.jpg")
        await ctx.api.download_chat_avatar(ctx.guid, path)
        if os.path.isfile(path) and os.path.getsize(path) > 100:
            photo = path
    except Exception:
        photo = None

    # Counters.
    def period_count(st):
        if days == 0:
            return _as_count(st.get("total"))
        if days == 1:
            return _as_count(st.get("today")) if st.get("day") == today else 0
        return _as_count(sum((st.get("week") or {}).values()))

    scores = {uid: period_count(st) for uid, st in stats.items()}
    top = [(uid, n) for uid, n in sorted(scores.items(), key=lambda x: (-x[1], str(x[0]))) if n > 0][:5]
    all_total = sum(_as_count(st.get("total")) for st in stats.values())
    today_total = sum(_as_count(st.get("today")) for st in stats.values() if st.get("day") == today)
    week_total = sum(_as_count(sum((st.get("week") or {}).values())) for st in stats.values())
    senders = sum(1 for st in stats.values() if _as_count(st.get("total")) > 0)
    deleted = sum(_as_count(st.get("deleted")) for st in stats.values())
    edited = sum(_as_count(st.get("edited")) for st in stats.values())
    media = sum(_as_count(st.get("media")) for st in stats.values())

    owner_link = owner_plain = "شناسایی نشد"
    if ctx.g.get("owner_verified") and ctx.g.get("owner"):
        try:
            owner_link = await ctx.target_link(ctx.g["owner"])
        except Exception:
            owner_link = f"`{ctx.g['owner']}`"
        try:
            owner_plain = str(await ctx.target_name(ctx.g["owner"]) or ctx.g["owner"])
        except Exception:
            owner_plain = str(ctx.g["owner"])

    # Reply text keeps clickable mentions; the photo caption is plain text.
    top_rows = []
    for uid, score in top:
        try:
            link_label = await ctx.target_link(uid)
        except Exception:
            link_label = str(uid)
        try:
            plain_label = str(await ctx.target_name(uid) or uid)
        except Exception:
            plain_label = str(uid)
        top_rows.append((link_label, plain_label, score))

    def render(owner_label, plain):
        line = "━━━━━━━━━━━━━━━━━━━━"
        out = [f"📊 {group_title} — {title}", line]
        out.append(f"🔗 لینک گروه: {link}" if link else "🔗 لینک گروه: دریافت نشد")
        out.append(f"👑 مالک واقعی: {owner_label}")
        members_line = f"👥 اعضای گروه: {utils.fa_num(members)}" if members else "👥 اعضای گروه: نامشخص"
        if online:
            members_line += f" | 🟢 آنلاین: {utils.fa_num(online)}"
        out.append(members_line)
        out.append(
            f"🛡 ادمین‌ها: {utils.fa_digits(len(ctx.g.get('admins', [])))} | "
            f"⭐ ویژه‌ها: {utils.fa_digits(len(ctx.g.get('specials', [])))} | "
            f"🧩 معاف‌ها: {utils.fa_digits(len(ctx.g.get('exempt', [])))} | "
            f"🔇 سکوت‌ها: {utils.fa_digits(len(ctx.g.get('mute', {})))}"
        )
        out.append(line)
        out.append(f"💬 کل پیام‌ها (از فعال‌سازی ربات): {utils.fa_num(all_total)}")
        out.append(f"📅 امروز: {utils.fa_num(today_total)} | ۷ روز اخیر: {utils.fa_num(week_total)}")
        out.append(f"👤 پیام‌دهنده‌ها: {utils.fa_num(senders)}")
        out.append(f"🗑 حذف‌شده: {utils.fa_num(deleted)} | ✏️ ویرایش‌شده: {utils.fa_num(edited)} | 🖼 رسانه: {utils.fa_num(media)}")
        out.append(line)
        if top_rows:
            label = {0: "کل", 1: "امروز", 7: "۷ روز اخیر"}.get(days, "کل")
            out.append(f"🏆 ۵ نفر پرچت ({label}):")
            for i, (link_label, plain_label, score) in enumerate(top_rows):
                name = plain_label if plain else link_label
                medal = ["🥇", "🥈", "🥉"][i] if i < 3 else "▫️"
                out.append(f"{medal} {name} — {utils.fa_num(score)} پیام")
        else:
            out.append("🏆 در این بازه هنوز پیامی ثبت نشده است.")
        return "\n".join(out)

    text = render(owner_link, plain=False)
    caption = render(owner_plain, plain=True)
    if photo and not ctx.g.get("sleep"):
        try:
            if len(caption) <= 1000:
                await ctx.api.send_photo(ctx.guid, photo, caption=caption)
                return
            # Too long for a caption: photo with the title, full summary as text below.
            await ctx.api.send_photo(ctx.guid, photo, caption=caption.split("\n")[0])
            await ctx.reply(text)
            return
        except Exception:
            pass
    await ctx.reply(text)


async def _activity(ctx):
    stats = db.stat(ctx.guid); today = time.strftime("%Y-%m-%d")
    total = senders = 0
    agg = {k: 0 for k in ("deleted","edited","media","text","joined","left")}
    for st in stats.values():
        if st.get("day") == today and st.get("today"):
            senders += 1; total += st.get("today", 0)
        for k in agg: agg[k] += st.get(k, 0)
    info = await ctx.api.group_info(ctx.guid)
    members = api_mod.deep(info, "group", "count_members", default=0) or 0
    await ctx.reply("\n".join(["📌 فعالیت امروز گروه", "", f"👥 اعضا: {utils.fa_num(members)}", f"🗣 پیام‌دهندگان: {utils.fa_num(senders)}", f"💬 پیام‌ها: {utils.fa_num(total)}", f"🗑 حذف‌شده: {utils.fa_num(agg['deleted'])}", f"✏️ ویرایش‌شده: {utils.fa_num(agg['edited'])}", f"🖼 رسانه: {utils.fa_num(agg['media'])}", f"📝 متن: {utils.fa_num(agg['text'])}", f"➕ عضو: {utils.fa_num(agg['joined'])}", f"➖ خارج‌شده: {utils.fa_num(agg['left'])}", "", utils.date_text()]))


async def _reports(ctx):
    stats = db.stat(ctx.guid); agg = {k:0 for k in ("text","media","deleted","edited")}
    for st in stats.values():
        for k in agg: agg[k] += st.get(k,0)
    total = max(1, agg['text'] + agg['media'])
    def bar(n): return "█" * int(20*n/total) or "▫️"
    await ctx.reply("\n".join(["📑 گزارش نوع پیام‌ها", "", f"📝 متن: {utils.fa_num(agg['text'])}\n{bar(agg['text'])}", f"🖼 رسانه: {utils.fa_num(agg['media'])}\n{bar(agg['media'])}", "", f"🗑 حذف‌شده: {utils.fa_num(agg['deleted'])}", f"✏️ ویرایش‌شده: {utils.fa_num(agg['edited'])}"]))


async def _deny(ctx):
    await ctx.err("این دستور فقط برای ادمین‌ها، ادمین‌های ویژه و مالک قابل اجراست.")
    return True
