# -*- coding: utf-8 -*-
"""Context layer for FBI SPLUS."""
from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace

from core import api as api_mod
from core import msgstyle, ranks, utils
from core.db import db

log = logging.getLogger("fbi.ctx")


def _get(obj, *keys, default=None):
    if obj is None:
        return default
    for key in keys:
        if isinstance(obj, dict):
            if key in obj and obj[key] is not None:
                return obj[key]
        else:
            try:
                value = getattr(obj, key)
            except Exception:
                continue
            if value is not None:
                return value
    return default


def _reply_sender_deep(msg):
    """Extract the sender of the replied message from heterogeneous SplusLib payloads."""
    seen = set()
    reply_keys = (
        "reply_to", "reply_to_message", "reply_message", "replied_message",
        "reply", "reply_sender", "reply_to_sender", "reply_to_user",
        "reply_author", "reply_from", "aux_data",
    )
    sender_keys = (
        "sender_id", "from_id", "author_id", "author_guid", "sender_guid",
        "reply_sender_id", "reply_to_sender_id", "reply_author_id",
        "reply_to_author_id", "from_user_id",
    )
    def walk(obj, depth=0, inside_reply=False):
        if obj is None or depth > 7 or id(obj) in seen:
            return None
        seen.add(id(obj))
        if inside_reply:
            value = _get(obj, *sender_keys, default=None)
            if value is not None:
                return value
            nested_user = _get(obj, "sender", "author", "from_user", "reply_sender", "reply_to_user", "user", default=None)
            if nested_user is not None:
                value = _get(nested_user, "id", "user_id", "user_guid", "guid", "sender_id", default=None)
                if value is not None:
                    return value
        for key in reply_keys:
            nested = _get(obj, key, default=None)
            if nested is None:
                continue
            found = walk(nested, depth + 1, True)
            if found is not None:
                return found
        return None
    return walk(msg)


