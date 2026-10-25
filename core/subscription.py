# -*- coding: utf-8 -*-
"""مدیریت اشتراک FBI SPLUS.

اشتراک‌ها محلی و پایدار هستند و فقط SUDO می‌تواند از طریق PV سازنده آن‌ها را
لغو/تمدید کند. گروه‌های زیرپرچم مستقل از لغو سراسری هستند.
"""
from __future__ import annotations

import re
import time

import config
from core import api as api_mod, utils
from core.db import db

RENEW_PENDING: dict[str, dict] = {}
GIFT_PENDING: dict[str, dict] = {}


def is_sudo(user_id) -> bool:
    return str(user_id or "") in {str(x) for x in getattr(config, "SUDOS", [])}


def _as_days(value):
    m = re.fullmatch(r"\s*(\d{1,5})(?:\s*روز)?\s*", str(value or ""))
    if not m:
        return None
    days = int(m.group(1))
    if days < 1 or days > 3650:
        return None
    return days


def ensure_subscription_fields(g):
    changed = False
    defaults = {
        "subscription_active": True,
        "subscription_until": 0,
        "subscription_revoked_at": 0,
        "subscription_notice_sent_at": 0,
    }
    for key, value in defaults.items():
        if key not in g:
            g[key] = value
            changed = True
    return changed


def allowed(guid: str) -> bool:
    """آیا FBI اجازه دارد در این گروه فعال بماند؟"""
    g = db.group(str(guid))
    changed = ensure_subscription_fields(g)
    if g.get("subordinate"):
        if changed:
            db.save("groups")
        return True
    if not bool(g.get("subscription_active")):
        if g.get("active"):
            g["active"] = False
            changed = True
        if changed:
            db.save("groups")
        return False
    until = float(g.get("subscription_until") or 0)
    if until and time.time() >= until:
        g["subscription_active"] = False
        g["active"] = False
        g["manual_disabled"] = True
        if not g.get("subscription_revoked_at"):
            g["subscription_revoked_at"] = time.time()
        changed = True
    if changed:
        db.save("groups")
    return bool(g.get("subscription_active")) or bool(g.get("subordinate"))


def expiry_text(g) -> str:
    until = float(g.get("subscription_until") or 0)
    if not g.get("subscription_active"):
        return "غیرفعال"
    if not until:
        return "فعال — بدون تاریخ پایان"
    remaining = max(0, int(until - time.time()))
    if remaining <= 0:
        return "منقضی‌شده"
    return f"فعال — {utils.human_duration(remaining)} باقی‌مانده"


def group_deactivation_message(guid: str) -> str:
    gid = str(guid)
    return (
        "🚫 FBI در این گروه غیرفعال شد.\n\n"
        "💳 اشتراک FBI برای این گروه لغو شده است.\n"
        f"💰 تمدید ۱ ماهه: {utils.fa_digits(50000)} تومان\n\n"
        "📩 برای تمدید، به PV سازنده FBI مراجعه کنید و شناسه زیر را بفرستید:\n"
        f"`تمدید اشتراک {gid}`\n\n"
        "🆔 شناسه گروه:\n"
        f"`{gid}`"
    )


async def cancel_one(api, guid: str) -> str:
    """لغو اشتراک یک گروه مشخص؛ همان اثر دکمهٔ «لغو» در پنل وب. برمی‌گرداند: عنوان گروه."""
    gid = str(guid)
    g = db.group(gid)
    now = time.time()
    g["subscription_active"] = False
    g["subscription_until"] = 0
    g["subscription_revoked_at"] = now
    g["subscription_notice_sent_at"] = now
    g["active"] = False
    g["manual_disabled"] = True
    g["subordinate"] = False
    db.save("groups")
    try:
        voice_id = await api.active_voice_id(gid)
        if voice_id:
            await api.voice_discard(gid, voice_id)
    except Exception:
        pass
    try:
        await api.send(gid, group_deactivation_message(gid))
    except Exception:
        pass
    return str(g.get("title") or gid)


async def cancel_all(api, *, include_inactive=False):
    """لغو سراسری همه گروه‌ها به جز زیرپرچم‌ها."""
    changed = 0
    targets = []
    now = time.time()
    for guid, g in db.data["groups"].items():
        ensure_subscription_fields(g)
        if g.get("subordinate"):
            continue
        was_on = bool(g.get("active") or g.get("subscription_active"))
        g["subscription_active"] = False
        g["subscription_until"] = 0
        g["subscription_revoked_at"] = now
        g["subscription_notice_sent_at"] = now
        g["active"] = False
        g["manual_disabled"] = True
        if was_on or include_inactive:
            targets.append(str(guid))
            changed += 1
    db.save("groups")

    sent = failed = 0
    for guid in targets:
        # توقف کالِ جاری، در حد توان API؛ شکست این مرحله نباید کل لغو را خراب کند.
        try:
            voice_id = await api.active_voice_id(guid)
            if voice_id:
                await api.voice_discard(guid, voice_id)
        except Exception:
            pass
        try:
            result = await api.send(guid, group_deactivation_message(guid))
            if result is not None:
                sent += 1
            else:
                failed += 1
        except Exception:
            failed += 1
    return changed, sent, failed


def mark_renew_pending(owner_id: str, guid: str):
    RENEW_PENDING[str(owner_id)] = {"guid": str(guid), "created": time.time()}


