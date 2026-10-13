# -*- coding: utf-8 -*-
"""پنل وب گروه‌های تحت مالکیت برای FBI SPLUS.

All requests are authorized by a short-lived, per-group owner token. The token
is stored as a SHA-256 digest only; commands are routed to the existing FBI
command dispatcher, so this panel does not create a second moderation engine.
"""
from __future__ import annotations

import hashlib
import html
import logging
import re
import secrets
import time
import asyncio
import shutil
import socket
import json
import os
import platform
import tarfile
import urllib.error
import urllib.request
from types import SimpleNamespace
from urllib.parse import quote, urlencode

import config
from core import ranks, utils, subscription
from core.context import Ctx
from core.db import db

log = logging.getLogger("fbi.webpanel")

try:
    from aiohttp import web
except Exception:  # pragma: no cover - requirements.txt installs aiohttp
    web = None

_CLIENT = None
_API = None
_ME_GUID = None
_DISPATCH = None
_RUNNER = None
_SELECTIONS: dict[str, dict] = {}
_DYNAMIC_BASE_URL = ""
_TUNNEL_PROCESS = None
_TUNNEL_TASK = None
_TUNNEL_EVENT = None
_TUNNEL_SUPERVISOR_TASK = None
_PUBLIC_TUNNEL_READY = None
_PUBLIC_TUNNEL_HEALTHY = False
_TUNNEL_REGISTERED = False      # cloudflared reported a successful connector registration
_LAST_PROBE_ERROR = ""          # why the last public /health probe failed (owner-facing hint)
_LAST_TUNNEL_ERROR = ""         # last ERR/WRN line printed by cloudflared

# Internal setting key -> Persian label.
FEATURES = {
    "sleep": "حالت خواب",
    "fun": "سرگرمی",
    "manage_mode": "حالت مدیریت",
    "welcome": "خوشامدگویی",
    "goodbye": "خداحافظی",
    "speaker": "سخنگو",
    "talkative": "پرحرفی",
    "learning": "یادگیری",
    "auto_delete": "حذف خودکار پیام‌های بات",
    "bio_link_mute": "محدودیت لینک در بیو",
    "ai_chat": "گفت‌وگوی هوش مصنوعی",
    "ai_behavior": "رفتار هوش مصنوعی",
    "sassy": "لحن شوخ",
}

LOCK_LABELS = {
    "link": "لینک", "username": "آیدی / یوزرنیم", "forward": "فوروارد",
    "hyperlink": "هایپرلینک", "photo": "عکس", "video": "ویدیو",
    "gif": "گیف", "voice": "ویس", "music": "موزیک", "file": "فایل",
    "sticker": "استیکر", "poll": "نظرسنجی", "contact": "مخاطب",
    "location": "موقعیت", "text": "متن", "spam": "اسپم",
    "badword": "کلمه نامناسب", "english": "انگلیسی", "phone": "شماره",
    "story": "استوری", "post": "پست", "obscene": "محتوای نامناسب",
    "edit": "ویرایش", "mention": "تگ / منشن", "file_size": "فایل حجیم",
    "hang_code": "کد هنگ‌کننده",
}

MODERATION_ACTIONS = {
    "mute": "سکوت کاربر", "unmute": "رفع سکوت", "ban": "بن کاربر",
    "unban": "رفع بن", "warn": "اخطار", "clear_warn": "حذف اخطارها",
    "block": "افزودن به بلاک", "unblock": "حذف از بلاک",
    "kill": "کیل (حذف با ورود مجدد)", "unkill": "لغو کیل",
    "exempt": "معاف کردن", "unexempt": "لغو معافیت",
    "add_admin": "ارتقای ادمین فول", "remove_admin": "برکناری ادمین",
}