def make_update(msg, *, edited=False):
    chat_id = _get(msg, "chat_id")
    sender_id = _get(msg, "sender_id")
    mid = _get(msg, "id", "message_id")
    text = _get(msg, "raw_text", "message", "text", default="") or ""
    reply_id = _get(msg, "reply_to_msg_id", "reply_to_message_id", "reply_message_id", default=None)
    reply_obj = _get(msg, "reply_to_sender", "reply_sender", "reply_to_user", default=None)
    nested_reply = _get(msg, "reply_to", "reply_to_message", "reply_message", "replied_message", default=None)
    if reply_id is None and nested_reply is not None:
        reply_id = _get(nested_reply, "reply_to_msg_id", "reply_to_message_id", "msg_id", "message_id", "id", default=None)
    # Some SplusLib builds keep the actual reply reference under msg.reply_to
    # and expose the id one level deeper. Walk the reply container before giving up.
    if reply_id is None:
        for container_name in ("reply_to", "reply", "reply_info", "reply_to_message"):
            container = _get(msg, container_name, default=None)
            if container is None:
                continue
            reply_id = _get(container, "reply_to_msg_id", "reply_to_message_id", "msg_id", "message_id", "id", default=None)
            if reply_id is not None:
                break

    reply_sender = api_mod.sender_guid_of(reply_obj)
    if reply_sender is None:
        reply_sender = _get(msg, "reply_sender_id", "reply_to_sender_id", "reply_author_id", "reply_to_author_id", default=None)
    if reply_sender is None and reply_obj is not None and not hasattr(reply_obj, "id"):
        reply_sender = reply_obj
    if reply_sender is None and nested_reply is not None:
        reply_sender = api_mod.sender_guid_of(nested_reply)
    if reply_sender is None and isinstance(nested_reply, dict):
        reply_sender = _get(nested_reply, "sender_id", "author_id", "from_id", "user_id", default=None)
    if reply_sender is None:
        reply_sender = _reply_sender_deep(msg)

    explicit_private = _get(msg, "is_private", default=None)
    explicit_group = _get(msg, "is_group", default=None)
    explicit_channel = _get(msg, "is_channel", default=None)
    explicit_private = explicit_private if isinstance(explicit_private, bool) else None
    explicit_group = explicit_group if isinstance(explicit_group, bool) else None
    explicit_channel = explicit_channel if isinstance(explicit_channel, bool) else None

    # SplusLib documents ``msg.is_private`` / ``msg.is_group`` as the
    # authoritative routing flags.  Do not infer a DM from chat_id/sender_id
    # because an incoming private message normally has different ids too.
    ids_differ = chat_id is not None and sender_id is not None and str(chat_id) != str(sender_id)
    same = chat_id is not None and sender_id is not None and str(chat_id) == str(sender_id)
    if explicit_private is True:
        is_private = True
        is_group = False
    elif explicit_group is True:
        is_private = False
        is_group = True
    elif explicit_channel is True:
        is_private = False
        is_group = False
    else:
        # Best-effort fallback only when the library did not provide the
        # documented routing flag.
        is_private = bool(same)
        is_group = bool(ids_differ and not is_private)

    media = _get(msg, "media", default=None)
    file_obj = _get(msg, "file", "document", default=None)
    file_size = _get(file_obj, "size", "file_size", default=0) if file_obj is not None else 0
    file_size = file_size or _get(msg, "file_size", "size", default=0) or 0

    def has(*names):
        for name in names:
            value = _get(msg, name, default=None)
            if value is True:
                return True
            if value not in (None, False, "") and name in {"photo", "video", "gif", "voice", "audio", "music", "sticker", "poll", "contact", "location", "file", "document"}:
                return True
        return False

    metadata = {"meta_data_parts": []}
    for ent in _get(msg, "entities", default=[]) or []:
        if "url" in type(ent).__name__.lower():
            metadata["meta_data_parts"].append({"type": "Link"})

    return SimpleNamespace(
        object_guid=str(chat_id) if chat_id is not None else None,
        author_guid=str(sender_id) if sender_id is not None else None,
        message_id=str(mid) if mid is not None else None,
        reply_message_id=str(reply_id) if reply_id is not None else None,
        reply_sender_id=str(reply_sender) if reply_sender is not None else None,
        text=str(text), raw_text=str(text),
        is_forward=bool(_get(msg, "forward", "fwd_from", "is_forward", default=False)),
        is_group=is_group, is_private=is_private, is_channel=bool(explicit_channel),
        is_me=bool(_get(msg, "is_me", "out", default=False)),
        is_edited=edited or bool(_get(msg, "edit_date", default=False)),
        metadata=metadata, original_update=msg,
        photo=has("photo"), video=has("video"), gif=has("gif", "animation"),
        voice=has("voice"), music=has("audio", "music"),
        file=has("file", "document"), sticker=has("sticker"),
        poll=has("poll"), contact=has("contact"), location=has("location"),
        file_size=file_size, media=media,
    )


# Every user mention in a message is (__[name](link)__). The underline markers are
# one constant so they can be changed in one place if the renderer differs.
USER_UNDERLINE = "__"


def _escape_label(text):
    return str(text).replace("[", "\\[").replace("]", "\\]")