def pending(owner_id: str):
    item = RENEW_PENDING.get(str(owner_id))
    if not item:
        return None
    if time.time() - float(item.get("created") or 0) > 600:
        RENEW_PENDING.pop(str(owner_id), None)
        return None
    return item


def clear_pending(owner_id: str):
    RENEW_PENDING.pop(str(owner_id), None)


def mark_gift_all_pending(owner_id: str):
    GIFT_PENDING[str(owner_id)] = {"mode": "all", "created": time.time()}

def mark_gift_one_pending(owner_id: str, guid: str):
    GIFT_PENDING[str(owner_id)] = {"mode": "one", "guid": str(guid), "created": time.time()}

def pending_gift(owner_id: str):
    item = GIFT_PENDING.get(str(owner_id))
    if not item:
        return None
    if time.time() - float(item.get("created") or 0) > 600:
        GIFT_PENDING.pop(str(owner_id), None)
        return None
    return item

def clear_gift_pending(owner_id: str):
    GIFT_PENDING.pop(str(owner_id), None)


def _gift_until(g: dict, days: int, now: float):
    current_until = float(g.get("subscription_until") or 0)
    start = max(now, current_until) if current_until else now
    return start + int(days) * 86400


async def gift_one(api, guid: str, days: int):
    preview = await group_preview(api, str(guid))
    if not preview:
        raise ValueError("گروه شناسایی نشد.")
    gid = preview["guid"]
    g = db.group(gid)
    now = time.time()
    g["subscription_until"] = _gift_until(g, days, now)
    g["subscription_active"] = True
    g["subscription_revoked_at"] = 0
    g["subscription_notice_sent_at"] = 0
    g["manual_disabled"] = False
    g["active"] = True
    g["opened_at"] = g.get("opened_at") or now
    db.save("groups")
    return preview, g["subscription_until"]


async def gift_all(api, days: int):
    now = time.time()
    changed = 0
    skipped = 0
    for gid, g in db.data.get("groups", {}).items():
        try:
            ensure_subscription_fields(g)
            if g.get("subordinate"):
                # هدیه برای زیرپرچم هم فعال می‌ماند؛ سیستم اشتراک روی آن محدودیتی ندارد.
                g["subscription_active"] = True
                g["manual_disabled"] = False
                g["active"] = True
                changed += 1
                continue
            g["subscription_until"] = _gift_until(g, days, now)
            g["subscription_active"] = True
            g["subscription_revoked_at"] = 0
            g["subscription_notice_sent_at"] = 0
            g["manual_disabled"] = False
            g["active"] = True
            g["opened_at"] = g.get("opened_at") or now
            changed += 1
        except Exception:
            skipped += 1
    db.save("groups")
    sent = 0
    failed = 0
    for gid, g in db.data.get("groups", {}).items():
        try:
            msg = (
                "🎁 اشتراک هدیه FBI فعال شد.\n\n"
                f"⏳ مدت هدیه: {utils.fa_digits(days)} روز\n"
                "🤖 ربات FBI دوباره فعال است.\n"
            )
            result = await api.send(str(gid), msg)
            if result is not None:
                sent += 1
            else:
                failed += 1
        except Exception:
            failed += 1
    return changed, sent, failed, skipped


async def group_preview(api, guid: str):
    """گروه را با API زنده یا رکورد معتبر دیتابیس شناسایی می‌کند."""
    gid = str(guid).strip()
    if not re.fullmatch(r"-?\d+", gid):
        return None
    known = gid in db.data.get("groups", {})
    g = db.group(gid) if known else {}
    info = None
    snap = None
    try:
        info = await api.group_info(gid)
    except Exception:
        info = None
    try:
        snap = await api._role_snapshot(gid, fresh=True)
    except Exception:
        snap = None

    live_ok = bool(info) or bool(snap and (snap.get("enumerated") or snap.get("role_scan_ok")))
    if not live_ok and not known:
        return None

    title = (
        api_mod.deep(info, "group", "group_title", default=None)
        or api_mod.deep(info, "chat", "title", default=None)
        or g.get("title")
        or "گروه نامشخص"
    )
    owner = None
    if snap and snap.get("owner_verified") and snap.get("owner_source") == "live_creator" and snap.get("owner") is not None:
        owner = snap.get("owner")
    if owner is None and g.get("owner_verified") and g.get("owner_source") in {"live_creator", "manual_owner"}:
        owner = g.get("owner")
    if owner is not None:
        owner = str(owner)
    return {"guid": gid, "title": str(title), "owner": owner, "group": g, "live": live_ok}


async def renew(api, guid: str, days: int):
    gid = str(guid)
    g = db.group(gid)
    now = time.time()
    current_until = float(g.get("subscription_until") or 0)
    # در تمدید، روزهای جدید از پایان اشتراک فعلی حساب می‌شوند اگر هنوز معتبر باشد.
    start = max(now, current_until) if g.get("subscription_active") and current_until else now
    g["subscription_until"] = start + int(days) * 86400
    g["subscription_active"] = True
    g["subscription_revoked_at"] = 0
    g["subscription_notice_sent_at"] = 0
    g["manual_disabled"] = False
    g["active"] = True
    g["opened_at"] = g.get("opened_at") or now
    db.save("groups")
    return g["subscription_until"]