def _latin_digits(value: str) -> str:
    return str(value or "").translate(str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789"))


# CSS کمکی برای همه‌ی دستگاه‌ها: جلوگیری از زوم خودکار آیفون روی فیلدها، اندازه‌ی
# مناسب لمس، حذف افکت hover روی موبایل، پشتیبانی از Safari قدیمی و فاصله از notch.
TOUCH_CSS = """
[hidden]{display:none!important}
@supports (min-height:100dvh){body{min-height:100dvh}}
@supports (-webkit-backdrop-filter:blur(1px)) and (not (backdrop-filter:blur(1px))){header{-webkit-backdrop-filter:blur(20px)}.card{-webkit-backdrop-filter:blur(18px)}}
@media (max-width:760px),(pointer:coarse){input[type=text],input[type=number],input[type=search],input:not([type]),select,textarea{font-size:16px}}
@media (pointer:coarse){button,.btn,.check,.compact{min-height:44px}input[type=number],input[type=text],select{min-height:40px}}
@media (hover:none){.card:hover,.stat:hover,.check:hover,.lock-row:hover,button:hover,.btn:hover{transform:none!important}}
@media (max-width:760px){main{padding-left:max(12px,env(safe-area-inset-left,0px));padding-right:max(12px,env(safe-area-inset-right,0px))}}
"""

# جدول گروه‌ها در پنل سازنده: روی گوشی هر ردیف به یک کارت تبدیل می‌شود تا
# ستون وضعیت و دکمه‌های عملیات بیرون از صفحه نمانند.
CREATOR_TABLE_CSS = """
@media (max-width:700px){
.table-wrap{overflow:visible;border:0;background:none}
table{min-width:0;background:none}
thead{display:none}
#groups-body tr{display:block;margin:0 0 12px;padding:12px 14px;border:1px solid var(--edge);border-radius:16px;background:#0d162cc0}
#groups-body td{display:flex;justify-content:space-between;align-items:center;gap:10px;padding:7px 0;border:0;text-align:right}
#groups-body td::before{content:attr(data-label);color:var(--muted);font-size:12px;flex:0 0 auto}
#groups-body td.cell-title,#groups-body td.cell-actions,#groups-body td[colspan]{display:block}
#groups-body td.cell-title::before,#groups-body td.cell-actions::before{content:none}
#groups-body td.cell-title{padding-bottom:10px;border-bottom:1px solid #ffffff14}
.mini-actions{min-width:0;width:100%;margin-top:4px}
.mini-actions input{width:84px}
.mini-actions button{flex:1 1 auto;min-height:44px}
}
"""

def _real_owner(g: dict) -> str | None:
    """Return only an owner verified by the live group creator scanner."""
    live = g.get("live_owner")
    if g.get("live_owner_verified") and live not in (None, ""):
        return str(live)
    if (g.get("owner_verified") and g.get("owner_source") == "live_creator"
            and g.get("owner") not in (None, "")):
        return str(g.get("owner"))
    return None


def owned_groups(user_id: str) -> list[tuple[str, dict]]:
    """Return saved groups whose verified real owner is exactly user_id."""
    uid = str(user_id or "")
    result = []
    for guid, group in db.data.get("groups", {}).items():
        if uid and _real_owner(group) == uid:
            result.append((str(guid), group))
    result.sort(key=lambda row: (str(row[1].get("title") or "گروه نامشخص").casefold(), row[0]))
    return result


def _token_hash(token: str) -> str:
    return hashlib.sha256(str(token).encode("utf-8")).hexdigest()


def _lan_ip() -> str:
    """Best-effort LAN address for same-Wi-Fi access; not a public Internet IP."""
    sock = None
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.connect(("1.1.1.1", 80))
        value = sock.getsockname()[0]
        if value and not value.startswith("127."):
            return value
    except Exception:
        pass
    finally:
        if sock is not None:
            try:
                sock.close()
            except Exception:
                pass
    try:
        for item in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            value = item[4][0]
            if value and not value.startswith("127."):
                return value
    except Exception:
        pass
    return "127.0.0.1"


def _base_url() -> str:
    configured = str(getattr(config, "WEB_PANEL_BASE_URL", "") or "").strip().rstrip("/")
    if configured:
        return configured
    # A Quick Tunnel hostname is only usable after its public /health endpoint
    # has actually responded. cloudflared prints a hostname before its connector
    # is necessarily online; publishing it immediately can hand users a 1033 URL.
    if _DYNAMIC_BASE_URL and (
        not bool(getattr(config, "WEB_PANEL_TUNNEL_ENABLED", False))
        or _PUBLIC_TUNNEL_HEALTHY
    ):
        return _DYNAMIC_BASE_URL.rstrip("/")
    # Never issue an Internet-facing owner link that silently falls back to a
    # private LAN address while Quick Tunnel mode is enabled but disconnected.
    if bool(getattr(config, "WEB_PANEL_TUNNEL_ENABLED", False)):
        return ""
    port = int(getattr(config, "WEB_PANEL_PORT", 8080))
    return "http://%s:%d" % (_lan_ip(), port)


def issue_creator_link(user_id: str) -> str:
    """Create a short-lived, single-owner link for the /owner web dashboard."""
    uid = str(user_id or "")
    if not subscription.is_sudo(uid):
        raise PermissionError("فقط سازندهٔ تأییدشده دسترسی دارد.")
    token = secrets.token_urlsafe(36)
    ttl_hours = max(1, min(168, int(getattr(config, "WEB_PANEL_CREATOR_TOKEN_TTL_HOURS", 24))))
    user = db.user(uid)
    user["creator_web_panel_session"] = {
        "owner": uid,
        "token_hash": _token_hash(token),
        "expires_at": time.time() + ttl_hours * 3600,
        "issued_at": time.time(),
    }
    db.save("users")
    base_url = _base_url()
    if not base_url:
        # Avoid generating a broken link when the public tunnel is not ready.
        user.pop("creator_web_panel_session", None)
        db.save("users")
        raise RuntimeError("تونل عمومی آماده نیست؛ چند ثانیه صبر کنید و دوباره /owner را بفرستید.")
    return "%s/creator/%s?%s" % (base_url, quote(uid, safe="-"), urlencode({"token": token}))


def _live_group_title(live) -> str | None:
    """Extract a real title from dicts, SplusLib models, and nested API wrappers."""
    preferred = ("group_title", "chat_title", "title", "group_name", "chat_name", "display_name", "name")
    generic = {"", "گروه", "گروه نامشخص", "unknown", "chat", "group", "none", "null"}
    chat_containers = {"chat", "peer", "entity", "conversation", "channel", "chat_info"}
    seen = set()
    candidates = []

    def read_key(obj, key):
        if isinstance(obj, dict):
            if key in obj:
                return obj.get(key)
            for actual, value in obj.items():
                if str(actual).casefold() == key.casefold():
                    return value
            return None
        try:
            return getattr(obj, key, None)
        except Exception:
            return None

    def walk(obj, depth=0, scope_priority=1):
        if obj is None or depth > 7 or id(obj) in seen:
            return
        seen.add(id(obj))
        if isinstance(obj, dict):
            items = list(obj.items())
        else:
            attrs = getattr(obj, "__dict__", None)
            items = list(attrs.items()) if isinstance(attrs, dict) else []
        field_rank = {key: i for i, key in enumerate(preferred)}
        for key in preferred:
            value = read_key(obj, key)
            if isinstance(value, str) and value.strip().casefold() not in generic:
                candidates.append((depth, scope_priority, field_rank[key], value.strip()))

        # Recurse through known chat/group wrappers first; also inspect less common
        # nested model fields so slotted/renamed API models still yield their title.
        children = []
        for key, value in items:
            if isinstance(value, (dict, list, tuple)) or hasattr(value, "__dict__"):
                lowered = str(key).casefold()
                priority = 0 if lowered in chat_containers else (1 if lowered in {"group", "group_info"} else 2)
                children.append((priority, value))
        if not isinstance(obj, dict):
            for key in ("chat", "peer", "entity", "conversation", "channel", "chat_info", "group", "group_info", "data", "result", "object", "raw"):
                value = read_key(obj, key)
                if value is not None and value is not obj and (isinstance(value, (dict, list, tuple)) or hasattr(value, "__dict__")):
                    priority = 0 if key in chat_containers else (1 if key in {"group", "group_info"} else 2)
                    children.append((priority, value))
        for priority, child in sorted(children, key=lambda item: item[0]):
            walk(child, depth + 1, priority)

        if isinstance(obj, (list, tuple)):
            for child in obj[:100]:
                walk(child, depth + 1, 2)

    walk(live)
    if not candidates:
        return None
    candidates.sort(key=lambda item: (item[0], item[1], item[2]))
    return candidates[0][3]


async def _refresh_group_title(guid: str, group: dict, timeout: float = 7.0) -> str | None:
    """Best-effort refresh of a group's real live title from SplusLib."""
    if _API is None:
        current = str(group.get("title") or "").strip()
        return current if current and current.casefold() not in {"گروه", "گروه نامشخص", "unknown", "chat"} else None
    try:
        live = await asyncio.wait_for(_API.group_info(str(guid)), timeout=timeout)
        title = _live_group_title(live)
        if title and title.casefold() not in {"گروه", "گروه نامشخص", "unknown", "chat", "group", "none", "null"}:
            if str(group.get("title") or "").strip() != title:
                group["title"] = title
                db.save("groups")
            return title
    except Exception as exc:
        log.debug("live group title refresh failed guid=%s: %r", guid, exc)
    current = str(group.get("title") or "").strip()
    return current if current and current.casefold() not in {"گروه", "گروه نامشخص", "unknown", "chat"} else None


def _issue_link(guid: str, owner_id: str) -> str:
    group = db.data.get("groups", {}).get(str(guid))
    if not group or _real_owner(group) != str(owner_id):
        raise PermissionError("مالکیت زندهٔ گروه تأیید نشد.")
    token = secrets.token_urlsafe(32)
    ttl_hours = max(1, min(720, int(getattr(config, "WEB_PANEL_TOKEN_TTL_HOURS", 168))))
    group["web_panel_session"] = {
        "owner": str(owner_id),
        "token_hash": _token_hash(token),
        "expires_at": time.time() + ttl_hours * 3600,
        "issued_at": time.time(),
    }
    db.save("groups")
    base_url = _base_url()
    if not base_url:
        group.pop("web_panel_session", None)
        db.save("groups")
        raise RuntimeError("تونل عمومی آماده نیست؛ چند ثانیه صبر کنید و دوباره عدد «۱» و شماره گروه را بفرستید.")
    return "%s/group/%s?%s" % (base_url, quote(str(guid), safe="-"), urlencode({"token": token}))


def _pv_guide_text() -> str:
    return (
        "🧭 راهنمای پنل وب FBI\n\n"
        "برای دیدن گروه‌هایی که مالک واقعی آن‌ها هستید، عدد «۱» را بفرستید.\n"
        "بعد شمارهٔ گروه را از فهرست بفرستید تا لینک پنل همان گروه ارسال شود.\n\n"
        "در پنل می‌توانید قفل‌ها، تنظیمات گروه، سکوت، بن، اخطار، معافیت، "
        "ادمین‌ها و سایر دستورات موجود ربات را مدیریت کنید.\n\n"
        "🔐 لینک پنل خصوصی است؛ آن را برای دیگران نفرستید.\n"
        "🌐 آدرس عمومی فقط هنگام اتصال تونل فعال است. اگر لینک خطای 1033 داد، "
        "وضعیت اتصال ربات را بررسی کنید و پس از اتصال مجدد، عدد «۱» و شماره گروه را دوباره بفرستید."
    )


async def handle_private(ctx) -> bool:
    """Handle owner menu selections; for normal owner DMs, send an extra hint."""
    uid = str(ctx.author or "")
    if not uid:
        return False
    available = owned_groups(uid)
    text = _latin_digits(ctx.text).strip()
    # /owner is handled by the creator menu in main.py; don't add the group
    # owner's generic hint before the creator dashboard link.
    if text == "/owner" and subscription.is_sudo(uid):
        return False
    pending = _SELECTIONS.get(uid)

    if text in {"راهنمای پنل", "پنل وب", "راهنما", "/panel"} and available:
        await ctx.reply(_pv_guide_text(), reply=False)
        return True

    if pending and text.isdigit():
        if float(pending.get("expires_at") or 0) < time.time():
            _SELECTIONS.pop(uid, None)
            await ctx.reply("⌛ زمان انتخاب تمام شد؛ دوباره عدد «۱» را بفرستید.", reply=False)
            return True
        index = int(text)
        group_ids = pending.get("groups") or []
        if index < 1 or index > len(group_ids):
            await ctx.reply("شمارهٔ گروه معتبر نیست. یکی از شماره‌های فهرست را بفرستید.", reply=False)
            return True
        guid = str(group_ids[index - 1])
        group = db.data.get("groups", {}).get(guid)
        if not group or _real_owner(group) != uid:
            _SELECTIONS.pop(uid, None)
            await ctx.reply("❌ مالکیت گروه دیگر تأیید نمی‌شود؛ لطفاً دوباره فهرست را بگیرید.", reply=False)
            return True
        live_title = await _refresh_group_title(guid, group)
        try:
            link = _issue_link(guid, uid)
        except Exception as exc:
            log.warning("could not create web panel link for %s: %r", guid, exc)
            if not _base_url():
                await ctx.reply(
                    "🌐 لینک عمومی فعلاً آماده نیست؛ احتمالاً تونل قطع شده است.\n"
                    "چند ثانیه صبر کنید و پس از اتصال مجدد، عدد «۱» و شماره گروه را دوباره بفرستید.\n"
                    "🔎 " + tunnel_problem_hint(),
                    reply=False,
                )
            else:
                await ctx.reply("❌ ساخت لینک پنل انجام نشد. مالکیت گروه را دوباره بررسی کنید.", reply=False)
            return True
        title = str(live_title or group.get("title") or ("گروه · " + guid))
        await ctx.reply(
            "🌐 پنل مدیریت گروه «%s»\n\n%s\n\n"
            "🔒 این لینک به مالک تأییدشدهٔ گروه اختصاص دارد و تا %s ساعت اعتبار دارد.\n"
            "آن را عمومی نکنید. وقتی تونل HTTPS آماده باشد، لینک از اینترنت باز می‌شود؛ اگر تونل شکست خورده باشد، باید لاگ یا FBI_WEB_PANEL_BASE_URL را بررسی کنید."
            % (title, link, utils.fa_digits(max(1, min(720, int(getattr(config, "WEB_PANEL_TOKEN_TTL_HOURS", 168)))))),
            reply=False,
        )
        _SELECTIONS.pop(uid, None)
        return True

    if text == "1" and available:
        now = time.time()
        _SELECTIONS[uid] = {"groups": [guid for guid, _ in available], "expires_at": now + 600}
        lines = ["👑 گروه‌های تحت مالکیت شما", "━━━━━━━━━━━━━━━━━━━━"]
        for index, (guid, group) in enumerate(available, 1):
            title = str(group.get("title") or "گروه نامشخص")
            status = "فعال ✅" if group.get("active") else "ثبت‌شده / غیرفعال ⚪"
            lines.append("%s. %s\n   %s" % (utils.fa_digits(index), title, status))
        lines.extend(["", "برای دریافت لینک پنل، شمارهٔ گروه را بفرستید.", "فهرست تا ۱۰ دقیقه معتبر است."])
        await ctx.reply("\n".join(lines), reply=False)
        return True

    # If the user has just asked a normal PV question, don't interrupt the
    # existing PV flow: send an additional menu hint, then let main.py continue.
    if available:
        await ctx.reply("برای دیدن گروه‌های تحت مالکیت، عدد «۱» را بفرستید.\n\nبرای راهنمای کامل بنویسید: «راهنمای پنل»", reply=False)
    return False


def _lock_keys(group: dict) -> list[str]:
    keys = set(LOCK_LABELS)
    keys.update((group.get("locks") or {}).keys())
    return sorted(keys, key=lambda k: (LOCK_LABELS.get(k, k).casefold(), k))


def _checked(value: bool) -> str:
    return " checked" if value else ""


def _safe_int(value, default=0, low=None, high=None):
    try:
        result = int(value)
    except (TypeError, ValueError):
        result = int(default)
    if low is not None:
        result = max(int(low), result)
    if high is not None:
        result = min(int(high), result)
    return result


def _checkbox(name: str, label: str, checked: bool, *, compact=False) -> str:
    cls = "check compact" if compact else "check"
    return '<label class="%s"><input type="checkbox" name="%s" value="1"%s><span>%s</span></label>' % (
        cls, html.escape(name, quote=True), _checked(bool(checked)), html.escape(str(label)))


def _group_status(g: dict) -> str:
    if g.get("active") and not g.get("manual_disabled"):
        return '<span class="pill on">فعال</span>'
    return '<span class="pill off">غیرفعال</span>'


def _render_page(guid: str, group: dict, token: str, notice: str = "", members: list[dict] | None = None) -> str:
    title_value = str(group.get("title") or "").strip()
    if title_value.casefold() in {"", "گروه", "گروه نامشخص", "unknown", "chat"}:
        title_value = "گروه · " + str(guid)
    title = html.escape(title_value)
    gid = html.escape(str(guid))
    safe_token = quote(token, safe="")
    path = "/group/%s?token=%s" % (quote(str(guid), safe="-"), safe_token)
    notice_html = '<div class="notice">%s</div>' % html.escape(notice) if notice else ""
    features = "".join(_checkbox("feature_" + key, label, bool(group.get(key, False))) for key, label in FEATURES.items())
    locks = []
    for key in _lock_keys(group):
        locks.append(
            '<div class="lock-row"><div class="lock-name">%s</div><div class="lock-opt">%s</div><div class="lock-opt">%s</div><div class="lock-opt">%s</div></div>' % (
                html.escape(LOCK_LABELS.get(key, key)),
                _checkbox("lock_" + key, "فعال", bool((group.get("locks") or {}).get(key)), compact=True),
                _checkbox("lock_warn_" + key, "اخطار", (group.get("lock_warn") or {}).get(key, True), compact=True),
                _checkbox("lock_ban_" + key, "بن خودکار", (group.get("lock_ban") or {}).get(key, True), compact=True),
            )
        )
    mutes = group.get("mute") or {}
    mute_items = []
    for uid, until in list(mutes.items())[:30]:
        if until in (0, True):
            duration = "دائمی"
        else:
            remaining = max(0, int(float(until or 0) - time.time()))
            duration = "پایان‌یافته" if remaining <= 0 else utils.human_duration(remaining)
        mute_items.append('<div class="list-item"><span>%s</span><small>%s</small></div>' % (html.escape(str(uid)), html.escape(duration)))
    warn_items = []
    for uid, count in list((group.get("warns") or {}).items())[:30]:
        warn_items.append('<div class="list-item"><span>%s</span><small>%s اخطار</small></div>' % (html.escape(str(uid)), html.escape(utils.fa_digits(count))))
    admin_items = []
    for uid in list(group.get("admins") or [])[:30]:
        admin_items.append('<div class="list-item"><span>%s</span><small>ادمین</small></div>' % html.escape(str(uid)))
    lock_html = "".join(locks)
    mute_html = "".join(mute_items) or '<p class="muted">فهرست سکوت خالی است.</p>'
    warn_html = "".join(warn_items) or '<p class="muted">اخطاری ثبت نشده است.</p>'
    admin_html = "".join(admin_items) or '<p class="muted">ادمین محلی ثبت‌شده‌ای وجود ندارد.</p>'
    member_options = []
    for member in (members or []):
        member_id = str(member.get("id") or "").strip()
        if not member_id:
            continue
        full_name = (str(member.get("first_name") or member.get("name") or member.get("display_name") or "").strip() + " " + str(member.get("last_name") or "").strip()).strip()
        username = str(member.get("username") or member.get("user_name") or "").strip().lstrip("@")
        label = full_name or ("@" + username if username else "کاربر")
        role = " · مالک" if member.get("is_owner") else (" · ادمین" if member.get("is_admin") else "")
        member_options.append('<option value="%s" data-search="%s">%s — %s%s</option>' % (
            html.escape(member_id, quote=True),
            html.escape((label + " " + username + " " + member_id).casefold(), quote=True),
            html.escape(label), html.escape(member_id), html.escape(role)))
    member_select_options = "".join(member_options)
    member_count = len(member_options)
    closed = bool(group.get("closed"))
    panel_title = "پنل مدیریت FBI"
    return f'''<!doctype html>
<html lang="fa" dir="rtl"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="referrer" content="no-referrer"><title>{panel_title} | {title}</title>
<style>
:root{{--bg:#070b17;--panel:#111b31;--panel2:#192945;--line:rgba(143,177,235,.22);--text:#f1f6ff;--muted:#a5b7d5;--blue:#77a7ff;--mint:#64f1d0;--red:#ff7b9a;--violet:#ad8bff;}}
*{{box-sizing:border-box}}html{{scroll-behavior:smooth}}body{{margin:0;min-height:100vh;background:radial-gradient(ellipse at 12% 0%,rgba(77,99,205,.24),transparent 36%),radial-gradient(ellipse at 90% 18%,rgba(42,211,184,.14),transparent 32%),linear-gradient(145deg,#070b17,#0b1427 58%,#0a1020);color:var(--text);font-family:Tahoma,"Segoe UI",sans-serif;line-height:1.75;overflow-x:hidden}}
body:before,body:after{{content:"";position:fixed;z-index:-1;pointer-events:none;border-radius:50%;filter:blur(2px);opacity:.55}}body:before{{width:320px;height:320px;top:20%;left:-180px;background:radial-gradient(circle,rgba(105,115,255,.22),transparent 72%)}}body:after{{width:360px;height:360px;right:-200px;bottom:0;background:radial-gradient(circle,rgba(72,239,197,.16),transparent 72%)}}
main{{max-width:1240px;margin:auto;padding:28px 18px 52px;perspective:1400px}}header{{display:flex;align-items:center;justify-content:space-between;gap:16px;flex-wrap:wrap;margin-bottom:22px;padding:18px 20px;border:1px solid rgba(155,187,246,.19);border-radius:24px;background:linear-gradient(135deg,rgba(26,42,72,.82),rgba(12,22,41,.66));backdrop-filter:blur(20px);box-shadow:0 24px 70px rgba(0,0,0,.28),inset 0 1px rgba(255,255,255,.08)}}
.brand{{display:flex;gap:15px;align-items:center}}.logo{{width:58px;height:58px;display:grid;place-items:center;border-radius:19px;background:linear-gradient(145deg,#78a6ff 0%,#8067f5 48%,#5df0cc 100%);font-size:27px;color:#071322;font-weight:900;box-shadow:0 12px 28px rgba(87,130,255,.30),inset 0 2px 2px rgba(255,255,255,.6);transform:rotate(-5deg);border:1px solid rgba(255,255,255,.45)}}
h1{{font-size:22px;margin:0}}.subtitle{{color:var(--muted);font-size:13px}}.pill{{display:inline-block;padding:3px 12px;border-radius:99px;font-size:12px;font-weight:bold}}.pill.on{{background:#164a45;color:#7cebd1}}.pill.off{{background:#44303d;color:#ffabb7}}
.grid{{display:grid;grid-template-columns:repeat(12,minmax(0,1fr));gap:17px;transform-style:preserve-3d}}.card{{grid-column:span 12;position:relative;isolation:isolate;overflow:hidden;background:linear-gradient(145deg,rgba(27,43,72,.82),rgba(13,24,44,.88));border:1px solid var(--line);border-radius:23px;padding:22px;backdrop-filter:blur(18px);box-shadow:0 24px 55px rgba(0,0,0,.24),inset 0 1px rgba(255,255,255,.08),inset 0 -1px rgba(0,0,0,.16);transition:transform .24s ease,border-color .24s ease,box-shadow .24s ease;transform:translateZ(0)}}.card:before{{content:"";position:absolute;top:0;left:14%;right:14%;height:1px;background:linear-gradient(90deg,transparent,rgba(142,195,255,.7),transparent);opacity:.8;z-index:-1}}.card:hover{{transform:translateY(-3px) rotateX(.35deg);border-color:rgba(125,171,255,.38);box-shadow:0 30px 65px rgba(0,0,0,.3),inset 0 1px rgba(255,255,255,.1)}}
.card.half{{grid-column:span 6}}h2{{font-size:18px;margin:0 0 6px;letter-spacing:-.2px}}.desc{{color:var(--muted);font-size:13px;margin:0 0 16px}}.feature-grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(210px,1fr));gap:9px}}
.check{{display:flex;align-items:center;gap:10px;min-height:44px;padding:8px 11px;border:1px solid rgba(139,169,220,.18);background:linear-gradient(145deg,rgba(18,31,54,.86),rgba(11,22,40,.86));border-radius:13px;cursor:pointer;transition:all .18s ease;box-shadow:inset 0 1px rgba(255,255,255,.035)}}.check:hover{{border-color:rgba(110,239,208,.45);transform:translateY(-1px);background:rgba(28,48,75,.9)}}.check input{{accent-color:#55dfbd;width:17px;height:17px}}.check span{{font-size:13px}}.compact{{justify-content:center;min-height:32px;padding:4px 6px;gap:6px;border:0;background:transparent;white-space:nowrap}}.compact span{{font-size:12px}}
.lock-table{{border:1px solid var(--line);border-radius:12px;overflow:hidden}}.lock-row{{display:grid;grid-template-columns:minmax(100px,1fr) repeat(3,minmax(75px,110px));align-items:center;gap:4px;padding:6px 10px;border-bottom:1px solid #263650}}.lock-row:last-child{{border-bottom:0}}.lock-name{{font-size:13px;font-weight:600}}.lock-opt{{text-align:center}}.lock-head{{background:#0e1829;color:var(--muted);font-size:12px}}
button,.btn{{border:1px solid rgba(255,255,255,.20);border-radius:13px;padding:11px 17px;background:linear-gradient(135deg,#8bb4ff 0%,#6c8df4 48%,#64efcb 100%);color:#081326;font:inherit;font-size:13px;font-weight:800;cursor:pointer;text-decoration:none;display:inline-block;box-shadow:0 10px 24px rgba(68,119,227,.22),inset 0 1px rgba(255,255,255,.65);transition:transform .18s ease,filter .18s ease,box-shadow .18s ease}}button:hover,.btn:hover{{filter:brightness(1.08);transform:translateY(-2px);box-shadow:0 14px 28px rgba(68,119,227,.28),inset 0 1px rgba(255,255,255,.65)}}button:active,.btn:active{{transform:translateY(0)}}.btn.secondary,button.secondary{{background:linear-gradient(145deg,rgba(34,52,81,.96),rgba(25,39,65,.96));color:var(--text);border:1px solid rgba(130,162,218,.25);box-shadow:none}}.btn.danger,button.danger{{background:linear-gradient(145deg,#782e4a,#512437);color:#ffdce2;border:1px solid #804051}}.actions{{display:flex;flex-wrap:wrap;gap:9px;margin-top:15px}}
.form-grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(190px,1fr));gap:12px}}.field{{display:flex;flex-direction:column;gap:5px;font-size:13px;color:var(--muted)}}input[type=text],input[type=number],select,textarea{{width:100%;background:rgba(5,13,28,.72);border:1px solid rgba(129,162,217,.27);border-radius:13px;padding:12px;color:var(--text);font:inherit;font-size:13px;outline:none;box-shadow:inset 0 2px 8px rgba(0,0,0,.18);transition:border-color .18s ease,box-shadow .18s ease}}textarea{{min-height:96px;resize:vertical}}input:focus,select:focus,textarea:focus{{border-color:var(--blue);box-shadow:0 0 0 3px rgba(119,167,255,.13),inset 0 2px 8px rgba(0,0,0,.18)}}.notice{{background:linear-gradient(135deg,rgba(23,75,67,.88),rgba(18,53,58,.86));color:#a4f5df;border:1px solid rgba(77,217,188,.38);padding:12px 15px;border-radius:15px;margin:0 0 18px;font-size:14px;box-shadow:0 12px 28px rgba(0,0,0,.13)}}
.stat-grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(130px,1fr));gap:10px;margin-top:12px}}.stat{{padding:14px;border-radius:15px;background:linear-gradient(145deg,rgba(14,27,48,.95),rgba(16,29,50,.72));border:1px solid rgba(125,159,218,.2);box-shadow:inset 0 1px rgba(255,255,255,.05),0 8px 18px rgba(0,0,0,.12)}}.stat b{{display:block;font-size:19px}}.stat span{{color:var(--muted);font-size:12px}}.muted{{color:var(--muted);font-size:13px}}.list{{display:grid;gap:7px;max-height:260px;overflow:auto}}.list-item{{display:flex;justify-content:space-between;gap:12px;align-items:center;padding:8px 10px;background:rgba(7,17,32,.72);border:1px solid rgba(110,143,198,.19);border-radius:11px;font-size:12px;direction:ltr;text-align:left}}.list-item small{{color:var(--muted)}}.notice-warn{{color:#ffd7a0;background:rgba(66,53,31,.8);border:1px solid rgba(180,143,62,.5);padding:10px 12px;border-radius:13px;font-size:12px}}
footer{{text-align:center;color:#7285a4;font-size:12px;margin-top:22px}}code{{direction:ltr;unicode-bidi:embed;color:#a9c9ff}}@media(max-width:760px){{main{{padding:12px 9px 28px}}header{{padding:14px;border-radius:18px}}.card.half{{grid-column:span 12}}.card{{padding:15px;border-radius:18px}}.lock-row{{grid-template-columns:minmax(80px,1fr) repeat(3,minmax(65px,85px));padding:4px 6px}}.compact span{{font-size:10px}}h1{{font-size:18px}}.logo{{width:48px;height:48px;border-radius:15px}}}}
@keyframes fbiAura{{0%,100%{{opacity:.38;transform:translate3d(0,0,0) scale(1)}}50%{{opacity:.66;transform:translate3d(8px,-10px,0) scale(1.06)}}}}body:before{{animation:fbiAura 12s ease-in-out infinite}}body:after{{animation:fbiAura 15s ease-in-out infinite reverse}}header{{transform-style:preserve-3d}}.brand .logo{{transform:rotate(-5deg) translateZ(12px);transition:transform .25s ease}}header:hover .logo{{transform:rotate(0deg) translateZ(20px) scale(1.03)}}.stat{{transition:transform .2s ease,border-color .2s ease;transform:translateZ(0)}}.stat:hover{{transform:translateY(-3px) translateZ(8px);border-color:rgba(128,175,255,.45)}}.lock-row:hover{{background:rgba(72,106,166,.11)}}button:focus-visible,.btn:focus-visible,input:focus-visible,select:focus-visible,textarea:focus-visible{{outline:2px solid var(--mint);outline-offset:2px}}@media(prefers-reduced-motion:reduce){{*,*:before,*:after{{animation:none!important;transition:none!important;scroll-behavior:auto!important}}}}
{TOUCH_CSS}</style></head><body><main>
<header><div class="brand"><div class="logo">F</div><div><h1>{panel_title}</h1><div class="subtitle">مدیریت امن و مستقیم تنظیمات ربات</div></div></div><div>{_group_status(group)}</div></header>
{notice_html}
<section class="card" style="margin-bottom:16px"><h2>{title}</h2><p class="desc">شناسهٔ گروه: <code>{gid}</code> · مالک تأییدشده: <code>{html.escape(str(_real_owner(group) or 'نامشخص'))}</code></p>
<div class="stat-grid"><div class="stat"><b>{utils.fa_digits(len(group.get('admins') or []))}</b><span>ادمین‌های محلی ثبت‌شده</span></div><div class="stat"><b>{utils.fa_digits(len(group.get('mute') or {}))}</b><span>کاربران در فهرست سکوت</span></div><div class="stat"><b>{utils.fa_digits(len(group.get('warns') or {}))}</b><span>کاربران دارای اخطار</span></div><div class="stat"><b>{'بسته' if closed else 'باز'}</b><span>وضعیت ارسال پیام گروه</span></div></div>
<div class="actions"><form method="post" action="/group/{quote(str(guid), safe='-')}/group-action?token={safe_token}"><input type="hidden" name="action" value="{'open' if closed else 'close'}"><button class="{'secondary' if closed else 'danger'}" type="submit">{'🔓 بازکردن ارسال پیام' if closed else '🔒 بستن ارسال پیام'}</button></form><form method="post" action="/group/{quote(str(guid), safe='-')}/group-action?token={safe_token}"><input type="hidden" name="action" value="refresh"><button class="secondary" type="submit">↻ به‌روزرسانی وضعیت</button></form></div>
</section>
<div class="grid">
<section class="card"><h2>⚙️ تنظیمات کلی</h2><p class="desc">این گزینه‌ها در دیتابیس اصلی ربات ذخیره می‌شوند.</p>
<form method="post" action="/group/{quote(str(guid), safe='-')}/settings?token={safe_token}"><input type="hidden" name="settings_submitted" value="1"><div class="feature-grid">{features}</div>
<div class="form-grid" style="margin-top:15px"><label class="field">حداکثر اخطار پیش از بن<input type="number" name="max_warn" min="1" max="100" value="{_safe_int(group.get('max_warn'), 5, 1, 100)}"></label><label class="field">زمان حذف خودکار (ثانیه)<input type="number" name="auto_delete_time" min="1" max="86400" value="{_safe_int(group.get('auto_delete_time'), 60, 1, 86400)}"></label><label class="field">حداکثر حجم فایل (مگابایت)<input type="number" name="max_file_mb" min="1" max="2048" value="{_safe_int(group.get('max_file_mb'), 20, 1, 2048)}"></label><label class="field">سطح ضداسپم<select name="spam_level"><option value="1"{' selected' if _safe_int(group.get('spam_level'), 2, 1, 3)==1 else ''}>۱ — ملایم</option><option value="2"{' selected' if _safe_int(group.get('spam_level'), 2, 1, 3)==2 else ''}>۲ — متعادل</option><option value="3"{' selected' if _safe_int(group.get('spam_level'), 2, 1, 3)==3 else ''}>۳ — سخت‌گیرانه</option></select></label></div>
<div class="actions"><button type="submit">ذخیره تنظیمات</button></div></form></section>
<section class="card"><h2>🔐 قفل‌ها و تنبیه خودکار</h2><p class="desc">قفل هر نوع محتوا، اخطار یا بن خودکار را جداگانه تعیین کنید.</p>
<form method="post" action="/group/{quote(str(guid), safe='-')}/settings?token={safe_token}"><input type="hidden" name="locks_submitted" value="1"><div class="lock-table"><div class="lock-row lock-head"><div>نوع محتوا</div><div class="lock-opt">قفل</div><div class="lock-opt">اخطار</div><div class="lock-opt">بن خودکار</div></div>{lock_html}</div><div class="actions"><button type="submit">ذخیره قفل‌ها</button></div></form></section>
<section class="card"><h2>🛡️ مدیریت کاربران</h2><p class="desc">فهرست اعضای گروه هنگام بازشدن پنل تازه‌سازی می‌شود. از فهرست انتخاب کنید یا در صورت نیاز شناسه را دستی وارد کنید.</p>
<form method="post" action="/group/{quote(str(guid), safe='-')}/moderation?token={safe_token}"><div class="form-grid"><label class="field">جست‌وجو در اعضای گروه<input id="member-search" type="text" placeholder="نام، آیدی یا شناسه را جست‌وجو کنید" autocomplete="off"></label><label class="field">فهرست اعضای زندهٔ گروه<select id="member-picker"><option value="">— انتخاب عضو —</option>{member_select_options}</select><span class="muted">اعضای قابل انتخاب از سرور: {utils.fa_digits(member_count)}</span></label><label class="field">شناسه کاربر<input id="user-id" type="text" name="user_id" inputmode="numeric" pattern="-?[0-9]+" maxlength="32" placeholder="از فهرست بالا انتخاب کنید" required></label><label class="field">عملیات<select name="action">{''.join('<option value="%s">%s</option>'%(html.escape(k,quote=True),html.escape(v)) for k,v in MODERATION_ACTIONS.items())}</select></label><label class="field">مدت سکوت به دقیقه<input type="number" name="minutes" min="1" max="43200" placeholder="خالی = دائمی"></label><label class="field">دلیل اخطار (اختیاری)<input type="text" name="reason" maxlength="300" placeholder="دلیل تنبیه"></label></div><div class="actions"><button type="submit">اجرای عملیات</button><button type="button" class="secondary" onclick="location.reload()">↻ تازه‌سازی فهرست اعضا</button></div></form>
<div class="notice-warn" style="margin-top:14px">عملیات بن، سکوت و ارتقای ادمین به دسترسی سروری نیاز دارد. نتیجهٔ هر عملیات بعد از پاسخ سرور گزارش می‌شود؛ ربات افراد دارای مقام بالاتر/معاف را از تنبیه محافظت می‌کند. اگر API گروه فقط بخشی از اعضا را برگرداند، از شناسهٔ مستقیم هم می‌توانید استفاده کنید.</div></section>
<section class="card half"><h2>🔇 فهرست سکوت</h2><p class="desc">حداکثر ۳۰ مورد اخیر از دیتابیس گروه</p><div class="list">{mute_html}</div></section>
<section class="card half"><h2>⚠️ فهرست اخطارها</h2><p class="desc">حداکثر ۳۰ مورد اخیر از دیتابیس گروه</p><div class="list">{warn_html}</div></section>
<section class="card half"><h2>👮 ادمین‌های محلی</h2><p class="desc">شناسه‌هایی که در تنظیمات FBI ثبت شده‌اند</p><div class="list">{admin_html}</div></section>
<section class="card half"><h2>⌨️ اجرای سایر دستورات ربات</h2><p class="desc">برای دسترسی به فرمان‌هایی که دکمهٔ اختصاصی ندارند، همان دستور فارسی ربات را وارد کنید.</p>
<form method="post" action="/group/{quote(str(guid), safe='-')}/command?token={safe_token}"><label class="field">دستور گروه<textarea name="command" maxlength="1500" placeholder="مثلاً: خوشامدگویی روشن&#10;یا: لیست سکوت" required></textarea></label><div class="actions"><button type="submit">اجرای دستور</button></div></form><p class="muted">پاسخ بسیاری از دستورات در خود گروه ارسال می‌شود تا با رفتار فعلی ربات هماهنگ بماند.</p></section>
</div><footer>FBI SPLUS · پنل سه‌بعدی مالک · لینک اختصاصی؛ هرگز آن را عمومی نکنید.</footer></main>
<script>
(()=>{{const picker=document.getElementById('member-picker'),target=document.getElementById('user-id'),search=document.getElementById('member-search');if(!picker||!target)return;picker.addEventListener('change',()=>{{if(picker.value)target.value=picker.value;}});if(search)search.addEventListener('input',()=>{{const q=search.value.toLocaleLowerCase();Array.from(picker.options).forEach((o,i)=>{{if(i===0)return;const hay=(o.text+' '+(o.dataset.search||'')).toLocaleLowerCase();o.hidden=!!q&&!hay.includes(q);}});}});}})();
</script></body></html>'''


def _page_response(guid: str, group: dict, token: str, notice: str = ""):
    response = web.Response(text=_render_page(guid, group, token, notice), content_type="text/html", charset="utf-8")
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Cache-Control"] = "no-store, max-age=0"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    return response


def _authorize(request):
    guid = str(request.match_info.get("guid") or "")
    token = str(request.query.get("token") or "")
    if not re.fullmatch(r"-?\d{1,40}", guid) or len(token) < 32 or len(token) > 200:
        return None
    group = db.data.get("groups", {}).get(guid)
    if not isinstance(group, dict):
        return None
    session = group.get("web_panel_session") or {}
    owner_id = str(session.get("owner") or "")
    actual_owner = _real_owner(group)
    if not owner_id or actual_owner != owner_id or float(session.get("expires_at") or 0) <= time.time():
        return None
    if not secrets.compare_digest(str(session.get("token_hash") or ""), _token_hash(token)):
        return None
    return guid, group, owner_id, token


async def _dashboard(request):
    auth = _authorize(request)
    if not auth:
        return web.Response(text="لینک پنل نامعتبر یا منقضی شده است. در پیوی ربات دوباره عدد ۱ و شماره گروه را بفرستید.", status=403, content_type="text/plain", charset="utf-8")
    guid, group, owner_id, token = auth
    notice = str(request.query.get("notice") or "")[:500]
    members = []
    # Query the live chat metadata so a missing/stale title in the local DB
    # never leaves the group dashboard looking unnamed.
    if _API is not None:
        try:
            live = await asyncio.wait_for(_API.group_info(guid), timeout=10)
            live_title = _live_group_title(live)
            current_title = str(group.get("title") or "").strip()
            if live_title and current_title != live_title:
                group["title"] = live_title
                db.save("groups")
        except Exception as exc:
            log.debug("live group title fetch failed guid=%s: %r", guid, exc)
        try:
            response = await asyncio.wait_for(_API.members(guid), timeout=18)
            rows = response.get("members", response.get("users", response.get("participants", []))) if isinstance(response, dict) else response
            if isinstance(rows, dict):
                normalized_rows = []
                for map_id, value in rows.items():
                    if isinstance(value, dict):
                        item = dict(value)
                        item.setdefault("id", map_id)
                        normalized_rows.append(item)
                    else:
                        normalized_rows.append({"id": map_id, "name": str(value or "")})
                rows = normalized_rows
            members = []
            for row in (rows or []):
                if isinstance(row, dict):
                    item = dict(row)
                    item["id"] = str(item.get("id") or item.get("user_id") or item.get("user_guid") or item.get("guid") or item.get("member_id") or "")
                    item["first_name"] = str(item.get("first_name") or item.get("display_name") or item.get("full_name") or item.get("name") or item.get("first") or "")
                    item["last_name"] = str(item.get("last_name") or item.get("last") or "")
                    item["username"] = str(item.get("username") or item.get("user_name") or "")
                    item["is_admin"] = bool(item.get("is_admin") or item.get("admin"))
                    item["is_owner"] = bool(item.get("is_owner") or item.get("owner") or item.get("creator") or item.get("is_creator"))
                    members.append(item)
                else:
                    members.append({
                        "id": str(getattr(row, "id", getattr(row, "user_id", getattr(row, "user_guid", getattr(row, "guid", "")))) or ""),
                        "first_name": str(getattr(row, "first_name", getattr(row, "display_name", getattr(row, "name", ""))) or ""),
                        "last_name": str(getattr(row, "last_name", "") or ""),
                        "username": str(getattr(row, "username", getattr(row, "user_name", "")) or ""),
                        "is_admin": bool(getattr(row, "is_admin", getattr(row, "admin", False))),
                        "is_owner": bool(getattr(row, "is_owner", getattr(row, "owner", getattr(row, "creator", False)))),
                    })
            members = [row for row in members if str(row.get("id") or "").strip()]
            known_admins = {str(x) for x in (group.get("admins") or [])}
            real_owner = str(_real_owner(group) or "")
            for member in members:
                member_id = str(member.get("id") or "")
                member["is_owner"] = bool(member.get("is_owner") or (real_owner and member_id == real_owner))
                member["is_admin"] = bool(member.get("is_admin") or member_id in known_admins or member.get("is_owner"))
        except Exception as exc:
            log.debug("live group members fetch failed guid=%s: %r", guid, exc)
    body = _render_page(guid, group, token, notice, members=members)
    response = web.Response(text=body, content_type="text/html", charset="utf-8")
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Cache-Control"] = "no-store, max-age=0"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    return response


async def _redirect_notice(request, guid: str, token: str, message: str):
    location = "/group/%s?%s" % (quote(str(guid), safe="-"), urlencode({"token": token, "notice": str(message)[:350]}))
    raise web.HTTPSeeOther(location=location)


async def _save_settings(request):
    auth = _authorize(request)
    if not auth:
        return web.Response(text="دسترسی غیرمجاز یا لینک منقضی شده است.", status=403, content_type="text/plain", charset="utf-8")
    guid, group, owner_id, token = auth
    form = await request.post()
    changed = []
    if "settings_submitted" in form:
        for key in FEATURES:
            group[key] = ("feature_" + key) in form
        int_fields = {
            "max_warn": (1, 100, 5),
            "auto_delete_time": (1, 86400, 60),
            "max_file_mb": (1, 2048, 20),
            "spam_level": (1, 3, 2),
        }
        for key, (low, high, default) in int_fields.items():
            try:
                value = int(_latin_digits(form.get(key, default)))
            except Exception:
                value = default
            group[key] = max(low, min(high, value))
        changed.append("تنظیمات کلی")
    if "locks_submitted" in form:
        locks = group.setdefault("locks", {})
        warn_settings = group.setdefault("lock_warn", {})
        ban_settings = group.setdefault("lock_ban", {})
        for key in _lock_keys(group):
            locks[key] = ("lock_" + key) in form
            warn_settings[key] = ("lock_warn_" + key) in form
            ban_settings[key] = ("lock_ban_" + key) in form
        if _API is not None:
            try:
                synced = await _API.sync_group_permissions(guid)
                changed.append("محدودیت سروری قفل‌ها اعمال شد" if synced else "محدودیت سروری قفل‌ها اعمال نشد")
            except Exception:
                changed.append("محدودیت سروری قفل‌ها اعمال نشد")
        changed.append("قفل‌ها و تنبیه خودکار")
    if not changed:
        return await _redirect_notice(request, guid, token, "هیچ تنظیمی برای ذخیره دریافت نشد.")
    db.save("groups")
    return await _redirect_notice(request, guid, token, "✅ ذخیره شد: " + "، ".join(changed))


async def _group_action(request):
    auth = _authorize(request)
    if not auth:
        return web.Response(text="دسترسی غیرمجاز یا لینک منقضی شده است.", status=403, content_type="text/plain", charset="utf-8")
    guid, group, owner_id, token = auth
    form = await request.post()
    action = str(form.get("action") or "")
    try:
        if action == "close":
            ok = await _API.close_group_permissions(guid)
            if not ok:
                raise RuntimeError("close_group_permissions returned false")
            group["closed"] = True
            group["closed_until"] = 0
            db.save("groups")
            message = "🔒 ارسال پیام در گروه بسته شد."
        elif action == "open":
            ok = await _API.open_group_permissions(guid)
            if not ok:
                raise RuntimeError("open_group_permissions returned false")
            group["closed"] = False
            group["closed_until"] = 0
            group["opened_at"] = time.time()
            db.save("groups")
            message = "🔓 ارسال پیام در گروه باز شد."
        elif action == "refresh":
            info = await _API.group_info(guid)
            live_title = _live_group_title(info)
            if live_title:
                group["title"] = live_title
                db.save("groups")
            message = "وضعیت صفحه به‌روزرسانی شد."
        else:
            message = "عملیات ناشناخته است."
    except Exception as exc:
        log.warning("web group action failed group=%s action=%s: %r", guid, action, exc)
        message = "❌ عملیات در سرور انجام نشد؛ دسترسی ادمین کامل ربات را بررسی کنید."
    return await _redirect_notice(request, guid, token, message)


async def _moderation(request):
    auth = _authorize(request)
    if not auth:
        return web.Response(text="دسترسی غیرمجاز یا لینک منقضی شده است.", status=403, content_type="text/plain", charset="utf-8")
    guid, group, owner_id, token = auth
    form = await request.post()
    target = _latin_digits(str(form.get("user_id") or "").strip())
    action = str(form.get("action") or "")
    reason_text = str(form.get("reason") or "").strip()[:300]
    try:
        if not re.fullmatch(r"-?\d{1,32}", target) or target == guid or target == str(_ME_GUID):
            raise ValueError("شناسهٔ کاربر معتبر نیست.")
        if action not in MODERATION_ACTIONS:
            raise ValueError("عملیات ناشناخته است.")
        fake_update = SimpleNamespace(
            object_guid=guid, author_guid=owner_id, message_id="", text="",
            is_group=True, is_private=False, is_channel=False,
        )
        ctx = Ctx(_CLIENT, _API, fake_update, _ME_GUID)
        ctx.g = group
        ctx.rank = ranks.OWNER

        needs_protection = action in {"mute", "ban", "warn", "block", "kill", "exempt", "add_admin"}
        if needs_protection:
            protection = await ctx.protection_reason(target)
            if protection:
                raise ValueError(protection)

        if action == "mute":
            raw_minutes = _latin_digits(str(form.get("minutes") or "").strip())
            minutes = None
            if raw_minutes:
                if not raw_minutes.isdigit() or not (1 <= int(raw_minutes) <= 43200):
                    raise ValueError("مدت سکوت باید بین ۱ تا ۴۳۲۰۰ دقیقه باشد.")
                minutes = int(raw_minutes)
            group.setdefault("mute", {})[target] = time.time() + minutes * 60 if minutes else 0
            group.setdefault("mute_started", {})[target] = time.time()
            group.setdefault("mute_messages", {})[target] = []
            group.setdefault("mute_reason", {})[target] = "web_panel"
            try:
                await _API.mute_member(guid, target, minutes=minutes)
                group.setdefault("mute_server_applied", {})[target] = True
                message = "🔇 کاربر سکوت شد" + (" به مدت %s دقیقه." % utils.fa_digits(minutes) if minutes else " به‌صورت دائمی.")
            except Exception as exc:
                group.setdefault("mute_server_applied", {})[target] = False
                message = "⚠️ سکوت محلی ذخیره شد؛ سکوت کامل سروری اعمال نشد: %s" % (str(exc) or type(exc).__name__)
            group.setdefault("mute_permission_last", {})[target] = time.time()
        elif action == "unmute":
            group.setdefault("mute", {}).pop(target, None)
            group.setdefault("mute_server_applied", {}).pop(target, None)
            group.setdefault("mute_permission_last", {}).pop(target, None)
            group.setdefault("mute_reason", {}).pop(target, None)
            group.setdefault("mute_started", {}).pop(target, None)
            group.setdefault("mute_messages", {}).pop(target, None)
            try:
                await _API.unmute_member(guid, target)
                group.setdefault("unmute_pending", {}).pop(str(target), None)
                message = "✅ سکوت کاربر برداشته شد."
            except Exception as exc:
                # Keep the release queued: restore_active_mutes retries it.
                group.setdefault("unmute_pending", {})[str(target)] = time.time()
                message = "⚠️ از فهرست سکوت حذف شد؛ رفع محدودیت سروری تأیید نشد (%s) و دوباره امتحان می‌شود." % (str(exc) or type(exc).__name__)
        elif action == "ban":
            await _API.ban(guid, target)
            message = "🚫 کاربر از گروه بن شد."
        elif action == "unban":
            await _API.unban(guid, target)
            try:
                added = await _API.add_member(guid, target)
            except Exception:
                added = False
            message = "✅ بن برداشته شد." + (" کاربر دوباره اضافه شد." if added else " برای ورود دوباره، لینک دعوت لازم است.")
        elif action == "warn":
            warns = group.setdefault("warns", {})
            count = int(warns.get(target, 0) or 0) + 1
            maximum = max(1, int(group.get("max_warn") or 5))
            warns[target] = count
            if count >= maximum:
                warns.pop(target, None)
                await _API.ban(guid, target)
                message = "🚫 سقف اخطار پر شد؛ کاربر بن شد."
            else:
                message = "⚠️ اخطار ثبت شد: %s از %s%s" % (
                    utils.fa_digits(count), utils.fa_digits(maximum), (" — " + reason_text) if reason_text else "")
        elif action == "clear_warn":
            group.setdefault("warns", {}).pop(target, None)
            message = "✅ اخطارهای این کاربر حذف شد."
        elif action == "block":
            items = group.setdefault("block", [])
            if target not in items: items.append(target)
            message = "🚫 کاربر به فهرست بلاک محلی اضافه شد."
        elif action == "unblock":
            items = group.setdefault("block", [])
            if target in items: items.remove(target)
            message = "✅ کاربر از فهرست بلاک محلی حذف شد."
        elif action == "kill":
            items = group.setdefault("kill", [])
            if target not in items: items.append(target)
            message = "⛔ کاربر به فهرست کیل اضافه شد؛ با ورود مجدد حذف می‌شود."
        elif action == "unkill":
            items = group.setdefault("kill", [])
            if target in items: items.remove(target)
            message = "✅ کاربر از فهرست کیل حذف شد."
        elif action == "exempt":
            items = group.setdefault("exempt", [])
            if target not in items: items.append(target)
            message = "✅ کاربر معاف شد و از تنبیه‌های خودکار مستثنا است."
        elif action == "unexempt":
            items = group.setdefault("exempt", [])
            if target in items: items.remove(target)
            message = "✅ معافیت کاربر برداشته شد."
        elif action == "add_admin":
            await _API.set_admin(guid, target, access="full")
            admins = group.setdefault("admins", [])
            if target not in admins: admins.append(target)
            full = group.setdefault("full_admins", [])
            if target not in full: full.append(target)
            message = "✅ دسترسی ادمین فول اعطا شد."
        elif action == "remove_admin":
            await _API.unset_admin(guid, target)
            group["admins"] = [str(x) for x in group.get("admins", []) if str(x) != target]
            group["full_admins"] = [str(x) for x in group.get("full_admins", []) if str(x) != target]
            message = "✅ دسترسی ادمین حذف شد."
        else:
            message = "عملیات اجرا نشد."
        db.save("groups")
    except ValueError as exc:
        message = "❌ " + str(exc)
    except Exception as exc:
        log.exception("web moderation failed group=%s target=%s action=%s", guid, target, action)
        message = "❌ عملیات انجام نشد (%s). اختیارات ادمین ربات و شناسه کاربر را بررسی کنید." % type(exc).__name__
    return await _redirect_notice(request, guid, token, message)


async def _run_command(request):
    auth = _authorize(request)
    if not auth:
        return web.Response(text="دسترسی غیرمجاز یا لینک منقضی شده است.", status=403, content_type="text/plain", charset="utf-8")
    guid, group, owner_id, token = auth
    form = await request.post()
    command = str(form.get("command") or "").strip()
    if not command or len(command) > 1500:
        return await _redirect_notice(request, guid, token, "❌ دستور خالی یا طولانی است.")
    if _DISPATCH is None:
        return await _redirect_notice(request, guid, token, "❌ مسیریاب دستورات در دسترس نیست.")
    fake_message = SimpleNamespace(
        chat_id=guid,
        sender_id=owner_id,
        id="",
        message_id="",
        raw_text=command,
        text=command,
        is_group=True,
        is_private=False,
        is_channel=False,
        is_me=False,
        out=False,
    )
    try:
        # Reuse the same routing/permission checks/features as ordinary group commands.
        await _DISPATCH(fake_message, internal=True)
        message = "✅ دستور به مسیریاب اصلی ربات سپرده شد؛ پاسخ بیشتر دستورات در خود گروه فرستاده می‌شود."
    except Exception as exc:
        log.exception("web command dispatch failed group=%s", guid)
        message = "❌ اجرای دستور شکست خورد (%s)." % type(exc).__name__
    return await _redirect_notice(request, guid, token, message)


def _creator_authorize(request):
    uid = str(request.match_info.get("creator_id") or "")
    token = str(request.query.get("token") or "")
    if not uid or len(token) < 32 or len(token) > 200 or not subscription.is_sudo(uid):
        return None
    user = db.data.get("users", {}).get(uid) or {}
    session = user.get("creator_web_panel_session") or {}
    if str(session.get("owner") or "") != uid or float(session.get("expires_at") or 0) <= time.time():
        return None
    if not secrets.compare_digest(str(session.get("token_hash") or ""), _token_hash(token)):
        return None
    return uid, token


def _creator_counts():
    groups = db.data.get("groups", {})
    total = len(groups)
    active = 0
    subordinate = 0
    for gid, group in groups.items():
        if group.get("subordinate"):
            subordinate += 1
        if group.get("active") and (group.get("subordinate") or subscription.allowed(str(gid))):
            active += 1
    return total, active, max(0, total - active), subordinate


def _creator_html(uid: str, token: str, notice: str = "") -> str:
    total, active, inactive, subordinate = _creator_counts()
    groups = []
    for guid, group in db.data.get("groups", {}).items():
        title = str(group.get("title") or "").strip()
        if title.casefold() in {"", "گروه", "گروه نامشخص", "unknown", "chat"}:
            title = "گروه · " + str(guid)
        status = "فعال" if group.get("active") and not group.get("manual_disabled") else "غیرفعال"
        sub = "زیرپرچم" if group.get("subordinate") else "اشتراک عادی"
        expiry = subscription.expiry_text(group)
        owner = str(group.get("owner") or group.get("live_owner") or "—")
        groups.append((str(guid), title, status, sub, expiry, owner))
    groups.sort(key=lambda row: (row[1].casefold(), row[0]))
    token_q = quote(token, safe="")
    base_path = "/creator/%s" % quote(uid, safe="-")
    notice_html = '<div class="notice">%s</div>' % html.escape(notice) if notice else ""
    row_html = []
    for guid, title, status, sub, expiry, owner in groups:
        gid_html = html.escape(guid)
        search = (title + " " + guid + " " + owner + " " + status + " " + sub).casefold()
        row_html.append(f'''<tr data-search="{html.escape(search, quote=True)}">
<td class="cell-title"><b>{html.escape(title)}</b><small dir="ltr">{gid_html}</small></td>
<td data-label="وضعیت"><span class="badge {'good' if status == 'فعال' else 'bad'}">{status}</span></td><td data-label="نوع">{sub}</td>
<td data-label="اعتبار">{html.escape(expiry)}</td><td dir="ltr" data-label="شناسه مالک">{html.escape(owner)}</td>
<td class="cell-actions" data-label="عملیات"><form method="post" action="{base_path}/action?token={token_q}" class="mini-actions">
<input type="hidden" name="guid" value="{html.escape(guid, quote=True)}">
<input type="number" name="days" min="1" max="3650" value="30" aria-label="روز">
<button name="action" value="renew">تمدید</button><button name="action" value="gift">هدیه</button>
<button name="action" value="subordinate">تغییر زیرپرچم</button><button class="danger" name="action" value="cancel_one" onclick="return confirm('اشتراک این گروه غیرفعال شود؟')">لغو گروه</button>
</form></td></tr>''')
    if not row_html:
        row_html.append('<tr><td colspan="6">هنوز گروهی در دیتابیس ثبت نشده است.</td></tr>')
    return f'''<!doctype html><html lang="fa" dir="rtl"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover"><meta name="referrer" content="no-referrer"><title>پنل سازنده FBI</title><style>
:root{{--bg:#070816;--ink:#f4f7ff;--muted:#a8b2d5;--edge:rgba(181,197,255,.18);--cyan:#67f2d0;--blue:#80a9ff;--purple:#ad8cff;--red:#ff789c}}
*{{box-sizing:border-box}}body{{margin:0;min-height:100vh;background:radial-gradient(ellipse at 15% 0%,#22295b 0,transparent 32%),radial-gradient(ellipse at 90% 20%,rgba(33,155,154,.18),transparent 28%),linear-gradient(155deg,#070816,#0b1327 55%,#0a0c1a);color:var(--ink);font-family:Tahoma,"Segoe UI",sans-serif;line-height:1.75}}main{{width:min(1480px,100%);margin:auto;padding:clamp(12px,3vw,34px);perspective:1400px}}header{{display:flex;align-items:center;justify-content:space-between;gap:18px;flex-wrap:wrap;padding:clamp(18px,3vw,30px);border-radius:26px;border:1px solid var(--edge);background:linear-gradient(135deg,rgba(41,52,100,.68),rgba(10,17,38,.84));box-shadow:0 28px 80px #0008,inset 0 1px #ffffff18;backdrop-filter:blur(18px);transform:translateZ(10px)}}.brand{{display:flex;align-items:center;gap:16px}}.logo{{height:60px;width:60px;border-radius:20px;display:grid;place-items:center;font-size:20px;font-weight:900;color:#080f20;background:linear-gradient(145deg,#9ac4ff,#8b83ff 48%,#6ef4d5);box-shadow:0 14px 32px #6e90ff48,inset 0 2px 3px #fff;transform:rotate(-5deg)}}h1{{margin:0;font-size:clamp(20px,3vw,29px)}}.sub{{color:var(--muted);font-size:13px}}.notice{{margin:16px 0;padding:14px 18px;border:1px solid #3bc5a766;background:linear-gradient(140deg,#12443d,#152e42);border-radius:16px;color:#b4ffea}}.cards{{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:14px;margin:18px 0}}.card{{min-width:0;padding:20px;border:1px solid var(--edge);border-radius:22px;background:linear-gradient(145deg,rgba(32,43,79,.84),rgba(11,19,40,.9));box-shadow:0 18px 42px #0005,inset 0 1px #ffffff12;transition:transform .22s,border-color .22s,box-shadow .22s;transform:translateZ(0)}}.card:hover{{transform:translateY(-4px) rotateX(.4deg);border-color:#8eb9ff66;box-shadow:0 26px 54px #0006,inset 0 1px #ffffff18}}.num{{font-size:clamp(23px,4vw,32px);font-weight:900;background:linear-gradient(90deg,#9fc2ff,#87f2d4);color:transparent;background-clip:text}}.label{{font-size:12px;color:var(--muted)}}h2{{font-size:18px;margin:0 0 7px}}.toolbar{{display:flex;flex-wrap:wrap;gap:10px;align-items:center;margin-bottom:14px}}input,select,textarea,button{{font:inherit}}input[type=text],input[type=number],select,textarea{{min-width:0;background:#080e21;color:var(--ink);border:1px solid #8297ca42;border-radius:12px;padding:10px 12px;outline:none}}input:focus,textarea:focus{{border-color:var(--blue);box-shadow:0 0 0 3px #80a9ff21}}button,.btn{{border:1px solid #fff3;border-radius:12px;padding:10px 14px;background:linear-gradient(135deg,#8ab2ff,#8178f4 52%,#67ebd0);color:#071226;font-size:12px;font-weight:900;cursor:pointer;box-shadow:0 8px 18px #668dff28,inset 0 1px #fff9;transition:transform .18s,filter .18s}}button:hover,.btn:hover{{transform:translateY(-2px);filter:brightness(1.08)}}button.secondary{{background:#1b2946;color:var(--ink);box-shadow:none}}button.danger{{background:linear-gradient(135deg,#792c4a,#4c213c);color:#ffdce8}}textarea{{width:100%;min-height:94px;resize:vertical}}.table-wrap{{overflow-x:auto;border:1px solid var(--edge);border-radius:17px}}table{{width:100%;min-width:1000px;border-collapse:collapse;background:#0d162caa}}th,td{{text-align:right;padding:12px;border-bottom:1px solid #ffffff0d;vertical-align:middle}}th{{font-size:12px;color:var(--muted);background:#111d37}}td{{font-size:12px}}td small{{display:block;color:var(--muted);margin-top:3px}}tr:hover td{{background:#ffffff06}}.badge{{display:inline-block;padding:3px 10px;border-radius:99px;font-size:11px;font-weight:800}}.good{{background:#164c43;color:#8effdc}}.bad{{background:#57283e;color:#ffc0d0}}.mini-actions{{display:flex;flex-wrap:wrap;gap:6px;align-items:center;min-width:330px}}.mini-actions input{{width:66px;padding:7px}}.mini-actions button{{padding:7px 9px;font-size:11px}}.grid2{{display:grid;grid-template-columns:1fr 1fr;gap:16px}}.hint{{font-size:12px;color:var(--muted)}}footer{{text-align:center;color:#8590b3;font-size:11px;padding:25px 5px}}@media(max-width:850px){{.cards{{grid-template-columns:repeat(2,minmax(0,1fr))}}.grid2{{grid-template-columns:1fr}}}}@media(max-width:450px){{main{{padding:10px}}header{{border-radius:20px}}.logo{{width:48px;height:48px;border-radius:15px}}.cards{{gap:8px}}.card{{padding:13px;border-radius:17px}}.toolbar input{{width:100%}}}}
{TOUCH_CSS}{CREATOR_TABLE_CSS}</style></head><body><main><header><div class="brand"><div class="logo">FBI</div><div><h1>پنل سازنده FBI</h1><div class="sub">مدیریت سراسری · آمار · اشتراک‌ها · اطلاع‌رسانی</div></div></div><div><span class="badge good">دسترسی سازنده تأیید شد</span><div class="sub" style="margin-top:6px">شناسه سازنده: <b dir="ltr">{html.escape(uid)}</b></div></div></header>
{notice_html}<div class="cards"><div class="card"><div class="num">{utils.fa_digits(total)}</div><div class="label">کل گروه‌های ثبت‌شده</div></div><div class="card"><div class="num">{utils.fa_digits(active)}</div><div class="label">گروه‌های فعال</div></div><div class="card"><div class="num">{utils.fa_digits(inactive)}</div><div class="label">گروه‌های غیرفعال</div></div><div class="card"><div class="num">{utils.fa_digits(subordinate)}</div><div class="label">گروه‌های زیرپرچم</div></div></div>
<div class="grid2"><section class="card"><h2>🎁 عملیات سراسری اشتراک</h2><p class="hint">این عملیات روی تعداد زیادی گروه اثر می‌گذارد؛ قبل از اجرا تأیید گرفته می‌شود.</p><form method="post" action="{base_path}/action?token={token_q}" class="toolbar" onsubmit="return confirm('برای تمام گروه‌های مشمول هدیه ثبت شود؟')"><input type="hidden" name="action" value="gift_all"><label>مدت هدیه (روز) <input type="number" name="days" min="1" max="3650" value="30" required></label><button type="submit">هدیه اشتراک سراسری</button></form><form method="post" action="{base_path}/action?token={token_q}" onsubmit="return confirm('هشدار: لغو اشتراک سراسری گروه‌های غیرزیرپرچم را غیرفعال می‌کند. ادامه می‌دهید؟')"><input type="hidden" name="action" value="cancel_all"><button class="danger" type="submit">لغو اشتراک سراسری</button></form></section>
<section class="card"><h2>📣 اطلاع‌رسانی</h2><p class="hint">ارسال به کاربران شناخته‌شده، گروه‌های فعال، یا هر دو.</p><form method="post" action="{base_path}/action?token={token_q}" onsubmit="return confirm('پیام برای مخاطبان انتخاب‌شده ارسال شود؟')"><textarea name="message" maxlength="3000" placeholder="متن اطلاع‌رسانی را بنویسید..." required></textarea><div class="toolbar" style="margin-top:10px"><select name="target"><option value="both">پیوی و گروه‌های فعال</option><option value="users">فقط پیوی کاربران</option><option value="groups">فقط گروه‌های فعال</option></select><button type="submit" name="action" value="announce">ارسال اطلاع‌رسانی</button></div></form></section></div>
<section class="card" style="margin-top:16px"><h2>🗂 مدیریت گروه‌ها و اشتراک‌ها</h2><p class="hint">جست‌وجو با نام گروه، شناسه، مالک یا وضعیت. برای هر گروه می‌توانید اشتراک را تمدید کنید، هدیه بدهید، وضعیت زیرپرچم را عوض کنید یا اشتراک را لغو کنید.</p><div class="toolbar"><input id="group-search" type="text" placeholder="جست‌وجوی گروه‌ها..." style="flex:1"><span class="hint">{utils.fa_digits(len(groups))} رکورد</span></div><div class="table-wrap"><table><thead><tr><th>گروه</th><th>وضعیت</th><th>نوع</th><th>اعتبار</th><th>شناسه مالک</th><th>عملیات</th></tr></thead><tbody id="groups-body">{''.join(row_html)}</tbody></table></div></section>
<footer>FBI SPLUS · پنل سازنده · دسترسی این صفحه مختص حساب سازنده است. لینک را خصوصی نگه دارید.</footer></main><script>(()=>{{const q=document.getElementById('group-search');if(!q)return;q.addEventListener('input',()=>{{const val=q.value.trim().toLocaleLowerCase();document.querySelectorAll('#groups-body tr').forEach(r=>{{r.hidden=!!val&&!(r.dataset.search||'').includes(val);}});}});}})();</script></body></html>'''


async def _creator_dashboard(request):
    auth = _creator_authorize(request)
    if not auth:
        return web.Response(text="لینک پنل سازنده نامعتبر یا منقضی شده است. در پیوی ربات دوباره /owner را بفرستید.", status=403, content_type="text/plain", charset="utf-8")
    uid, token = auth
    body = _creator_html(uid, token, str(request.query.get("notice") or "")[:400])
    response = web.Response(text=body, content_type="text/html", charset="utf-8")
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Cache-Control"] = "no-store, max-age=0"
    response.headers["Content-Security-Policy"] = "default-src 'self' 'unsafe-inline'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'"
    return response


async def _creator_action(request):
    auth = _creator_authorize(request)
    if not auth:
        return web.Response(text="دسترسی سازنده معتبر نیست.", status=403, content_type="text/plain", charset="utf-8")
    uid, token = auth
    form = await request.post()
    action = str(form.get("action") or "").strip()
    result = "عملیات ناشناخته است."
    try:
        if action == "gift_all":
            days = _safe_int(_latin_digits(form.get("days")), 0, 1, 3650)
            if not days:
                raise ValueError("مدت هدیه باید بین ۱ تا ۳۶۵۰ روز باشد.")
            changed, sent, failed, skipped = await subscription.gift_all(_API, days)
            result = "هدیه سراسری انجام شد: %s گروه، پیام موفق %s، ناموفق %s، ردشده %s." % tuple(utils.fa_digits(x) for x in (changed, sent, failed, skipped))
        elif action == "cancel_all":
            changed, sent, failed = await subscription.cancel_all(_API)
            result = "لغو اشتراک سراسری انجام شد: %s گروه، پیام موفق %s، ناموفق %s. گروه‌های زیرپرچم مستثنا ماندند." % tuple(utils.fa_digits(x) for x in (changed, sent, failed))
        elif action == "announce":
            message = str(form.get("message") or "").strip()[:3000]
            target = str(form.get("target") or "both")
            if not message:
                raise ValueError("متن اطلاع‌رسانی خالی است.")
            if target not in {"users", "groups", "both"}:
                raise ValueError("مقصد ارسال معتبر نیست.")
            users = []
            for key in db.data.get("users", {}):
                sid = str(key)
                if sid.lstrip("-").isdigit() and sid not in {str(x) for x in config.SUDOS} and sid != str(_ME_GUID):
                    users.append(sid)
            groups = [str(gid) for gid, group in db.data.get("groups", {}).items() if group.get("active") and not group.get("manual_disabled")]
            sent_users = sent_groups = failed = 0
            body = "📢 اطلاع‌رسانی FBI\n\n" + message
            if target in {"users", "both"}:
                for recipient in list(dict.fromkeys(users)):
                    try:
                        if await _API.send(recipient, body) is not None:
                            sent_users += 1
                        else:
                            failed += 1
                    except Exception:
                        failed += 1
            if target in {"groups", "both"}:
                for recipient in groups:
                    try:
                        if await _API.send(recipient, body) is not None:
                            sent_groups += 1
                        else:
                            failed += 1
                    except Exception:
                        failed += 1
            result = "اطلاع‌رسانی پایان یافت: پیوی %s، گروه %s، ناموفق %s." % tuple(utils.fa_digits(x) for x in (sent_users, sent_groups, failed))
        elif action in {"renew", "gift", "cancel_one", "subordinate"}:
            guid = _latin_digits(str(form.get("guid") or "").strip())
            if not re.fullmatch(r"-?\d{1,40}", guid) or guid not in db.data.get("groups", {}):
                raise ValueError("شناسهٔ گروه در دیتابیس موجود نیست.")
            group = db.data["groups"][guid]
            if action in {"renew", "gift"}:
                days = _safe_int(_latin_digits(form.get("days")), 0, 1, 3650)
                if not days:
                    raise ValueError("مدت باید بین ۱ تا ۳۶۵۰ روز باشد.")
                if action == "gift":
                    preview, _until = await subscription.gift_one(_API, guid, days)
                    verb = "هدیه"
                    title = preview.get("title") or group.get("title") or guid
                else:
                    await subscription.renew(_API, guid, days)
                    verb = "تمدید"
                    title = group.get("title") or guid
                result = "%s اشتراک گروه «%s» برای %s روز انجام شد." % (verb, str(title), utils.fa_digits(days))
            elif action == "cancel_one":
                group["subscription_active"] = False
                group["subscription_until"] = 0
                group["subscription_revoked_at"] = time.time()
                group["subscription_notice_sent_at"] = time.time()
                group["active"] = False
                group["manual_disabled"] = True
                group["subordinate"] = False
                db.save("groups")
                try:
                    await _API.send(guid, subscription.group_deactivation_message(guid))
                except Exception:
                    pass
                result = "اشتراک گروه «%s» لغو شد." % str(group.get("title") or guid)
            elif action == "subordinate":
                group["subordinate"] = not bool(group.get("subordinate"))
                db.save("groups")
                result = "وضعیت زیرپرچم گروه «%s» اکنون %s است." % (str(group.get("title") or guid), "فعال" if group["subordinate"] else "غیرفعال")
        else:
            raise ValueError("عملیات شناخته‌شده نیست.")
    except ValueError as exc:
        result = "خطا: " + str(exc)
    except Exception as exc:
        log.exception("creator web action failed action=%s", action)
        result = "عملیات ناموفق بود (%s). اختیارات حساب و وضعیت API را بررسی کنید." % type(exc).__name__
    location = "/creator/%s?%s" % (quote(uid, safe="-"), urlencode({"token": token, "notice": result[:400]}))
    raise web.HTTPSeeOther(location=location)

async def _health(_request):
    return web.json_response({"ok": True, "service": "FBI SPLUS web panel"})


async def _read_tunnel_output(proc):
    global _DYNAMIC_BASE_URL, _PUBLIC_TUNNEL_HEALTHY, _TUNNEL_REGISTERED, _LAST_TUNNEL_ERROR
    try:
        while proc.stdout:
            raw = await proc.stdout.readline()
            if not raw:
                break
            line = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else str(raw)
            if "Registered tunnel connection" in line:
                _TUNNEL_REGISTERED = True
            elif re.search(r"\b(ERR|WRN)\b|level=(error|warn)", line):
                detail = line.strip()[-300:]
                if detail != _LAST_TUNNEL_ERROR:
                    _LAST_TUNNEL_ERROR = detail
                    log.warning("cloudflared: %s", detail)
            match = re.search(r"https://[a-zA-Z0-9-]+\.trycloudflare\.com", line)
            if match:
                candidate = match.group(0).rstrip("/")
                if candidate != _DYNAMIC_BASE_URL:
                    _PUBLIC_TUNNEL_HEALTHY = False
                _DYNAMIC_BASE_URL = candidate
                log.info("Cloudflare Quick Tunnel hostname discovered; verifying public /health before sharing: %s", _DYNAMIC_BASE_URL)
                if _TUNNEL_EVENT is not None:
                    _TUNNEL_EVENT.set()
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        log.warning("Could not read Cloudflare tunnel output: %r", exc)


def _cloudflared_asset_name(system_name: str, machine: str) -> str | None:
    system_name = str(system_name or "").lower()
    machine = str(machine or "").lower()
    if machine in {"arm64", "aarch64"}:
        arch = "arm64"
    elif machine in {"amd64", "x86_64", "x64"}:
        arch = "amd64"
    elif machine in {"i386", "i686", "x86"}:
        arch = "386"
    else:
        return None
    if system_name.startswith("win"):
        return "cloudflared-windows-%s.exe" % arch if arch in {"amd64", "arm64"} else None
    if system_name == "linux":
        return "cloudflared-linux-%s" % arch if arch in {"amd64", "arm64", "386"} else None
    if system_name == "darwin":
        if arch == "386":
            return None
        return "cloudflared-darwin-%s.tgz" % arch
    return None


def _download_cloudflared_binary() -> str:
    """Download official cloudflared release and verify GitHub's published SHA-256.

    This runs only when public-tunnel support is enabled and no local binary exists.
    The archive is unpacked only after its digest matches the release metadata.
    """
    asset_name = _cloudflared_asset_name(platform.system(), platform.machine())
    if not asset_name:
        raise RuntimeError("Automatic cloudflared install is unsupported on this OS/CPU. Install it manually.")
    runtime_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".runtime")
    os.makedirs(runtime_dir, exist_ok=True)
    target_name = "cloudflared.exe" if asset_name.endswith(".exe") else "cloudflared"
    target_path = os.path.join(runtime_dir, target_name)
    if os.path.isfile(target_path) and os.path.getsize(target_path) > 1024:
        return target_path

    def request_bytes(url: str, limit: int) -> bytes:
        request = urllib.request.Request(url, headers={"User-Agent": "FBI-SPLUS-WebPanel/4.7.22", "Accept": "application/vnd.github+json"})
        with urllib.request.urlopen(request, timeout=18) as response:
            data = response.read(limit + 1)
        if len(data) > limit:
            raise RuntimeError("cloudflared download exceeds the safe size limit")
        return data

    metadata = json.loads(request_bytes("https://api.github.com/repos/cloudflare/cloudflared/releases/latest", 2_000_000).decode("utf-8"))
    asset = next((item for item in metadata.get("assets", []) if item.get("name") == asset_name), None)
    if not asset:
        raise RuntimeError("Official cloudflared release asset was not found: " + asset_name)
    expected = str(asset.get("digest") or "")
    if not expected.startswith("sha256:"):
        raise RuntimeError("GitHub did not provide a SHA-256 digest; refusing unverified executable download")
    url = str(asset.get("browser_download_url") or "")
    if not url.startswith("https://github.com/cloudflare/cloudflared/releases/download/"):
        raise RuntimeError("Unexpected cloudflared download URL")
    archive_bytes = request_bytes(url, 60_000_000)
    actual = "sha256:" + hashlib.sha256(archive_bytes).hexdigest()
    if not secrets.compare_digest(actual.casefold(), expected.casefold()):
        raise RuntimeError("cloudflared SHA-256 verification failed")

    if asset_name.endswith(".tgz"):
        import io
        found = None
        with tarfile.open(fileobj=io.BytesIO(archive_bytes), mode="r:gz") as archive:
            for member in archive.getmembers():
                if os.path.basename(member.name) == "cloudflared" and member.isfile() and member.size <= 60_000_000:
                    stream = archive.extractfile(member)
                    if stream is not None:
                        found = stream.read(60_000_001)
                    break
        if not found or len(found) > 60_000_000:
            raise RuntimeError("cloudflared executable not found in the verified archive")
        with open(target_path + ".tmp", "wb") as handle:
            handle.write(found)
    else:
        with open(target_path + ".tmp", "wb") as handle:
            handle.write(archive_bytes)
    os.replace(target_path + ".tmp", target_path)
    if os.name != "nt":
        os.chmod(target_path, 0o755)
    return target_path


