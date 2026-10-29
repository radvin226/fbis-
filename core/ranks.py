# -*- coding: utf-8 -*-
"""مقام‌های FBI؛ مقام واقعی گروه از SplusLib جدا از مقام داخلی نگه داشته می‌شود."""
import config
from core.db import db

USER = 0
ADMIN = 1
RESPONSIVE_ADMIN = 1.5  # ادمین پاسخگوی ربات: مثل ادمین عمل می‌کند؛ بین ادمین و ادمین ویژه
SPECIAL = 2
OWNER = 3
CREATOR = 4
BOT = 5

NAMES = {USER: "کاربر", ADMIN: "ادمین", RESPONSIVE_ADMIN: "ادمین پاسخگوی ربات", SPECIAL: "ادمین ویژه",
         OWNER: "مالک", CREATOR: "سازنده", BOT: "سلطان / FBI"}


def name(level):
    if level in NAMES:
        return NAMES[level]
    return NAMES.get(int(level), "کاربر")


def rank_label(uid, level):
    """نام مقام به‌همراه اسم اختصاصی کاربر (مثلاً ادمین پاسخگوی ربات (زروش))."""
    base = name(level)
    nickname = {str(k): v for k, v in (getattr(config, "NICKNAMES", {}) or {}).items()}.get(str(uid))
    return f"{base} ({nickname})" if nickname else base


def rank(guid, user_guid):
    if not user_guid:
        return USER
    uid = str(user_guid)
    if uid == str(getattr(config, "BOT_GUID", "")):
        return BOT
    if uid in {str(x) for x in getattr(config, "CREATOR_IDS", getattr(config, "SUDOS", []))}:
        return CREATOR
    g = db.group(str(guid)) if guid else {}
    # Both the real SPLUS group owner and a manually assigned FBI owner
    # are OWNER-level inside the bot. A manual owner must never demote the
    # actual chat owner in FBI role checks.
    owner_ids = set()
    if g.get("owner_verified") and g.get("owner") is not None:
        owner_ids.add(str(g.get("owner")))
    if g.get("live_owner_verified") and g.get("live_owner") is not None:
        owner_ids.add(str(g.get("live_owner")))
    if uid in owner_ids:
        return OWNER
    if uid in {str(x) for x in g.get("specials", [])}:
        return SPECIAL
    if uid in {str(x) for x in getattr(config, "RESPONSIVE_ADMIN_IDS", ())}:
        return RESPONSIVE_ADMIN
    if uid in {str(x) for x in g.get("admins", [])}:
        return ADMIN
    return USER


def protected(guid, user_guid):
    return rank(guid, user_guid) > USER