class Ctx:
    def __init__(self, client, api, update, me_guid):
        self.client = client
        self.api = api
        self.update = update
        self.me = str(me_guid) if me_guid is not None else None
        self.guid = str(getattr(update, "object_guid", "")) if getattr(update, "object_guid", None) is not None else None
        self.author = str(getattr(update, "author_guid", "")) if getattr(update, "author_guid", None) is not None else None
        self.message_id = str(getattr(update, "message_id", "") or "")
        self.reply_id = getattr(update, "reply_message_id", None)
        self.reply_sender_id = getattr(update, "reply_sender_id", None)
        self.raw_text = getattr(update, "text", "") or ""
        self.text = utils.normalize(self.raw_text)
        self.key = self.text.lower()
        self.is_group = bool(getattr(update, "is_group", False))
        self.is_private = bool(getattr(update, "is_private", False))
        self.g = db.group(self.guid) if self.is_group and self.guid else {}
        self.rank = ranks.rank(self.guid, self.author) if self.is_group and self.guid else ranks.USER
        self._target = None
        self._target_loaded = False

    async def sender_name(self):
        """Return the current sender's display name without relying on a
        non-existent Context helper. SplusLib message objects expose
        ``get_sender()``; the API adapter caches that result.
        """
        if self.author:
            try:
                getter = getattr(self.update.original_update, "get_sender", None)
                if callable(getter):
                    sender = getter()
                    if asyncio.iscoroutine(sender):
                        sender = await sender
                    if sender is not None:
                        await self.api.prime_user(self.author, sender)
            except Exception as exc:
                log.debug("sender_name lookup failed user=%s: %s", self.author, exc)
        return await self.target_link(self.author)

    def rank_name(self):
        return ranks.name(self.rank)

    def disabled(self, cmd) -> bool:
        if self.rank > ranks.USER:
            return False
        return utils.normalize(cmd) in self.g.get("disabled_cmds", [])

    async def target(self):
        if self._target_loaded:
            return self._target
        self._target_loaded = True

        # Reply target wins over numeric arguments in the command. This is
        # essential for commands such as "سکوت 100", "اخطار 3" and "حذف 10".
        candidate = self.reply_sender_id
        if candidate and str(candidate) != str(self.me):
            self._target = str(candidate)
            return self._target

        original = self.update.original_update
        getter = getattr(original, "get_reply_message", None) if original is not None else None
        if callable(getter):
            try:
                replied = getter()
                if asyncio.iscoroutine(replied):
                    replied = await replied
                val = api_mod.sender_guid_of(replied)
                if val is not None and str(val) != str(self.me):
                    self._target = str(val)
                    # Keep the exact replied message available for later handlers.
                    try:
                        await self.api.cache_reply_message(self.guid, self.reply_id, replied)
                    except Exception:
                        pass
                    return self._target
            except Exception as exc:
                log.debug("reply getter lookup failed chat=%s msg=%s: %s", self.guid, self.reply_id, exc)

        if self.reply_id:
            try:
                getter = getattr(self.api, "get_reply_message", None)
                replied = await getter(self.guid, self.reply_id) if callable(getter) else None
                if replied is not None:
                    val = api_mod.sender_guid_of(replied)
                    if val is not None and str(val) != str(self.me):
                        self._target = str(val)
                        return self._target
            except Exception as exc:
                log.debug("reply object lookup failed chat=%s msg=%s: %s", self.guid, self.reply_id, exc)

            try:
                result = await self.api.get_messages(self.guid, [self.reply_id])
                rows = api_mod.members_of(result) or api_mod._val(result, "messages", default=[]) or []
                for row in rows:
                    val = api_mod.sender_guid_of(row)
                    if val is None:
                        val = api_mod._val(row, "reply_sender_id", "reply_to_sender_id", "author_guid", "sender_id", "from_id", default=None)
                    if val is not None and str(val) != str(self.me):
                        self._target = str(val)
                        return self._target
            except Exception as exc:
                log.debug("reply-target lookup failed chat=%s msg=%s: %s", self.guid, self.reply_id, exc)

        direct = utils.extract_guid(self.raw_text)
        if direct:
            if str(direct).startswith("@"): 
                resolved = await self.api.resolve_user_guid(direct, chat_hint=self.guid if self.is_group else None)
                if resolved and str(resolved) != str(self.me):
                    self._target = str(resolved)
                    return self._target
            elif str(direct) != str(self.me):
                self._target = str(direct)
                return self._target
        return None

    async def refresh_rank(self):
        if not self.is_group or not self.guid or not self.author:
            return self.rank
        aid = str(self.author)
        before = self.rank

        # FBI/SUDO are global identities and do not depend on the group's
        # participant API for their own rank.
        base = ranks.rank(self.guid, aid)
        if base in (ranks.BOT, ranks.CREATOR):
            self.rank = base
            return self.rank

        try:
            # Cached for ROLE_CACHE_SECONDS. A fresh enumeration on every message made
            # each ordinary member's message cost a full admin lookup.
            snap = await self.api._role_snapshot(self.guid, fresh=False)
            owner = str(snap.get("owner")) if snap.get("owner") is not None else None
            live_owner = owner
            if live_owner is not None:
                self.g["live_owner"] = live_owner
                self.g["live_owner_verified"] = bool(snap.get("owner_verified"))
            elif snap.get("role_scan_ok"):
                self.g["live_owner"] = None
                self.g["live_owner_verified"] = False
            admin_ids = {str(x) for x in (snap.get("admins") or [])}
            special_ids = {str(x) for x in (self.g.get("specials") or [])}

            manual_owner = bool(
                self.g.get("owner_verified")
                and self.g.get("owner_source") == "manual_owner"
                and self.g.get("owner") is not None
            )

            # Only a verified chat-wide creator can populate the local owner,
            # مگر اینکه «تنظیم مالک» قبلاً یک مالک داخلی FBI ثبت کرده باشد.
            if not manual_owner:
                if owner is not None and snap.get("owner_verified"):
                    if (self.g.get("owner") != owner or not self.g.get("owner_verified")
                            or self.g.get("owner_source") != "live_creator"):
                        self.g["owner"] = owner
                        self.g["owner_verified"] = True
                        self.g["owner_source"] = "live_creator"
                    self.g["admins"] = [x for x in sorted(admin_ids) if str(x) != owner]
                elif snap.get("role_scan_ok"):
                    # A successful role scan with no creator means we must not keep
                    # a stale/local owner and must not promote the current sender.
                    if self.g.get("owner") is not None or self.g.get("owner_verified") or self.g.get("owner_source"):
                        self.g["owner"] = None
                        self.g["owner_verified"] = False
                        self.g["owner_source"] = ""
                    self.g["admins"] = sorted(admin_ids)

            if manual_owner and aid == str(self.g.get("owner")):
                self.rank = ranks.OWNER
            elif live_owner and aid == live_owner:
                self.rank = ranks.OWNER
            elif aid in special_ids:
                self.rank = ranks.SPECIAL
            elif aid in admin_ids:
                self.rank = ranks.ADMIN
            else:
                self.rank = ranks.USER
            if self.rank != before or owner is not None or self.g.get("live_owner") is not None:
                db.save("groups")
            return self.rank
        except Exception as exc:
            log.debug("refresh_rank failed chat=%s user=%s: %s", self.guid, aid, exc)

        # Last-resort local rank. This fallback can never create ownership.
        self.rank = ranks.rank(self.guid, aid)
        return self.rank

    async def protection_reason(self, guid):
        gid = str(guid or "")
        if not gid:
            return "❌ کاربر هدف مشخص نشد."
        if gid == str(self.me):
            return "❌ این دستور روی خودِ FBI قابل اجرا نیست."
        if gid in {str(x) for x in self.g.get("exempt", [])}:
            return "❌ این کاربر در لیست معاف است و دستور روی او اعمال نمی‌شود."
        if gid in {str(x) for x in getattr(__import__("config"), "SUDOS", [])}:
            return "❌ این کاربر مقام «سودو / سازنده» دارد و دستور روی او اعمال نمی‌شود."
        local = ranks.rank(self.guid, gid)
        if local >= ranks.BOT:
            return f"❌ این کاربر مقام «{ranks.name(local)}» دارد و دستور روی او اعمال نمی‌شود."
        if local > ranks.USER:
            return f"❌ این کاربر مقام «{ranks.name(local)}» دارد و دستور روی او اعمال نمی‌شود."
        try:
            # Cached role (ROLE_CACHE_SECONDS). This runs on ordinary messages; a fresh
            # lookup here cost one network scan per message.
            role = await self.api.user_role(self.guid, gid, fresh=False)
            if role.get("is_owner") and role.get("source") in {"admin-scan", "exact", "permissions"}:
                self.g["owner"] = gid
                self.g["owner_verified"] = True
                self.g["owner_source"] = "live_creator"
                db.save("groups")
                return "❌ این کاربر مالک واقعی گروه است و قابل تنبیه نیست."
            if role.get("is_admin"):
                if gid not in {str(x) for x in self.g.get("admins", [])} and gid not in {str(x) for x in self.g.get("specials", [])}:
                    self.g.setdefault("admins", []).append(gid)
                    db.save("groups")
                level = ranks.SPECIAL if gid in {str(x) for x in self.g.get("specials", [])} else ranks.ADMIN
                return f"❌ این کاربر مقام «{ranks.name(level)}» دارد و دستور روی او اعمال نمی‌شود."
        except Exception:
            pass
        local = ranks.rank(self.guid, gid)
        if local > ranks.USER:
            return f"❌ این کاربر مقام «{ranks.name(local)}» دارد و دستور روی او اعمال نمی‌شود."
        return None

    async def target_name(self, guid=None):
        gid = str(guid or (await self.target() or ""))
        if not gid:
            return "کاربر"
        stored = db.data["users"].get(gid, {})
        info = await self.api.user_info(gid, chat_hint=self.guid if self.is_group else None)
        name = api_mod.name_of(info) if info else "کاربر"
        if name != "کاربر":
            user = db.user(gid)
            user["name"] = name
            user["username"] = api_mod._val(info, "username", "user_name", default=user.get("username", "")) if info else user.get("username", "")
            user["bio"] = api_mod._val(info, "bio", "about", default=user.get("bio", "")) if info else user.get("bio", "")
            db.save("users")
        return name if name != "کاربر" else (stored.get("name") or "کاربر")

    async def target_link(self, guid=None, fallback_name=None):
        """Clickable user label: (__[name](link)__), with the nickname before it when set.

        The nickname (لقب) is shown unless the user made it private (privacy_nick = self).
        """
        gid = str(guid or (await self.target() or ""))
        if not gid:
            return fallback_name or "کاربر"
        stored = db.data["users"].get(gid, {})
        info = await self.api.user_info(gid, chat_hint=self.guid if self.is_group else None)
        username = api_mod._val(info, "username", "user_name", default=None) if info else None
        username = username or stored.get("username")
        if fallback_name:
            name = fallback_name
        elif info:
            name = api_mod.name_of(info)
        else:
            name = stored.get("name") or "کاربر"
        name = _escape_label(name or "کاربر")
        if username:
            url = f"https://splus.ir/{str(username).lstrip('@')}"
        else:
            # tg://user?id= keeps the name clickable for users without a public username.
            url = f"tg://user?id={gid}"
        label = f"({USER_UNDERLINE}[{name}]({url}){USER_UNDERLINE})"
        nick = str(stored.get("nick") or "").strip()
        if nick and stored.get("privacy_nick") != "self":
            return f"{_escape_label(nick)} {label}"
        return label

    async def reply(self, text, reply=True, mention=None):
        if self.g.get("sleep") and self.is_group:
            return None
        body = str(text)
        if mention is not None:
            body = body.replace("{user}", await self.target_link(mention))
        return await self.api.send(self.guid, body, reply_to=self.message_id if reply and self.message_id else None)

    async def ok(self, text, mention=None):
        return await self.reply(msgstyle.status(self.g, "ok", text), mention=mention)

    async def err(self, text):
        return await self.reply(msgstyle.status(self.g, "err", text))

    async def _auto_delete(self, ids, delay):
        try:
            await asyncio.sleep(delay)
            await self.api.delete(self.guid, ids)
        except Exception:
            pass

    async def status_update(self, store_key, text):
        """Edit a single per-group status message; do nothing when state is unchanged."""
        if not self.guid:
            return None
        bucket = self.g.setdefault("status_messages", {})
        item = bucket.get(str(store_key)) or {}
        state = str(text)
        if item.get("state") == state:
            return None
        mid = item.get("message_id")
        result = None
        if mid:
            try:
                result = await self.api.edit(self.guid, mid, state)
            except Exception:
                result = None
        if result is None:
            result = await self.api.send(self.guid, state, reply_to=None)
            mid = self.api.result_message_id(result)
        bucket[str(store_key)] = {"message_id": mid, "state": state, "updated_at": __import__("time").time()}
        db.save("groups")
        return result

    async def delete_this(self):
        if self.message_id:
            await self.api.delete(self.guid, [self.message_id])