async def _find_cloudflared() -> str | None:
    binary = shutil.which("cloudflared") or shutil.which("cloudflared.exe")
    if binary:
        return binary
    runtime_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".runtime")
    local = os.path.join(runtime_dir, "cloudflared.exe" if os.name == "nt" else "cloudflared")
    if os.path.isfile(local) and os.path.getsize(local) > 1024:
        return local
    if not bool(getattr(config, "WEB_PANEL_AUTO_INSTALL_CLOUDFLARED", True)):
        return None
    try:
        log.info("Public panel tunnel is enabled; downloading official cloudflared and verifying its SHA-256...")
        return await asyncio.wait_for(asyncio.to_thread(_download_cloudflared_binary), timeout=45)
    except Exception as exc:
        log.error("Automatic cloudflared installation failed: %s. Install cloudflared manually or set FBI_WEB_PANEL_BASE_URL to a public HTTPS URL.", exc)
        return None


def _describe_probe_error(exc) -> str:
    """یک توضیح کوتاه و قابل‌فهم از علت شکست بررسی /health می‌سازد."""
    import socket
    if isinstance(exc, urllib.error.HTTPError):
        return "پاسخ HTTP %s از مسیر عمومی" % exc.code
    reason = getattr(exc, "reason", exc)
    if isinstance(exc, socket.gaierror) or isinstance(reason, socket.gaierror):
        return "نام دامنه از روی سرور قابل حل نیست (DNS)"
    if isinstance(exc, TimeoutError) or isinstance(reason, TimeoutError):
        return "مهلت پاسخ تمام شد (timeout)"
    if isinstance(reason, ConnectionRefusedError):
        return "اتصال رد شد"
    return ("%s: %s" % (type(exc).__name__, str(reason)))[:180]


