# -*- coding: utf-8 -*-
"""مقام‌های FBI؛ مقام واقعی گروه از SplusLib جدا از مقام داخلی نگه داشته می‌شود."""
import config
from core.db import db

USER = 0
ADMIN = 1
SPECIAL = 2
ZAROOSH = 2.5  # زروش (ادمین سازنده): بین ادمین ویژه و مالک؛ دسترسی‌های PV سازنده را ندارد
OWNER = 3
CREATOR = 4
BOT = 5

NAMES = {USER: "کاربر", ADMIN: "ادمین", SPECIAL: "ادمین ویژه", ZAROOSH: "زروش", OWNER: "مالک", CREATOR: "سازنده", BOT: "سلطان / FBI"}


def name(level):
    if level in NAMES:
        return NAMES[level]
    return NAMES.get(int(level), "کاربر")


def rank(guid, user_guid):
    if not user_guid:
        return USER
    uid = str(user_guid)
    if uid == str(getattr(config, "BOT_GUID", "")):
        return BOT
    if uid in {str(x) for x in getattr(config, "CREATOR_IDS", getattr(config, "SUDOS", []))}:
        return CREATOR
    if uid in {str(x) for x in getattr(config, "CREATOR_ADMIN_IDS", ())}:
        return ZAROOSH  # زروش: مقامی جدا از مالک و سازنده
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
    if uid in {str(x) for x in g.get("admins", [])}:
        return ADMIN
    return USER


def protected(guid, user_guid):
    return rank(guid, user_guid) > USER