def _probe_public_tunnel(url: str) -> bool:
    """Return True only when the public tunnel can reach this panel's health route."""
    global _LAST_PROBE_ERROR
    try:
        request = urllib.request.Request(
            url.rstrip("/") + "/health",
            headers={"User-Agent": "FBI-SPLUS-WebPanel/4.7.30"},
        )
        with urllib.request.urlopen(request, timeout=8) as response:
            status = int(getattr(response, "status", 200))
        if status == 200:
            _LAST_PROBE_ERROR = ""
            return True
        _LAST_PROBE_ERROR = "پاسخ HTTP %s از مسیر عمومی" % status
        return False
    except Exception as exc:
        _LAST_PROBE_ERROR = _describe_probe_error(exc)
        return False


def tunnel_problem_hint() -> str:
    """یک توضیح کوتاه درباره‌ی علت آماده‌نبودن لینک عمومی، برای مالک و لاگ."""
    if not _TUNNEL_REGISTERED and _TUNNEL_PROCESS is None:
        hint = "cloudflared اجرا نمی‌شود (پیدا نشده یا فرایندش متوقف شده است)."
    elif not _TUNNEL_REGISTERED:
        hint = "cloudflared هنوز به شبکه Cloudflare وصل نشده است؛ اینترنت خروجی یا فایروال سرور را بررسی کنید."
    elif _LAST_PROBE_ERROR:
        hint = "cloudflared وصل است، اما بررسی /health از خود سرور شکست خورد: %s." % _LAST_PROBE_ERROR
    else:
        hint = "تونل در حال راه‌اندازی است؛ چند لحظه بعد دوباره امتحان کنید."
    if _LAST_TUNNEL_ERROR:
        hint += " آخرین خطای cloudflared: " + _LAST_TUNNEL_ERROR[:160]
    return hint


async def _run_tunnel_instance(binary: str, port: int) -> None:
    """Run one quick tunnel; do not publish its URL until public /health works."""
    global _DYNAMIC_BASE_URL, _TUNNEL_PROCESS, _TUNNEL_EVENT, _PUBLIC_TUNNEL_READY
    global _PUBLIC_TUNNEL_HEALTHY, _TUNNEL_REGISTERED, _LAST_PROBE_ERROR
    proc = None
    reader_task = None
    ready_wait = None
    process_wait = None
    attempt_event = asyncio.Event()
    previous_url = ""
    try:
        _TUNNEL_EVENT = attempt_event
        _PUBLIC_TUNNEL_HEALTHY = False
        _TUNNEL_REGISTERED = False
        _LAST_PROBE_ERROR = ""
        proc = await asyncio.create_subprocess_exec(
            binary, "tunnel", "--url", "http://127.0.0.1:%d" % port, "--no-autoupdate",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
        )
        _TUNNEL_PROCESS = proc
        reader_task = asyncio.create_task(_read_tunnel_output(proc))
        ready_wait = asyncio.create_task(attempt_event.wait())
        process_wait = asyncio.create_task(proc.wait())
        done, _ = await asyncio.wait(
            {ready_wait, process_wait}, timeout=40, return_when=asyncio.FIRST_COMPLETED
        )
        if ready_wait in done and _DYNAMIC_BASE_URL and proc.returncode is None:
            previous_url = _DYNAMIC_BASE_URL
            log.info("Quick Tunnel hostname detected (%s); waiting for Cloudflare to route /health before issuing links.", previous_url)
            await asyncio.sleep(2)  # allow edge route/DNS propagation before the first probe
            failed_probes = 0
            was_healthy = False
            while not process_wait.done():
                is_healthy = await asyncio.to_thread(_probe_public_tunnel, previous_url)
                if is_healthy:
                    failed_probes = 0
                    _PUBLIC_TUNNEL_HEALTHY = True
                    if not was_healthy:
                        was_healthy = True
                        log.info("✅ Public web panel verified and ready: %s", previous_url)
                        if _PUBLIC_TUNNEL_READY is not None:
                            _PUBLIC_TUNNEL_READY.set()
                    delay = 90
                else:
                    failed_probes += 1
                    if _PUBLIC_TUNNEL_HEALTHY:
                        log.warning("Public /health probe failed; pausing new link issuance while keeping cloudflared running (%s).", previous_url)
                    _PUBLIC_TUNNEL_HEALTHY = False
                    was_healthy = False
                    log.warning(
                        "Public /health is not reachable (%s consecutive check(s)); no public panel link will be issued yet. "
                        "cloudflared will be kept alive while its connection/retry logic runs. Check DNS/firewall and cloudflared logs if this persists.",
                        failed_probes,
                    )
                    delay = 5
                try:
                    await asyncio.wait_for(asyncio.shield(process_wait), timeout=delay)
                except asyncio.TimeoutError:
                    continue
        else:
            log.warning("Cloudflare Quick Tunnel exited before publishing a hostname, or timed out.")
            if proc.returncode is None:
                try:
                    proc.terminate()
                except Exception:
                    pass
                try:
                    await asyncio.wait_for(asyncio.shield(process_wait), timeout=5)
                except asyncio.TimeoutError:
                    try:
                        proc.kill()
                    except Exception:
                        pass
                    await process_wait
    except asyncio.CancelledError:
        if proc is not None and proc.returncode is None:
            try:
                proc.terminate()
                await asyncio.wait_for(proc.wait(), timeout=5)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
        raise
    except Exception as exc:
        log.warning("Quick Tunnel instance failed: %r", exc)
    finally:
        for task in (ready_wait, process_wait):
            if task is not None and not task.done():
                task.cancel()
        if reader_task is not None:
            try:
                await asyncio.wait_for(reader_task, timeout=3)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                if not reader_task.done():
                    reader_task.cancel()
            except Exception:
                pass
        if _TUNNEL_PROCESS is proc:
            _TUNNEL_PROCESS = None
        if _TUNNEL_EVENT is attempt_event:
            _TUNNEL_EVENT = None
        # A trycloudflare hostname is tied to this process. Never keep serving it
        # as if it were healthy after the connector has stopped.
        _DYNAMIC_BASE_URL = ""
        _PUBLIC_TUNNEL_HEALTHY = False
        _TUNNEL_REGISTERED = False


async def _tunnel_supervisor(port: int, binary: str) -> None:
    global _TUNNEL_SUPERVISOR_TASK
    try:
        while _RUNNER is not None:
            await _run_tunnel_instance(binary, port)
            if _RUNNER is None:
                break
            log.warning(
                "Public tunnel stopped. Retrying in 8 seconds; any old trycloudflare URL "
                "is invalid, so request a fresh owner/group panel link after reconnection."
            )
            await asyncio.sleep(8)
    except asyncio.CancelledError:
        raise
    finally:
        _TUNNEL_SUPERVISOR_TASK = None


async def _start_public_tunnel(port: int):
    global _TUNNEL_SUPERVISOR_TASK, _PUBLIC_TUNNEL_READY, _PUBLIC_TUNNEL_HEALTHY, _DYNAMIC_BASE_URL
    binary = await _find_cloudflared()
    if not binary:
        log.error("Public internet link unavailable: cloudflared was not available. Install it or configure a stable public HTTPS URL.")
        return False
    _DYNAMIC_BASE_URL = ""
    _PUBLIC_TUNNEL_HEALTHY = False
    _PUBLIC_TUNNEL_READY = asyncio.Event()
    _TUNNEL_SUPERVISOR_TASK = asyncio.create_task(_tunnel_supervisor(port, binary))
    try:
        await asyncio.wait_for(_PUBLIC_TUNNEL_READY.wait(), timeout=45)
        return bool(_DYNAMIC_BASE_URL)
    except asyncio.TimeoutError:
        log.error("Quick Tunnel did not pass the public /health check in time. It will keep checking in the background; no public owner/group links should be issued until the log says 'Public web panel verified and ready'. %s", tunnel_problem_hint())
        return False


async def start(client, api, me_guid, dispatch):
    """Start the HTTP panel in the bot's existing asyncio loop."""
    global _CLIENT, _API, _ME_GUID, _DISPATCH, _RUNNER
    if not getattr(config, "WEB_PANEL_ENABLED", True):
        log.info("Web panel disabled by FBI_WEB_PANEL_ENABLED")
        return False
    if web is None:
        log.error("Web panel needs aiohttp; install requirements.txt")
        return False
    if _RUNNER is not None:
        return True
    _CLIENT, _API, _ME_GUID, _DISPATCH = client, api, str(me_guid), dispatch
    app = web.Application(client_max_size=32 * 1024)
    app.router.add_get("/health", _health)
    app.router.add_get("/creator/{creator_id}", _creator_dashboard)
    app.router.add_post("/creator/{creator_id}/action", _creator_action)
    app.router.add_get("/group/{guid}", _dashboard)
    app.router.add_post("/group/{guid}/settings", _save_settings)
    app.router.add_post("/group/{guid}/group-action", _group_action)
    app.router.add_post("/group/{guid}/moderation", _moderation)
    app.router.add_post("/group/{guid}/command", _run_command)
    runner = web.AppRunner(app, access_log=None, shutdown_timeout=10)
    try:
        await runner.setup()
        site = web.TCPSite(runner, host=str(getattr(config, "WEB_PANEL_HOST", "0.0.0.0")),
                           port=int(getattr(config, "WEB_PANEL_PORT", 8080)))
        await site.start()
    except Exception:
        await runner.cleanup()
        log.exception("Could not start web panel")
        return False
    _RUNNER = runner
    if (bool(getattr(config, "WEB_PANEL_TUNNEL_ENABLED", False))
            and not str(getattr(config, "WEB_PANEL_BASE_URL", "") or "").strip()):
        await _start_public_tunnel(int(getattr(config, "WEB_PANEL_PORT", 8080)))
    log.info("Web panel listening on %s:%s (base URL: %s)",
             getattr(config, "WEB_PANEL_HOST", "0.0.0.0"),
             getattr(config, "WEB_PANEL_PORT", 8080), _base_url())
    return True


async def stop():
    global _RUNNER, _TUNNEL_PROCESS, _TUNNEL_TASK, _TUNNEL_SUPERVISOR_TASK
    global _TUNNEL_EVENT, _PUBLIC_TUNNEL_READY, _DYNAMIC_BASE_URL, _PUBLIC_TUNNEL_HEALTHY
    runner, _RUNNER = _RUNNER, None
    if runner is not None:
        try:
            await runner.cleanup()
        except Exception:
            log.exception("Web panel shutdown failed")
    supervisor, _TUNNEL_SUPERVISOR_TASK = _TUNNEL_SUPERVISOR_TASK, None
    if supervisor is not None and not supervisor.done():
        supervisor.cancel()
        try:
            await asyncio.wait_for(supervisor, timeout=8)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            pass
    proc, _TUNNEL_PROCESS = _TUNNEL_PROCESS, None
    if proc is not None and proc.returncode is None:
        try:
            proc.terminate()
            await asyncio.wait_for(proc.wait(), timeout=5)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
    task, _TUNNEL_TASK = _TUNNEL_TASK, None
    if task is not None and not task.done():
        task.cancel()
    _TUNNEL_EVENT = None
    _PUBLIC_TUNNEL_READY = None
    _DYNAMIC_BASE_URL = ""
    _PUBLIC_TUNNEL_HEALTHY = False

