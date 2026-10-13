# -*- coding: utf-8 -*-
"""FBI SPLUS API adapter - public SplusLib API only.

هدف این فایل این است که تمام قابلیت‌های قدیمی FBI از یک لایه‌ی پایدار استفاده
کنند و هیچ کدام برای role/permissions به _client یا TL داخلی SplusLib دست نزنند.
"""
from __future__ import annotations

import asyncio
import inspect
from datetime import datetime
import logging
import os
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Any

import config
from core.db import db

log = logging.getLogger("fbi.api")


def _val(obj: Any, *keys: str, default=None):
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


def deep(obj: Any, *keys: str, default=None):
    cur = obj
    for key in keys:
        cur = _val(cur, key, default=None)
        if cur is None:
            return default
    return cur


def _id(value):
    if value is None:
        return None
    try:
        return int(value)
    except Exception:
        return str(value)


def _as_int(value, default=0):
    try:
        return int(value)
    except Exception:
        return default


def name_of(obj) -> str:
    if obj is None:
        return "کاربر"
    first = _val(obj, "first_name", "first", "name", default="") or ""
    last = _val(obj, "last_name", "last", default="") or ""
    full = (str(first).strip() + " " + str(last).strip()).strip()
    if full:
        return full
    username = _val(obj, "username", "user_name", default="") or ""
    return ("@" + str(username).lstrip("@")) if username else "کاربر"


def guid_of(obj):
    """Return the primary object id. For a User object this is its user id;
    for a Message this is intentionally the message id. Use sender_guid_of()
    when the caller needs the author of a message/reply.
    """
    if obj is None:
        return None
    return _val(obj, "id", "user_id", "user_guid", "member_id", "author_id", "sender_id", "guid")


def sender_guid_of(obj):
    """Return the sender GUID of a message/reply across SplusLib object shapes.

    Reply payloads vary between SplusLib builds: some expose ``reply_to_message``,
    some ``reply_to``/``reply_sender``, and some nest the user under ``sender``
    or ``author``.  This resolver only follows reply/message/user containers and
    never treats a bare message ``id`` as the sender id.
    """
    if obj is None:
        return None

    direct_keys = (
        "sender_id", "from_id", "author_id", "author_guid", "sender_guid",
        "from_user_id", "reply_sender_id", "reply_to_sender_id",
        "reply_author_id", "reply_to_author_id", "user_id", "user_guid",
    )
    value = _val(obj, *direct_keys, default=None)
    if value is not None:
        return value

    containers = (
        "sender", "from_user", "author", "from_user_obj", "user",
        "reply_sender", "reply_to_sender", "reply_to_user",
        "reply", "reply_to", "reply_to_message", "reply_message",
        "replied_message", "message", "data", "result", "aux_data",
    )
    seen=set()
    def walk(value, depth=0):
        if value is None or depth > 6 or id(value) in seen:
            return None
        seen.add(id(value))
        direct = _val(value, *direct_keys, default=None)
        if direct is not None:
            return direct
        for key in containers:
            nested = _val(value, key, default=None)
            if nested is None:
                continue
            if key in {"sender", "from_user", "author", "user", "reply_sender", "reply_to_sender", "reply_to_user", "from_user_obj"}:
                candidate = _val(nested, "id", "user_id", "user_guid", "guid", "sender_id", default=None)
                if candidate is not None:
                    return candidate
            found = walk(nested, depth + 1)
            if found is not None:
                return found
        return None
    return walk(obj)


# صفحه‌بندی فهرست کامل اعضا با درخواست خام GetParticipants (حداکثر ۲۰۰ در هر صفحه).
MEMBER_PAGE_SIZE = 200
MEMBER_SCAN_MAX_PAGES = 60


def members_of(res):
    if res is None:
        return []
    if isinstance(res, list):
        return res
    value = _val(res, "members", "users", "results", "data", "participants", "requests", default=[])
    if isinstance(value, dict):
        value = list(value.values())
    return value if isinstance(value, list) else []


def _records_from_members(rows):
    out = []
    for row in rows or []:
        uid = guid_of(row)
        if uid is None:
            continue
        out.append({
            "id": str(uid),
            "first_name": str(_val(row, "first_name", "first", "display_name", "full_name", "name", default="") or ""),
            "last_name": str(_val(row, "last_name", "last", default="") or ""),
            "username": str(_val(row, "username", "user_name", default="") or ""),
            "is_admin": bool(_val(row, "is_admin", "admin", default=False)),
            "is_owner": bool(_val(row, "is_owner", "owner", "creator", "is_creator", default=False)),
            "raw": row,
        })
    return out


def _recursive_find(obj, wanted_keys, depth=0):
    if depth > 5 or obj is None:
        return None
    wanted = {x.lower() for x in wanted_keys}
    if isinstance(obj, dict):
        for k, v in obj.items():
            if str(k).lower() in wanted and v is not None:
                return v
        for v in obj.values():
            found = _recursive_find(v, wanted, depth + 1)
            if found is not None:
                return found
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            found = _recursive_find(v, wanted, depth + 1)
            if found is not None:
                return found
    else:
        for key in wanted:
            try:
                value = getattr(obj, key)
            except Exception:
                continue
            if value is not None:
                return value
    return None


def _recursive_find_real_title(obj, wanted_keys, generic_values, depth=0, seen=None):
    """Find a non-placeholder chat title in nested API payloads.

    Unlike _recursive_find, a generic outer field like title="گروه" must not
    stop the search before a real nested title is reached.
    """
    if obj is None or depth > 8:
        return None
    if seen is None:
        seen = set()
    if id(obj) in seen:
        return None
    seen.add(id(obj))
    wanted = tuple(str(key).casefold() for key in wanted_keys)
    rejected = {str(value).strip().casefold() for value in generic_values}

    def valid(value):
        return isinstance(value, str) and value.strip() and value.strip().casefold() not in rejected

    if isinstance(obj, dict):
        # Prefer semantically precise fields regardless of dict insertion order.
        for wanted_key in wanted:
            for key, value in obj.items():
                if str(key).casefold() == wanted_key and valid(value):
                    return value.strip()
        for value in obj.values():
            found = _recursive_find_real_title(value, wanted_keys, generic_values, depth + 1, seen)
            if found:
                return found
    elif isinstance(obj, (list, tuple)):
        for value in obj[:500]:
            found = _recursive_find_real_title(value, wanted_keys, generic_values, depth + 1, seen)
            if found:
                return found
    else:
        # Read public attributes even when the library model uses __slots__.
        for key in wanted:
            try:
                value = getattr(obj, key, None)
            except Exception:
                value = None
            if valid(value):
                return value.strip()
        attrs = getattr(obj, "__dict__", None)
        if isinstance(attrs, dict):
            found = _recursive_find_real_title(attrs, wanted_keys, generic_values, depth + 1, seen)
            if found:
                return found
        for key in ("chat", "peer", "entity", "conversation", "channel", "chat_info", "group", "group_info", "data", "result", "object", "raw", "info"):
            try:
                value = getattr(obj, key, None)
            except Exception:
                value = None
            if value is not None and value is not obj:
                found = _recursive_find_real_title(value, wanted_keys, generic_values, depth + 1, seen)
                if found:
                    return found
    return None


def _is_real_creator_participant(part):
    """Strictly identify a real group creator/owner participant."""
    if part is None:
        return False
    cls = type(part).__name__.lower()
    if "participantcreator" in cls or cls.endswith("creator"):
        return True
    role = str(_val(part, "role", "rank", "title", default="") or "").strip().lower()
    if role in {"creator", "owner", "مالک", "سازنده"} or role.endswith("_creator"):
        return True
    for key in ("is_creator", "is_owner"):
        try:
            if getattr(part, key) is True:
                return True
        except Exception:
            pass
    status = _val(part, "status", default=None)
    if status is not None and "creator" in type(status).__name__.lower():
        return True
    return False


def _owner_id_from_metadata(obj):
    """Extract an owner/creator ID from nested chat metadata when available."""
    direct = _recursive_find(obj, ("owner_id", "owner_guid", "creator_id", "creator_guid"))
    if direct not in (None, "", False):
        candidate = _val(direct, "id", "user_id", "user_guid", "guid", default=direct)
        if isinstance(candidate, (int, str)) and str(candidate).strip().lstrip("-").isdigit():
            return str(candidate)
    nested = _recursive_find(obj, ("owner", "creator"))
    if nested is not None and not isinstance(nested, (bool, int, float, str)):
        candidate = _val(nested, "id", "user_id", "user_guid", "guid", default=None)
        if candidate is not None and str(candidate).strip().lstrip("-").isdigit():
            return str(candidate)
    return None


_DIALOG_WARM = {}

async def _warm_dialogs_once(raw, *, cooldown=20):
    if raw is None:
        return False
    now = time.monotonic()
    key = id(raw)
    if now - _DIALOG_WARM.get(key, 0) < cooldown:
        return True
    try:
        getter = getattr(raw, "get_dialogs", None)
        if not callable(getter):
            return False
        result = getter(limit=300)
        if inspect.isawaitable(result):
            await result
        _DIALOG_WARM[key] = now
        return True
    except Exception as exc:
        log.debug("dialog warm failed: %s", exc)
        return False


# Lock name (key used by modules/locks.py) -> server message rights the lock closes
# for ordinary members. Text, mentions and spam have no single server right, so they
# stay local (the bot still deletes them).
LOCK_RIGHTS = {
    "link": ("send_links", "embed_links"),
    "hyperlink": ("embed_links",),
    "photo": ("send_photos",),
    "video": ("send_videos", "send_video_notes"),
    "gif": ("send_gifs", "send_animations"),
    "voice": ("send_voices", "send_voice_notes"),
    "music": ("send_audios",),
    "file": ("send_documents", "send_docs"),
    "sticker": ("send_stickers",),
    "poll": ("send_polls",),
    "contact": ("send_contacts",),
    "location": ("send_locations",),
}
_ALL_LOCK_RIGHTS = frozenset(r for rights in LOCK_RIGHTS.values() for r in rights)


class API:
    def __init__(self, client):
        self.client = client
        self._role_cache = {}
        self._member_cache = {}
        self._member_cache_ttl = 20.0
        self._user_cache = {}
        self._voice_sessions = {}
        self._voice_ids = {}
        self._voice_links = {}
        self._voice_tasks = {}
        self._bio_cache = {}
        self._bio_locks = {}
        # SplusLib resolves raw numeric -100... IDs through its entity cache.
        # Incoming Message objects already carry a resolved chat/sender entity,
        # so we cache those objects and reuse them for every later API call.
        self._chat_refs = {}
        self._user_refs = {}
        self._reply_refs = {}

    def _chat_key(self, guid):
        return str(_id(guid))

    def _user_key(self, guid):
        return str(_id(guid))

    def _chat_arg(self, guid):
        # SplusLib public methods document chat_id as int/str. Do not inject
        # custom/full entities here; keep entity objects only for the raw role resolver.
        return _id(guid)

    def _user_arg(self, guid):
        return _id(guid)

    async def _raw_chat_entity(self, guid):
        raw = getattr(self.client, "_client", None)
        if raw is None:
            return None
        ref = self._chat_refs.get(self._chat_key(guid))
        # Best path: the incoming event gave us the real resolved chat object.
        if ref is not None:
            for resolver in ("get_input_entity", "get_entity"):
                fn = getattr(raw, resolver, None)
                if not callable(fn):
                    continue
                try:
                    ent = fn(ref)
                    if inspect.isawaitable(ent):
                        ent = await ent
                    if ent is not None:
                        return ent
                except Exception:
                    continue
        # Next path: try the normal public identifier after warming dialogs.
        for attempt in range(2):
            for resolver in ("get_input_entity", "get_entity"):
                fn = getattr(raw, resolver, None)
                if not callable(fn):
                    continue
                try:
                    ent = fn(_id(guid))
                    if inspect.isawaitable(ent):
                        ent = await ent
                    if ent is not None:
                        return ent
                except Exception:
                    continue
            if attempt == 0:
                await _warm_dialogs_once(raw)
        return None

    async def _raw_user_entity(self, guid):
        raw = getattr(self.client, "_client", None)
        if raw is None:
            return None
        ref = self._user_refs.get(self._user_key(guid))
        for value in (ref, _id(guid)):
            if value is None:
                continue
            for resolver in ("get_input_entity", "get_entity"):
                fn = getattr(raw, resolver, None)
                if not callable(fn):
                    continue
                try:
                    ent = fn(value)
                    if inspect.isawaitable(ent):
                        ent = await ent
                    if ent is not None:
                        return ent
                except Exception:
                    continue
        return None

    async def prime_message(self, message):
        """Seed SplusLib's entity-aware path from a real incoming message.

        SplusLib documents ``get_chat()``, ``get_sender()`` and
        ``get_reply_message()`` on message objects. Using these keeps all
        subsequent high-level API calls on the same resolved entities instead
        of repeatedly asking Telethon to resolve a raw -100... id.
        """
        if message is None:
            return
        chat_id = _val(message, "chat_id", default=None)
        sender_id = _val(message, "sender_id", default=None)

        getter = getattr(message, "get_chat", None)
        if callable(getter) and chat_id is not None:
            try:
                chat = getter()
                if inspect.isawaitable(chat):
                    chat = await chat
                if chat is not None:
                    self._chat_refs[self._chat_key(chat_id)] = chat
            except Exception as exc:
                log.debug("prime chat failed chat=%s: %s", chat_id, exc)

        getter = getattr(message, "get_sender", None)
        if callable(getter) and sender_id is not None:
            try:
                sender = getter()
                if inspect.isawaitable(sender):
                    sender = await sender
                if sender is not None:
                    self._user_refs[self._user_key(sender_id)] = sender
                    self._remember(sender)
            except Exception as exc:
                log.debug("prime sender failed user=%s: %s", sender_id, exc)

        reply_getter = getattr(message, "get_reply_message", None)
        if callable(reply_getter):
            try:
                replied = reply_getter()
                if inspect.isawaitable(replied):
                    replied = await replied
                if replied is not None:
                    reply_mid = _val(replied, "id", "message_id", default=None)
                    if reply_mid is None:
                        reply_mid = _val(message, "reply_to_msg_id", "reply_to_message_id", default=None)
                    if chat_id is not None and reply_mid is not None:
                        await self.cache_reply_message(chat_id, reply_mid, replied)
                    rid = _val(replied, "sender_id", "author_id", default=None)
                    if rid is not None:
                        rgetter = getattr(replied, "get_sender", None)
                        sender = rgetter() if callable(rgetter) else None
                        if inspect.isawaitable(sender):
                            sender = await sender
                        if sender is not None:
                            self._user_refs[self._user_key(rid)] = sender
                            self._remember(sender)
            except Exception as exc:
                log.debug("prime reply failed: %s", exc)

    async def prime_chat(self, guid, entity=None):
        if entity is not None:
            self._chat_refs[self._chat_key(guid)] = entity
            return entity
        return self._chat_refs.get(self._chat_key(guid))

    async def prime_user(self, guid, entity=None):
        if entity is not None:
            self._user_refs[self._user_key(guid)] = entity
            self._remember(entity)
            return entity
        return self._user_refs.get(self._user_key(guid))

    async def cache_reply_message(self, chat_guid, message_id, message):
        if chat_guid is None or message_id in (None, "") or message is None:
            return message
        key = (self._chat_key(chat_guid), str(message_id))
        self._reply_refs[key] = message
        # Bound the cache so a busy group cannot grow it forever.
        if len(self._reply_refs) > 2000:
            for old_key in list(self._reply_refs)[:500]:
                self._reply_refs.pop(old_key, None)
        return message

    async def get_reply_message(self, chat_guid, message_id):
        if chat_guid is None or message_id in (None, ""):
            return None
        key = (self._chat_key(chat_guid), str(message_id))
        cached = self._reply_refs.get(key)
        if cached is not None:
            return cached
        try:
            item = await self.call("get_message_by_id", self._chat_arg(chat_guid), _as_int(message_id, message_id))
            if item is not None:
                await self.cache_reply_message(chat_guid, message_id, item)
            return item
        except Exception as exc:
            log.debug("get_reply_message failed chat=%s msg=%s: %s", chat_guid, message_id, exc)
            return None

    async def call(self, names, *args, **kwargs):
        if isinstance(names, str):
            names = [names]
        last_exc = None
        for name in names:
            fn = getattr(self.client, name, None)
            if not callable(fn):
                continue
            try:
                result = fn(*args, **kwargs)
                if inspect.isawaitable(result):
                    result = await result
                return result
            except TypeError as exc:
                last_exc = exc
                log.debug("signature mismatch %s: %s", name, exc)
                continue
            except Exception as exc:
                last_exc = exc
                db.log("api", f"{name}: {type(exc).__name__}: {exc}")
                raise
        if last_exc:
            raise last_exc
        return None

    # ---------------- Messages ----------------
    @staticmethod
    def _utf16_len(value):
        return len(str(value).encode("utf-16-le")) // 2

    @staticmethod
    def _spoiler_entity_class():
        # SplusLib ships a Telethon-style vendored TL layer.  Keep imports
        # local so the rest of the bot can still start on builds that do not
        # expose this type.
        try:
            from spluslib._base.tl.types import MessageEntitySpoiler
            return MessageEntitySpoiler
        except Exception:
            pass
        try:
            from spluslib._base import types
            return getattr(types, "MessageEntitySpoiler", None)
        except Exception:
            return None

    async def send_spoiler(self, guid, visible_prefix, hidden_text, reply_to=None):
        """Send a native text spoiler using MessageEntitySpoiler.

        The visible prefix remains readable while only ``hidden_text`` is
        covered by the spoiler entity.  ``||...||`` is used only as a final
        fallback for installations whose vendored TL layer lacks the entity.
        """
        prefix = str(visible_prefix)
        hidden = str(hidden_text)
        text = prefix + hidden
        entity_cls = self._spoiler_entity_class()
        raw = getattr(self.client, "_client", None)
        raw_send = getattr(raw, "send_message", None) if raw is not None else None
        if entity_cls is not None and callable(raw_send):
            try:
                entity = entity_cls(
                    offset=self._utf16_len(prefix),
                    length=self._utf16_len(hidden),
                )
                kwargs = {
                    "formatting_entities": [entity],
                    "parse_mode": None,
                    "link_preview": False,
                }
                if reply_to is not None:
                    kwargs["reply_to"] = _as_int(reply_to, reply_to)
                return await raw_send(self._chat_arg(guid), text, **kwargs)
            except Exception as exc:
                log.debug("native text spoiler failed: %s", exc)
        # Compatibility fallback; native entity is always preferred.
        return await self.send(
            guid, f"{prefix}||{hidden}||",
            reply_to=reply_to,
        )

    async def send(self, guid, text, reply_to=None, **kwargs):
        kwargs = dict(kwargs)
        buttons = kwargs.pop("buttons", None)
        if reply_to is not None:
            kwargs["reply_to"] = _as_int(reply_to, reply_to)
        kwargs.setdefault("parse_mode", "md")
        kwargs.setdefault("link_preview", True)

        # SplusLib's public SplusClient.send_message() wrapper in some builds
        # does not expose `buttons`, while its underlying SoroushClient does.
        # Use the underlying client only for messages that actually contain
        # inline keyboards; ordinary messages remain on the public wrapper.
        if buttons is not None:
            raw = getattr(self.client, "_client", None)
            raw_send = getattr(raw, "send_message", None) if raw is not None else None
            if callable(raw_send):
                return await raw_send(self._chat_arg(guid), str(text), buttons=buttons, **kwargs)
            # Forward-compatible fallback for builds that expose buttons on
            # the high-level wrapper itself.
            kwargs["buttons"] = buttons
        return await self.call("send_message", self._chat_arg(guid), str(text), **kwargs)

    async def send_photo(self, guid, photo, caption=None, reply_to=None):
        kwargs = {"progress": False}
        if caption is not None:
            kwargs["caption"] = caption
        if reply_to is not None:
            kwargs["reply_to"] = _as_int(reply_to, reply_to)
        return await self.call("send_photo", self._chat_arg(guid), photo, **kwargs)

    async def send_music(self, guid, path, caption=None, reply_to=None):
        kwargs = {"progress": False}
        if caption is not None:
            kwargs["caption"] = caption
        if reply_to is not None:
            kwargs["reply_to"] = _as_int(reply_to, reply_to)
        return await self.call(["send_audio", "send_file", "send_document"], self._chat_arg(guid), path, **kwargs)

    async def edit(self, guid, message_id, text):
        return await self.call("edit_message", self._chat_arg(guid), _as_int(message_id, message_id), str(text))

    async def react(self, guid, message_id, reaction=None):
        """React to a message; pass None to remove the current reaction."""
        return await self.call("react_message", self._chat_arg(guid), _as_int(message_id, message_id), reaction)

    def unsupported_rights(self, names):
        """Names from ``names`` that the installed edit_permissions does not accept.

        Empty when the signature cannot be read or takes **kwargs (then nothing is known
        to be missing).
        """
        raw = getattr(self.client, "_client", None)
        fn = getattr(raw, "edit_permissions", None) if raw is not None else None
        if not callable(fn):
            return []
        params = self._signature_params(fn)
        if not params or any(p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values()):
            return []
        return sorted(n for n in names if n not in params)

    def delete_supported(self):
        """True when this Splus build exposes delete_messages.

        ``call()`` returns None silently for a missing method, so callers that must
        know whether a deletion happened check this first.
        """
        return callable(getattr(self.client, "delete_messages", None))

    async def delete(self, guid, message_ids, kind="Global"):
        if not message_ids:
            return True
        ids = [_as_int(x, x) for x in (message_ids if isinstance(message_ids, (list, tuple, set)) else [message_ids])]
        return await self.call("delete_messages", self._chat_arg(guid), ids)

    async def pin(self, guid, message_id, action="Pin"):
        if str(action).lower().startswith("un"):
            return await self.call("unpin_message", self._chat_arg(guid), _as_int(message_id, message_id))
        return await self.call("pin_message", self._chat_arg(guid), _as_int(message_id, message_id))

    async def forward(self, from_guid, to_guid, message_ids):
        ids = [_as_int(x, x) for x in message_ids]
        return await self.call("forward_messages", self._chat_arg(from_guid), self._chat_arg(to_guid), ids)

    async def get_messages(self, guid, message_ids):
        out = []
        for mid in (message_ids if isinstance(message_ids, (list, tuple, set)) else [message_ids]):
            item = await self.call("get_message_by_id", self._chat_arg(guid), _as_int(mid, mid))
            if item:
                out.append(item)
        return {"messages": out}

    @staticmethod
    def _poll_option_bytes(index):
        return f"fbi-{int(index)}".encode("utf-8")

    @staticmethod
    def _tl_text_with_entities(types_mod, value):
        cls = getattr(types_mod, "TextWithEntities", None)
        if cls is None:
            return value
        try:
            return cls(text=str(value), entities=[])
        except Exception:
            return value

    async def send_poll(self, guid, question, options, *, quiz=False, correct_option_index=None,
                        is_anonymous=False, multiple_choice=False, close_period=None, reply_to=None,
                        explanation=None):
        """Send a REAL SPLUS poll/quiz through the documented high-level API first.

        SplusLib/RubPy-style clients expose ``create_poll`` with the same
        semantics used by Soroush Plus: ``type`` is ``Regular`` or ``Quiz``.
        The raw TL path remains only as a compatibility fallback.
        """
        opts = [str(x).strip()[:100] for x in (options or []) if str(x).strip()]
        if len(opts) < 2:
            raise ValueError("poll needs at least two options")
        if len(opts) > 10:
            opts = opts[:10]
        if quiz and correct_option_index is None:
            raise ValueError("quiz requires correct_option_index")
        if quiz and not 0 <= int(correct_option_index) < len(opts):
            raise ValueError("invalid correct option index")
        close_period = int(close_period) if close_period else None

        # DOCUMENTED SplusLib path: create_poll().  This is the preferred path
        # for Soroush Plus because the server itself creates the real clickable
        # Regular/Quiz poll instead of rendering a fake inline keyboard.
        creator = getattr(self.client, "create_poll", None)
        if callable(creator) and not quiz:
            try:
                kwargs = {
                    "type": "Quiz" if quiz else "Regular",
                    "is_anonymous": bool(is_anonymous),
                    "allows_multiple_answers": bool(multiple_choice),
                    "correct_option_index": int(correct_option_index) if quiz else None,
                    "explanation": explanation,
                    "reply_to_message_id": str(reply_to) if reply_to else None,
                }
                if close_period is not None:
                    # create_poll does not require a close_period in the
                    # documented contract; keep it only when the installed
                    # implementation explicitly accepts it.
                    kwargs["close_period"] = close_period
                try:
                    params = inspect.signature(creator).parameters
                    if not any(p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values()):
                        kwargs = {k: v for k, v in kwargs.items() if k in params}
                except Exception:
                    pass
                result = creator(self._chat_arg(guid), str(question), opts, **kwargs)
                if inspect.isawaitable(result):
                    result = await result
                return {
                    "ok": True, "native": True, "mode": "create_poll",
                    "message_id": self.result_message_id(result),
                    "poll_id": _val(result, "poll_id", "id", default=None),
                    "result": result,
                }
            except Exception as exc:
                log.debug("documented create_poll unavailable/fails: %r", exc)

        # Compatibility path for client builds that expose send_poll instead.
        public = getattr(self.client, "send_poll", None)
        if callable(public) and not quiz:
            try:
                kwargs = {
                    "is_anonymous": bool(is_anonymous), "multiple_choice": bool(multiple_choice),
                    "quiz": bool(quiz), "correct_option_index": correct_option_index,
                    "close_period": close_period,
                }
                try:
                    params = inspect.signature(public).parameters
                    if not any(p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values()):
                        kwargs = {k: v for k, v in kwargs.items() if k in params}
                except Exception:
                    pass
                result = public(self._chat_arg(guid), str(question), opts, **kwargs)
                if inspect.isawaitable(result):
                    result = await result
                return {"ok": True, "native": True, "message_id": self.result_message_id(result), "result": result}
            except Exception as exc:
                log.debug("public send_poll unavailable/fails: %r", exc)

        raw = getattr(self.client, "_client", None)
        if raw is None:
            raise RuntimeError("native poll API unavailable")
        chat = await self._raw_chat_entity(guid)
        if chat is None:
            raise RuntimeError("chat entity unavailable for poll")
        try:
            from spluslib._base.tl import functions, types
            msg_functions = getattr(functions, "messages", None)
            poll_cls = getattr(types, "Poll", None)
            answer_cls = getattr(types, "PollAnswer", None)
            input_poll_cls = getattr(types, "InputMediaPoll", None)
            send_cls = getattr(msg_functions, "SendMediaRequest", None) if msg_functions else None
            if None in (poll_cls, answer_cls, input_poll_cls, send_cls):
                raise RuntimeError("SplusLib poll TL constructors are unavailable")
            poll_opts = [self._poll_option_bytes(i) for i in range(len(opts))]
            try:
                ap = inspect.signature(answer_cls).parameters
            except Exception:
                ap = {}
            try:
                pp = inspect.signature(poll_cls).parameters
            except Exception:
                pp = {}

            def _entities_needed(params, name):
                try:
                    ann = params.get(name).annotation
                    return getattr(ann, "__name__", "") == "TextWithEntities"
                except Exception:
                    return False

            use_entities = _entities_needed(ap, "text") or _entities_needed(pp, "question")
            def _make_tl_text(value):
                return self._tl_text_with_entities(types, value) if use_entities else str(value)

            answers = []
            for i, option in enumerate(opts):
                answer_kwargs = {"option": poll_opts[i]}
                if not ap or "text" in ap:
                    answer_kwargs["text"] = _make_tl_text(option)
                answers.append(answer_cls(**answer_kwargs))

            poll_kwargs = {"id": int.from_bytes(os.urandom(8), "big") & ((1 << 63) - 1) or 1,
                           "question": _make_tl_text(question), "answers": answers,
                           "public_voters": not bool(is_anonymous),
                           "multiple_choice": bool(multiple_choice), "quiz": bool(quiz)}
            if close_period:
                poll_kwargs["close_period"] = close_period
                poll_kwargs["close_date"] = int(time.time() + close_period)
            if pp and not any(p.kind == inspect.Parameter.VAR_KEYWORD for p in pp.values()):
                poll_kwargs = {k: v for k, v in poll_kwargs.items() if k in pp}
            poll = poll_cls(**poll_kwargs)
            media_kwargs = {"poll": poll}
            correct = poll_opts[int(correct_option_index)] if quiz else None
            if quiz and correct is not None:
                media_kwargs["correct_answers"] = [correct]
            try:
                ip = inspect.signature(input_poll_cls).parameters
                if ip and not any(p.kind == inspect.Parameter.VAR_KEYWORD for p in ip.values()):
                    media_kwargs = {k: v for k, v in media_kwargs.items() if k in ip}
            except Exception:
                pass
            media = input_poll_cls(**media_kwargs)
            try:
                sp = inspect.signature(send_cls).parameters
            except Exception:
                sp = {}
            peer = chat
            try:
                get_input = getattr(raw, "get_input_entity", None)
                if callable(get_input):
                    peer = get_input(chat)
                    if inspect.isawaitable(peer):
                        peer = await peer
            except Exception:
                peer = chat
            req_kwargs = {"peer": peer, "media": media, "message": "",
                          "random_id": int.from_bytes(os.urandom(8), "big") & ((1 << 63) - 1) or 1}
            if sp and not any(p.kind == inspect.Parameter.VAR_KEYWORD for p in sp.values()):
                req_kwargs = {k: v for k, v in req_kwargs.items() if k in sp}
            req = send_cls(**req_kwargs)
            result = raw(req)
            if inspect.isawaitable(result):
                result = await result
            return {"ok": True, "native": True,
                    "message_id": self.result_message_id(result),
                    "poll_id": str(_val(poll, "id", default="")), "result": result}
        except Exception as exc:
            raise RuntimeError(f"native poll failed: {exc}") from exc

    async def get_poll_results(self, guid, message_id):
        """Best-effort readback of a native poll's current yes/no/option counts."""
        result = await self.get_messages(guid, message_id)
        rows = members_of(result)
        if not rows:
            return None
        msg = rows[0]
        media_poll = self._find_nested(msg, ("poll",))
        poll_results = self._find_nested(msg, ("results",))
        if poll_results is None:
            poll_results = self._find_nested(msg, ("poll_results",))
        poll_answers = _val(media_poll, "answers", default=[]) or []
        result_rows = _val(poll_results, "results", default=[]) or []
        counts = [0] * len(poll_answers)
        for rr in result_rows:
            opt = _val(rr, "option", default=None)
            voters = _val(rr, "voters", "count", default=0) or 0
            idx = None
            for i, ans in enumerate(poll_answers):
                aopt = _val(ans, "option", default=None)
                if opt == aopt:
                    idx = i
                    break
            if idx is None:
                try:
                    idx = int.from_bytes(opt, "big") if isinstance(opt, (bytes, bytearray)) else int(opt)
                    if idx >= len(counts) and idx > 0:
                        idx -= 1
                except Exception:
                    idx = None
            if idx is not None and 0 <= idx < len(counts):
                counts[idx] = int(voters)
        total = _val(poll_results, "total_voters", "total", default=None)
        if total is None:
            total = sum(counts)
        return {"counts": counts, "yes": counts[0] if counts else 0,
                "no": counts[1] if len(counts) > 1 else 0, "total": int(total or 0),
                "message_id": _as_int(message_id, message_id)}

    async def get_last_messages(self, guid, middle_id=None):
        max_id = _as_int(middle_id, 0) if middle_id else 0
        rows = await self.call("get_messages", self._chat_arg(guid), 100, max_id=max_id)
        return {"messages": rows or []}

    # ---------------- Identity/cache ----------------
    def _remember(self, row):
        uid = guid_of(row)
        if uid is None:
            return
        uid = str(uid)
        self._user_cache[uid] = {
            "id": uid,
            "first_name": str(_val(row, "first_name", "first", "display_name", "full_name", "name", default="") or ""),
            "last_name": str(_val(row, "last_name", "last", default="") or ""),
            "username": str(_val(row, "username", "user_name", default="") or ""),
            "bio": str(_val(row, "bio", "about", default="") or ""),
            "is_admin": bool(_val(row, "is_admin", "admin", default=False)),
            "is_owner": bool(_val(row, "is_owner", "owner", "creator", "is_creator", default=False)),
        }
        user = db.user(uid)
        for key in ("first_name", "last_name", "username", "bio"):
            if self._user_cache[uid].get(key):
                user["name"] = name_of(row)
                if key == "username":
                    user["username"] = self._user_cache[uid][key]
                elif key == "bio":
                    user["bio"] = self._user_cache[uid][key]
        db.save("users")

    async def _raw_members(self, guid):
        """فهرست کامل اعضا را با درخواست خام و صفحه‌بندی می‌گیرد.

        همان خانواده‌ی درخواستی است که اسکنر مالک/ادمین به آن تکیه دارد. روی
        گروه‌های بزرگ، get_chat_members با limit بزرگ گاهی هیچ عضوی برنمی‌گرداند.
        اگر مسیر خام در دسترس نباشد، None برمی‌گرداند تا مسیر قدیمی امتحان شود.
        """
        raw = getattr(self.client, "_client", None)
        if raw is None:
            return None
        entity = await self._raw_chat_entity(guid)
        if entity is None:
            return None
        try:
            from spluslib._base.tl import functions, types as raw_types
        except Exception:
            return None
        req_cls = getattr(getattr(functions, "channels", None), "GetParticipantsRequest", None)
        recent_cls = getattr(raw_types, "ChannelParticipantsRecent", None)
        if req_cls is None or recent_cls is None:
            return None

        out, seen, offset = [], set(), 0
        for _page in range(MEMBER_SCAN_MAX_PAGES):
            result = raw(req_cls(channel=entity, filter=recent_cls(), offset=offset,
                                 limit=MEMBER_PAGE_SIZE, hash=0))
            if inspect.isawaitable(result):
                result = await result
            participants = _val(result, "participants", default=[]) or []
            users = _val(result, "users", default=[]) or []
            if not participants:
                break
            users_by_id = {str(_val(u, "id", "user_id", default="")): u for u in users}
            for part in participants:
                uid = _val(part, "user_id", "participant_id", "member_id", "id", default=None)
                if uid is None:
                    nested = _val(part, "peer", "user", "participant", default=None)
                    uid = _val(nested, "user_id", "id", "user_guid", "guid", default=None)
                if uid is None:
                    continue
                sid = str(uid)
                if sid in seen:
                    continue
                seen.add(sid)
                user = users_by_id.get(sid) or self._user_refs.get(self._user_key(sid)) or {}
                is_owner = _is_real_creator_participant(part)
                is_admin = bool(is_owner or getattr(part, "admin_rights", None) is not None
                                or "admin" in type(part).__name__.lower())
                out.append({
                    "id": sid,
                    "first_name": str(_val(user, "first_name", "first", default="") or ""),
                    "last_name": str(_val(user, "last_name", "last", default="") or ""),
                    "username": str(_val(user, "username", "user_name", default="") or ""),
                    "is_admin": is_admin,
                    "is_owner": is_owner,
                })
            offset += len(participants)
            if len(participants) < MEMBER_PAGE_SIZE:
                break
            await asyncio.sleep(0.3)
        return out or None

    async def _cache_chat_members(self, guid, force=False):
        key = str(_id(guid))
        now = time.monotonic()
        old = self._member_cache.get(key)
        if not force and old and now - old[0] < self._member_cache_ttl:
            return old[1]

        rows = None
        try:
            rows = await self._raw_members(guid)
        except Exception as exc:
            log.debug("raw member scan failed chat=%s: %r", guid, exc)

        for limit in (5000, 200):
            if rows:
                break
            try:
                rows = members_of(await self.call("get_chat_members", self._chat_arg(guid),
                                                  limit=limit, filter_admins=False))
            except Exception as exc:
                log.debug("get_chat_members(limit=%s) failed chat=%s: %r", limit, guid, exc)
                rows = []

        rows = list(rows or [])
        for row in rows:
            self._remember(row)
        records = _records_from_members(rows)
        if records:
            self._member_cache[key] = (now, records)
        else:
            self._member_cache.pop(key, None)
            db.log("members", f"chat={guid}: raw scan and get_chat_members both returned no members")
        return records

    async def resolve_user_guid(self, value, chat_hint=None):
        """Resolve numeric ID or @username to a numeric Splus user id."""
        if value is None:
            return None
        raw_value = str(value).strip()
        if raw_value.lstrip("-").isdigit():
            return raw_value
        username = raw_value.lstrip("@").strip()
        if not username:
            return None
        # Prefer already cached group members.
        if chat_hint:
            try:
                rows = await self._cache_chat_members(chat_hint, force=True)
                for row in rows:
                    u = str(_val(row, "username", "user_name", default="") or "").lstrip("@")
                    if u.lower() == username.lower():
                        uid = guid_of(row)
                        if uid is not None:
                            self._remember(row)
                            return str(uid)
            except Exception:
                pass
        # Public client/entity fallback.
        for source in (self.client, getattr(self.client, "_client", None)):
            if source is None:
                continue
            for method in ("get_entity", "get_user", "get_user_info"):
                fn = getattr(source, method, None)
                if not callable(fn):
                    continue
                try:
                    obj = fn(username)
                    if inspect.isawaitable(obj):
                        obj = await obj
                    uid = guid_of(obj)
                    if uid is not None:
                        self._user_refs[self._user_key(uid)] = obj
                        self._remember(obj)
                        return str(uid)
                except Exception:
                    continue
        return None

    def result_message_id(self, result):
        if result is None:
            return None
        direct = _val(result, "id", "message_id", "msg_id", default=None)
        if direct is not None:
            return str(direct)
        for key in ("message", "messages", "result", "data", "updates"):
            nested = _val(result, key, default=None)
            if nested is None or nested is result:
                continue
            if isinstance(nested, (list, tuple)):
                for item in nested:
                    found = self.result_message_id(item)
                    if found is not None:
                        return found
            else:
                found = self.result_message_id(nested)
                if found is not None:
                    return found
        return None

    async def scan_all_messages(self, guid, *, max_messages=None):
        """Best-effort full history scan using public get_messages pagination."""
        max_messages = int(max_messages or getattr(config, "SCAN_ALL_MAX_MESSAGES", 100000))
        max_messages = max(100, min(max_messages, 500000))
        seen = set()
        rows = []
        cursor = 0
        for _ in range((max_messages // 100) + 5):
            batch = await self.call("get_messages", self._chat_arg(guid), 100, max_id=cursor) if cursor else await self.call("get_messages", self._chat_arg(guid), 100)
            if isinstance(batch, dict):
                batch = _val(batch, "messages", "results", "data", default=[])
            batch = batch or []
            if not isinstance(batch, (list, tuple)) or not batch:
                break
            new = 0
            for msg in batch:
                mid = _val(msg, "id", "message_id", default=None)
                key = str(mid) if mid is not None else repr(msg)
                if key in seen:
                    continue
                seen.add(key); rows.append(msg); new += 1
                if len(rows) >= max_messages:
                    break
            if len(rows) >= max_messages or not new:
                break
            ids = [_as_int(_val(x, "id", "message_id", default=0), 0) for x in batch]
            ids = [x for x in ids if x > 0]
            if not ids:
                break
            nxt = min(ids)
            if nxt <= 0 or nxt == cursor:
                break
            cursor = nxt
            if len(batch) < 100:
                break
        return rows

    @staticmethod
    def _group_permission_fields():
        return (
            "send_messages", "send_media", "send_stickers", "send_gifs", "send_games",
            "send_inline", "embed_links", "send_polls", "send_voices", "send_audios",
            "send_videos", "send_photos", "send_documents", "send_plain", "send_reactions",
            "send_links", "send_contacts", "send_locations", "send_stories", "send_animations",
            "send_video_notes", "send_voice_notes", "send_paid_media", "send_gifts", "manage_topics",
        )

    @classmethod
    def _all_message_permission_fields(cls, params=None, obj=None):
        """Merge known SPLUS rights with new send/embed/manage fields in the installed schema."""
        names = set(cls._group_permission_fields())
        if params:
            try:
                iterable = params.keys()
            except Exception:
                iterable = params
            for name in iterable:
                if name in {"self", "until_date", "flags", "_flags"}:
                    continue
                if str(name).startswith(("send_", "embed_", "manage_")):
                    names.add(str(name))
        if obj is not None:
            try:
                values = vars(obj).keys()
            except Exception:
                values = ()
            for name in values:
                if str(name).startswith(("send_", "embed_", "manage_")):
                    names.add(str(name))
        return tuple(sorted(names))

    @staticmethod
    def _find_nested(obj, keys, depth=0, seen=None):
        if obj is None or depth > 7:
            return None
        if seen is None:
            seen = set()
        try:
            marker = id(obj)
            if marker in seen:
                return None
            seen.add(marker)
        except Exception:
            pass
        value = _val(obj, *keys, default=None)
        if value is not None:
            return value
        if isinstance(obj, dict):
            values = obj.values()
        else:
            try:
                values = vars(obj).values()
            except Exception:
                values = ()
        for child in values:
            found = API._find_nested(child, keys, depth + 1, seen)
            if found is not None:
                return found
        return None

    async def get_group_default_permissions(self, guid):
        """Read current chat-default permissions from SPLUS raw TL.

        Returned values are *allowed=True/False*, matching the public-style
        permission semantics used by ``edit_permissions``.  This is captured
        before a group is closed and later reused verbatim on reopen.
        """
        raw = getattr(self.client, "_client", None)
        if raw is None:
            return None
        chat = await self._raw_chat_entity(guid)
        if chat is None:
            return None

        # First use the documented permission reader. SplusLib exposes
        # get_permissions(entity, user=None) for both a member and the chat
        # defaults; this is preferable to guessing a generated TL request.
        raw = getattr(self.client, "_client", None)
        public_get = getattr(raw, "get_permissions", None) if raw is not None else None
        if callable(public_get):
            try:
                full = public_get(chat, None)
                if inspect.isawaitable(full):
                    full = await full
                if full is not None:
                    allowed = {}
                    for key in self._all_message_permission_fields(obj=full):
                        banned = _val(full, key, default=None)
                        if banned is not None:
                            allowed[key] = not bool(banned)
                    if allowed:
                        return {
                            "captured_at": time.time(),
                            "source": "splus_get_permissions_default",
                            "fields": allowed,
                        }
            except Exception as exc:
                log.debug("public default permission read failed chat=%s: %r", guid, exc)

        try:
            from spluslib._base.tl import functions
            full = None
            messages_mod = getattr(functions, "messages", None)
            channels_mod = getattr(functions, "channels", None)
            candidates = []
            chat_id = _id(guid)
            try:
                candidates.append((messages_mod, "GetFullChatRequest", {"chat_id": int(chat_id)}))
                candidates.append((messages_mod, "GetFullChat", {"chat_id": int(chat_id)}))
            except Exception:
                pass
            candidates.append((channels_mod, "GetFullChannelRequest", {"channel": chat}))
            candidates.append((channels_mod, "GetFullChannel", {"channel": chat}))
            for mod, name, kwargs in candidates:
                cls = getattr(mod, name, None) if mod is not None else None
                if cls is None:
                    continue
                try:
                    req = cls(**kwargs)
                    result = raw(req)
                    if inspect.isawaitable(result):
                        result = await result
                    full = self._find_nested(result, ("default_banned_rights",))
                    if full is not None:
                        break
                except Exception:
                    continue
            if full is None:
                return None
            allowed = {}
            for key in self._all_message_permission_fields(obj=full):
                banned = _val(full, key, default=None)
                if banned is not None:
                    allowed[key] = not bool(banned)
            if not allowed:
                return None
            return {
                "captured_at": time.time(),
                "source": "splus_tl_default_banned_rights",
                "fields": allowed,
            }
        except Exception as exc:
            log.debug("group permission snapshot read failed chat=%s: %r", guid, exc)
            return None

    async def restore_group_default_permissions(self, guid, snapshot):
        """Restore the exact default-permission snapshot saved before close."""
        if not isinstance(snapshot, dict):
            return False
        fields = snapshot.get("fields") if "fields" in snapshot else snapshot
        if not isinstance(fields, dict) or not fields:
            return False
        raw = getattr(self.client, "_client", None)
        if raw is None:
            return False
        chat = await self._raw_chat_entity(guid)
        if chat is None:
            return False

        # Snapshot values are allowed=True/False. Only send_/embed_ rights are
        # restored; manage_* rights are never touched by a group close.
        rights = {
            str(k): bool(v) for k, v in fields.items()
            if str(k).startswith(("send_", "embed_"))
        }
        if not rights:
            return False

        edit_permissions = getattr(raw, "edit_permissions", None)
        if callable(edit_permissions):
            try:
                applied = await self._write_rights(edit_permissions, chat, None, rights)
                if "send_messages" in applied or "send_messages" not in rights:
                    return True
                log.error("group default restore did not accept send_messages chat=%s", guid)
            except Exception as exc:
                log.debug("snapshot edit_permissions restore failed chat=%s: %r", guid, exc)

        # Path 2: raw TL request. ChatBannedRights uses True = restricted.
        try:
            from spluslib._base.tl import functions, types
            msg_functions = getattr(functions, "messages", None)
            cls = (
                getattr(msg_functions, "EditChatDefaultBannedRightsRequest", None)
                or getattr(msg_functions, "EditChatDefaultBannedRights", None)
            )
            rights_cls = getattr(types, "ChatBannedRights", None)
            if cls is None or rights_cls is None:
                return False
            params = self._signature_params(rights_cls) or {}
            accepts_kwargs = any(p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values())
            kwargs = {}
            for key in self._message_right_names(params):
                if key in rights and (not params or accepts_kwargs or key in params):
                    kwargs[key] = not rights[key]
            rights_obj = rights_cls(**kwargs)
            try:
                req = cls(peer=chat, banned_rights=rights_obj)
            except Exception:
                req = cls(channel=chat, banned_rights=rights_obj)
            result = raw(req)
            if inspect.isawaitable(result):
                await result
            return True
        except Exception as exc:
            log.debug("snapshot TL restore failed chat=%s: %r", guid, exc)
            return False

    async def close_group_permissions(self, guid):
        """Close ordinary members' sending on the server. The caller sets the local flag."""
        return await self.sync_group_permissions(guid, closed=True)

    async def open_group_permissions(self, guid):
        """Open the group on the server. A right still needed by a switched-on lock stays closed."""
        return await self.sync_group_permissions(guid, closed=False)

    async def set_group_closed(self, guid, closed=True):
        """Close or open the whole group for ordinary members (the ``ارسال پیام`` switch).

        The local flag is stored too, so the bot's own message filter and the periodic
        restore loop agree with the server.
        """
        g = db.group(str(guid))
        g["closed"] = bool(closed)
        if not closed:
            g["closed_until"] = 0
        db.save("groups")
        return await self.sync_group_permissions(guid, closed=bool(closed))

    async def _write_chat_default_rights(self, guid, rights):
        """Write chat-default rights (True = allowed). True when the server accepted them."""
        raw = getattr(self.client, "_client", None)
        if raw is None:
            return False
        chat = await self._raw_chat_entity(guid)
        if chat is None:
            return False

        # Path 1: edit_permissions(chat, None) targets the chat defaults.
        edit_permissions = getattr(raw, "edit_permissions", None)
        if callable(edit_permissions):
            try:
                applied = await self._write_rights(edit_permissions, chat, None, rights)
                if "send_messages" not in rights or "send_messages" in applied:
                    return True
                log.error("chat default edit did not accept send_messages chat=%s", guid)
            except Exception as exc:
                log.debug("default edit_permissions failed chat=%s: %r", guid, exc)

        # Path 2: raw TL request. ChatBannedRights uses True = restricted.
        try:
            from spluslib._base.tl import functions, types
            msg_functions = getattr(functions, "messages", None)
            cls = (
                getattr(msg_functions, "EditChatDefaultBannedRightsRequest", None)
                or getattr(msg_functions, "EditChatDefaultBannedRights", None)
            )
            rights_cls = getattr(types, "ChatBannedRights", None)
            if cls is None or rights_cls is None:
                return False
            params = self._signature_params(rights_cls) or {}
            accepts_kwargs = any(p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values())
            kwargs = {}
            for key in rights:
                if not key.startswith(("send_", "embed_")):
                    continue
                if not params or accepts_kwargs or key in params:
                    kwargs[key] = not bool(rights[key])
            rights_obj = rights_cls(**kwargs)
            try:
                req = cls(peer=chat, banned_rights=rights_obj)
            except Exception:
                req = cls(channel=chat, banned_rights=rights_obj)
            result = raw(req)
            if inspect.isawaitable(result):
                await result
            return True
        except Exception as exc:
            log.debug("server group default-rights failed chat=%s: %r", guid, exc)
            return False

    @staticmethod
    def _stored_baseline(g):
        """The owner's chat defaults, captured before the bot first restricted anything."""
        for key in ("permissions_baseline", "closed_permissions_snapshot"):
            item = g.get(key)
            if not isinstance(item, dict):
                continue
            fields = item.get("fields") if "fields" in item else item
            if isinstance(fields, dict) and fields:
                return {str(k): bool(v) for k, v in fields.items() if str(k).startswith(("send_", "embed_"))}
        return None

    async def sync_group_permissions(self, guid, closed=None):
        """Make the server's chat-default rights match the bot's group state.

        Ordinary members lose the rights of a closed group and of every switched-on lock
        (LOCK_RIGHTS). Every other right keeps the baseline the owner had before the bot
        first restricted anything, so the owner's own defaults survive. Members in
        ``specials`` (and ``exempt`` while a lock is on) get an explicit allowance for the
        restricted rights, matching the bot's exemption rules. ``closed`` overrides the
        stored flag. Returns True when the server matches the desired state.
        """
        g = db.group(str(guid))
        is_closed = bool(g.get("closed")) if closed is None else bool(closed)
        locked = set()
        for key, on in (g.get("locks") or {}).items():
            if on:
                locked.update(LOCK_RIGHTS.get(key, ()))
        restricting = is_closed or bool(locked)

        baseline = self._stored_baseline(g)
        if restricting and baseline is None:
            snap = await self.get_group_default_permissions(guid)
            fields = dict((snap or {}).get("fields") or {})
            if not fields:
                fields = {n: True for n in self._message_right_names()}
            baseline = fields
            # Same key and shape as the 4.7 close snapshot, so saved data keeps working.
            g["closed_permissions_snapshot"] = {"captured_at": time.time(), "source": "sync", "fields": fields}
            db.save("groups")
        if not restricting and baseline is None:
            return True  # the bot never restricted this group, so there is nothing to restore

        base = baseline or {}
        names = sorted(set(self._message_right_names()) | _ALL_LOCK_RIGHTS)
        if restricting:
            target = {n: (False if is_closed or n in locked else bool(base.get(n, True))) for n in names}
        else:
            target = {n: bool(base.get(n, True)) for n in names}

        fingerprint = ",".join("%s=%d" % (n, int(target[n])) for n in sorted(target))
        current = await self.get_group_default_permissions(guid)
        cur = (current or {}).get("fields") or {}
        in_sync = g.get("permissions_synced") == fingerprint and all(
            cur[n] == target[n] for n in target if n in cur)
        if not in_sync:
            if not await self._write_chat_default_rights(guid, target):
                log.warning("group rights sync failed chat=%s; the restore loop retries", guid)
                return False
            g["permissions_synced"] = fingerprint

        if restricting:
            restricted = sorted(n for n, v in target.items() if not v)
            exempt_ids = {str(x) for x in (g.get("specials") or [])}
            if locked:
                exempt_ids |= {str(x) for x in (g.get("exempt") or [])}
            if g.get("exempt_rights") != restricted:
                g["exempt_rights"] = restricted
                g["exempt_synced"] = []
            done = {str(x) for x in (g.get("exempt_synced") or [])}
            for uid in sorted(exempt_ids - done):
                applied = await self._member_edit_permissions(
                    guid, uid, muted=False, rights={n: True for n in restricted})
                if applied:
                    done.add(uid)
            g["exempt_synced"] = sorted(done)
        else:
            for key in ("permissions_baseline", "closed_permissions_snapshot",
                        "exempt_rights", "exempt_synced"):
                g.pop(key, None)
        db.save("groups")
        return True

    async def user_info(self, guid, chat_hint=None, force=False):
        uid = str(guid)
        if not force and uid in self._user_cache:
            return dict(self._user_cache[uid])
        if force:
            try:
                profile = await self._full_user_profile(uid, live=True)
                if profile:
                    return dict(profile)
            except Exception as exc:
                log.debug("forced user profile failed user=%s: %r", uid, exc)
        if chat_hint:
            try:
                rows = await self._cache_chat_members(chat_hint)
                for row in rows:
                    if str(row.get("id")) == uid:
                        self._remember(row)
                        return dict(self._user_cache.get(uid, row))
            except Exception:
                pass
        stored = db.data.get("users", {}).get(uid)
        if stored and (stored.get("name") or stored.get("username")):
            return {
                "id": uid,
                "first_name": stored.get("name", ""),
                "last_name": "",
                "username": stored.get("username", ""),
                "bio": stored.get("bio", ""),
            }
        # Public SplusClient forwarding fallback; no direct _client/TL access here.
        for name in ("get_user_info", "get_user", "get_entity"):
            fn = getattr(self.client, name, None)
            if not callable(fn):
                continue
            try:
                item = fn(self._user_arg(guid))
                if inspect.isawaitable(item):
                    item = await item
                if item:
                    data = dict(item) if isinstance(item, dict) else {
                        "id": getattr(item, "id", _id(guid)),
                        "first_name": getattr(item, "first_name", "") or "",
                        "last_name": getattr(item, "last_name", "") or "",
                        "username": getattr(item, "username", "") or "",
                        "bio": getattr(item, "bio", "") or getattr(item, "about", "") or "",
                    }
                    self._remember(data)
                    return data
            except Exception:
                continue
        return None

    # ---------------- Group / roles ----------------
    async def group_info(self, guid):
        info = await self.call("get_chat_info", self._chat_arg(guid))
        info = info or {}
        title = _val(info, "title", "chat_title", "group_title", "group_name", "name", default=None)
        # SplusLib response models sometimes expose a generic outer title while
        # the actual chat title is nested in a Chat/peer/entity payload.
        generic_titles = {"", "گروه", "گروه نامشخص", "unknown", "chat", "group", "none", "null"}
        if not isinstance(title, str) or title.strip().casefold() in generic_titles:
            nested_title = _recursive_find_real_title(
                info, ("chat_title", "group_title", "group_name", "title", "chat_name", "display_name", "name"),
                generic_titles,
            )
            if isinstance(nested_title, str) and nested_title.strip().casefold() not in generic_titles:
                title = nested_title.strip()
            else:
                title = "گروه"
        member_count = _val(info, "participants_count", "member_count", "members_count", "count_members", default=0) or 0
        admin_count = _val(info, "admins_count", "count_admins", default=0) or 0
        if not member_count or not admin_count:
            try:
                admins = await self.call("get_chat_members", self._chat_arg(guid), limit=100, filter_admins=True)
                if not admin_count:
                    admin_count = len(members_of(admins))
            except Exception:
                pass
        owner = _recursive_find(info, ("owner_id", "owner_guid", "creator_id", "creator_guid"))
        return {
            "chat": info,
            "group": {
                "group_title": title,
                "count_members": member_count,
                "count_admins": admin_count,
                "group_guid": _val(info, "id", "chat_id", default=guid),
                "description": _val(info, "description", "about", default=""),
            },
            "owner_id": owner,
            "invite_link": _val(info, "invite_link", "join_link", "link", default="") or "",
            "description": _val(info, "description", "about", default="") or "",
        }

    async def _participant_role(self, guid, user_guid):
        """Read a user's real participant record without SplusLib.is_admin()."""
        raw = getattr(self.client, "_client", None)
        if raw is None:
            return None
        uid = _id(user_guid)
        chat = await self._raw_chat_entity(guid)
        user = await self._raw_user_entity(uid)
        if chat is None or user is None:
            return None

        # Channel/supergroup path: exact GetParticipantRequest.
        try:
            from spluslib._base.tl import functions
            ns = getattr(functions, "channels", None)
            req_cls = getattr(ns, "GetParticipantRequest", None)
            if req_cls is not None:
                result = raw(req_cls(channel=chat, participant=user))
                if inspect.isawaitable(result):
                    result = await result
                part = _val(result, "participant", default=None)
                if part is not None:
                    pcls = type(part).__name__.lower()
                    rights = getattr(part, "admin_rights", None)
                    # Only a concrete Creator participant is authoritative.
                    creator = _is_real_creator_participant(part)
                    admin = bool(creator or "admin" in pcls or rights is not None or getattr(part, "is_admin", False))
                    return {"is_owner": creator, "is_admin": admin, "source": "exact", "participant_class": type(part).__name__}
        except Exception as exc:
            log.debug("exact participant lookup failed chat=%s user=%s: %r", guid, uid, exc)

        # Basic group / fallback path.
        try:
            fn = getattr(raw, "get_permissions", None)
            if callable(fn):
                result = fn(chat, user)
                if inspect.isawaitable(result):
                    result = await result
                status = _val(result, "status", default=None)
                cls = type(status).__name__.lower() if status is not None else type(result).__name__.lower()
                creator = "creator" in cls
                rights = getattr(result, "admin_rights", None)
                admin = bool(creator or getattr(result, "is_admin", False) or rights is not None or "admin" in cls)
                return {"is_owner": creator, "is_admin": admin, "source": "permissions", "participant_class": cls}
        except Exception as exc:
            log.debug("permission lookup failed chat=%s user=%s: %r", guid, uid, exc)

        return None

    async def _role_snapshot(self, guid, *, fresh=False):
        """Return verified group roles.

        Important SplusLib detail:
        ``get_chat_members()`` serializes only User objects, so creator/admin
        participant classes are not preserved in its public dictionaries.  The
        only reliable way available with the current SplusLib/TL stack is:

        1. enumerate the *real* admin IDs using ``ChannelParticipantsAdmins``;
        2. keep the original User entity for each ID;
        3. inspect the participant classes from that same response;
        4. mark the user as owner only when the participant is a real
           ``ChannelParticipantCreator`` (or equivalent creator/owner status).

        We deliberately never infer owner from "activated user", first admin,
        or local database state.
        """
        key = str(_id(guid))
        now = time.monotonic()
        ttl = max(2, int(getattr(config, "ROLE_CACHE_SECONDS", 20)))
        cached = self._role_cache.get(key)
        if not fresh and cached and now - cached[0] < ttl:
            return cached[1]

        owner = None
        admins = set()
        records_by_id = {}
        raw = getattr(self.client, "_client", None)
        entity = await self._raw_chat_entity(guid)

        def remember_user(user, *, is_admin=False, is_owner=False, role_class=""):
            uid = guid_of(user)
            if uid is None:
                return None
            uid = str(uid)
            if is_admin:
                admins.add(uid)
            if is_owner:
                nonlocal owner
                owner = uid
                admins.add(uid)
            records_by_id[uid] = {
                "id": uid,
                "first_name": str(_val(user, "first_name", "first", default="") or ""),
                "last_name": str(_val(user, "last_name", "last", default="") or ""),
                "username": str(_val(user, "username", "user_name", default="") or ""),
                "is_admin": bool(is_admin or is_owner),
                "is_owner": bool(is_owner),
                "role_class": role_class,
            }
            try:
                self._user_refs[self._user_key(uid)] = user
                self._remember(user)
            except Exception:
                pass
            return uid

        # ---- Primary: raw participants request preserves Creator/Admin participant classes.
        # SplusLib's public iter_participants() intentionally returns User objects and
        # loses the participant subclass, which is why previous owner detection could
        # only ever see "admin candidates".
        admin_candidates = {}
        verified_from_scan = set()
        role_scan_ok = False
        if raw is not None and entity is not None:
            try:
                from spluslib._base.tl import functions, types as raw_types
                req_cls = getattr(getattr(functions, "channels", None), "GetParticipantsRequest", None)
                admin_filter_cls = getattr(raw_types, "ChannelParticipantsAdmins", None)
                if req_cls is not None and admin_filter_cls is not None:
                    result = raw(req_cls(
                        channel=entity,
                        filter=admin_filter_cls(),
                        offset=0,
                        limit=200,
                        hash=0,
                    ))
                    if inspect.isawaitable(result):
                        result = await result
                    participants = _val(result, "participants", default=[]) or []
                    users = _val(result, "users", default=[]) or []
                    users_by_id = {str(_val(u, "id", "user_id", default="")): u for u in users}
                    # An empty admin response cannot prove that a group has no
                    # admins/owner (every normal group has at least its creator).
                    # Treat it as an unavailable scan so the fallback path can run.
                    role_scan_ok = isinstance(participants, (list, tuple)) and bool(participants)
                    for part in participants:
                        uid = _val(part, "user_id", "participant_id", "member_id", "id", default=None)
                        if uid is None:
                            nested = _val(part, "peer", "user", "participant", default=None)
                            uid = _val(nested, "user_id", "id", "user_guid", "guid", default=None)
                        if uid is None:
                            continue
                        sid = str(uid)
                        user = users_by_id.get(sid) or self._user_refs.get(self._user_key(sid))
                        if user is None:
                            try:
                                user = await self._raw_user_entity(sid)
                            except Exception:
                                user = None
                        if user is None:
                            user = {"id": sid}
                        pcls = type(part).__name__
                        low = pcls.lower()
                        role_value = str(_val(part, "role", "rank", "title", "status", default="") or "").lower()
                        rights = getattr(part, "admin_rights", None)
                        is_owner = _is_real_creator_participant(part)
                        is_admin = bool(is_owner or "admin" in low or rights is not None or
                                        getattr(part, "is_admin", False) or "admin" in role_value)
                        if is_admin:
                            remember_user(user, is_admin=is_admin, is_owner=is_owner, role_class=pcls)
                            admin_candidates[sid] = user
                            verified_from_scan.add(sid)
                    log.info("raw role scan chat=%s participants=%s owner=%s admins=%s", guid, len(participants), owner, len(admin_candidates))
            except Exception as exc:
                log.warning("raw admin/creator scan failed chat=%s: %r", guid, exc)
                role_scan_ok = False

        # Basic-group fallback: GetFullChatRequest keeps ChatParticipantCreator/Admin
        # objects when ChannelParticipantsAdmins is not applicable.
        if not role_scan_ok and raw is not None and entity is not None:
            try:
                from spluslib._base.tl import functions as _f, types as _t
                msg_ns = getattr(_f, "messages", None)
                full_cls = getattr(msg_ns, "GetFullChatRequest", None)
                ent_id = getattr(entity, "id", None)
                if full_cls is not None and ent_id is not None:
                    full_result = raw(full_cls(chat_id=int(ent_id)))
                    if inspect.isawaitable(full_result):
                        full_result = await full_result
                    full_chat = _val(full_result, "full_chat", default=None)
                    participants_obj = _val(full_chat, "participants", default=None)
                    participants = _val(participants_obj, "participants", default=[]) or []
                    users = _val(full_result, "users", default=[]) or []
                    users_by_id = {str(_val(u, "id", "user_id", default="")): u for u in users}
                    if participants:
                        role_scan_ok = True
                    for part in participants:
                        uid = _val(part, "user_id", "participant_id", "member_id", "id", default=None)
                        if uid is None:
                            nested = _val(part, "peer", "user", "participant", default=None)
                            uid = _val(nested, "user_id", "id", "user_guid", "guid", default=None)
                        if uid is None:
                            continue
                        sid = str(uid)
                        user = users_by_id.get(sid) or self._user_refs.get(self._user_key(sid)) or {"id": sid}
                        pcls = type(part).__name__
                        low = pcls.lower()
                        role_value = str(_val(part, "role", "rank", "title", "status", default="") or "").lower()
                        is_owner = _is_real_creator_participant(part)
                        rights = getattr(part, "admin_rights", None)
                        is_admin = bool(is_owner or "admin" in low or rights is not None or "admin" in role_value)
                        if is_admin:
                            remember_user(user, is_admin=True, is_owner=is_owner, role_class=pcls)
                            admin_candidates[sid] = user
                            verified_from_scan.add(sid)
                    log.info("basic role scan chat=%s participants=%s owner=%s admins=%s", guid, len(participants), owner, len(admin_candidates))
            except Exception as exc:
                log.debug("basic full-chat role scan failed chat=%s: %r", guid, exc)

        # Fallback: public admin list gives candidates; exact role lookup may still
        # identify roles on builds where raw GetParticipantsRequest is unavailable.

        # Public API fallback: it returns admin User dicts, enough to obtain IDs
        # which are then verified with the exact participant request above.
        if not admin_candidates:
            try:
                rows = await self.call("get_chat_members", self._chat_arg(guid), limit=None, filter_admins=True)
                for row in members_of(rows):
                    uid = guid_of(row)
                    if uid is None:
                        continue
                    sid = str(uid)
                    admin_candidates[sid] = row
                    remember_user(row, is_admin=True, role_class="AdminCandidate")
            except Exception as exc:
                log.warning("public admin candidate scan failed chat=%s: %r", guid, exc)

        # Verify every candidate.  This is the piece the previous versions were
        # missing: the role class lives on the participant RPC response, not the
        # public User dict.
        verified = len(verified_from_scan)
        for uid, user_entity in list(admin_candidates.items()):
            # If GetParticipantsRequest already gave us the participant subclass,
            # that is the authoritative role. Do not issue a second RPC.
            if uid in verified_from_scan:
                continue
            try:
                self._user_refs[self._user_key(uid)] = user_entity
                role = await self._participant_role(guid, uid)
                if role:
                    verified += 1
                    is_owner = bool(role.get("is_owner"))
                    is_admin = bool(role.get("is_admin") or is_owner)
                    remember_user(
                        user_entity,
                        is_admin=is_admin,
                        is_owner=is_owner,
                        role_class=str(role.get("participant_class") or role.get("source") or "Verified"),
                    )
                    log.info(
                        "verified candidate chat=%s user=%s class=%s owner=%s admin=%s source=%s",
                        guid, uid, role.get("participant_class"), is_owner, is_admin, role.get("source")
                    )
            except Exception as exc:
                log.debug("candidate role verify failed chat=%s user=%s: %r", guid, uid, exc)

        # Secondary server hint: some Splus group-info responses expose a
        # creator/owner id even when the admin-list participant class is flattened.
        # We never trust that id by itself: it must pass the exact participant-role
        # check and therefore can never turn an ordinary activator into an owner.
        if owner is None and raw is not None and entity is not None:
            try:
                info_hint = await self.call("get_chat_info", self._chat_arg(guid))
                hinted_owner = _owner_id_from_metadata(info_hint)
                if hinted_owner is not None:
                    hinted_owner = str(hinted_owner)
                    # Strong corroboration: the group-info owner must also be a
                    # member of the real administrator scan. This prevents an
                    # unrelated metadata field from becoming ownership.
                    hinted_role = await self._participant_role(guid, hinted_owner)
                    corroborated = hinted_owner in verified_from_scan or hinted_owner in admin_candidates
                    if hinted_role and hinted_role.get("is_owner") and corroborated:
                        user = self._user_refs.get(self._user_key(hinted_owner)) or await self._raw_user_entity(hinted_owner) or {"id": hinted_owner}
                        remember_user(user, is_admin=True, is_owner=True, role_class=str(hinted_role.get("participant_class") or "Creator"))
                        verified_from_scan.add(hinted_owner)
                        role_scan_ok = True
                        owner = hinted_owner
                        log.info("metadata owner verified chat=%s user=%s", guid, hinted_owner)
                    elif hinted_role and hinted_role.get("is_admin") and corroborated and not owner:
                        # Some Splus builds flatten the creator participant into
                        # an Admin record. When the official group-info owner id
                        # points to one of those already-verified admins, it is
                        # the best available server corroboration.
                        user = self._user_refs.get(self._user_key(hinted_owner)) or await self._raw_user_entity(hinted_owner) or {"id": hinted_owner}
                        remember_user(user, is_admin=True, is_owner=True, role_class="MetadataCreator")
                        verified_from_scan.add(hinted_owner)
                        role_scan_ok = True
                        owner = hinted_owner
                        log.info("flattened metadata owner corroborated chat=%s user=%s", guid, hinted_owner)
            except Exception as exc:
                log.debug("metadata owner verification failed chat=%s: %r", guid, exc)

        # Keep member profiles current without using them to infer ownership.
        if raw is not None and entity is not None and len(records_by_id) < 2:
            try:
                async for user in raw.iter_participants(entity, limit=int(getattr(config, "ROLE_MEMBER_SCAN_LIMIT", 5000))):
                    remember_user(user, is_admin=False, is_owner=False, role_class="Member")
            except Exception as exc:
                log.debug("member profile scan failed chat=%s: %r", guid, exc)

        # Do not infer owner from chat metadata; only creator participant records count.

        member_count = 0
        try:
            info = await self.call("get_chat_info", self._chat_arg(guid))
            member_count = _as_int(_val(info, "participants_count", "member_count", "members_count", "count_members", default=0), 0)
        except Exception:
            member_count = 0
        snapshot = {
            "owner": owner,
            "owner_verified": bool(owner is not None and owner in verified_from_scan and role_scan_ok),
            "owner_source": "live_creator" if (owner is not None and owner in verified_from_scan and role_scan_ok) else "",
            "admins": sorted(admins),
            "members": list(records_by_id.values()),
            "enumerated": bool(records_by_id),
            "verified_roles": verified,
            "role_scan_ok": bool(role_scan_ok),
            "member_count": member_count,
        }
        self._role_cache[key] = (now, snapshot)
        self._member_cache[key] = (now, snapshot["members"])
        log.info(
            "role snapshot chat=%s owner=%s admins=%s verified=%s",
            guid, owner, len(admins), verified
        )
        return snapshot

    async def is_channel_member(self, channel_guid, user_guid, *, fresh=True):
        """بررسی عضویت با چند مسیر واقعی؛ از false-negative برای عضو عادی جلوگیری می‌کند."""
        if not channel_guid or user_guid is None:
            return False
        uid = str(_id(user_guid))

        # مسیر اول: participant دقیق. این مسیر برای ادمین/سازنده و بیشتر اعضا
        # سریع‌ترین و دقیق‌ترین پاسخ را می‌دهد.
        try:
            role = await self._participant_role(channel_guid, user_guid)
        except Exception as exc:
            role = None
            log.debug("channel membership participant lookup failed channel=%s user=%s: %r", channel_guid, user_guid, exc)
        if role:
            cls = str(role.get("participant_class") or "").lower()
            source = str(role.get("source") or "").lower()
            if any(bad in cls for bad in ("banned", "kicked", "left")):
                return False
            if source in {"exact", "permissions"} or role.get("is_admin") or role.get("is_owner"):
                return True

        # مسیر دوم: get_permissions مستقیم. بعضی buildهای Splus برای عضو عادی
        # participant را برنمی‌گردانند ولی permissions را برمی‌گردانند.
        try:
            raw = getattr(self.client, "_client", None)
            chat = await self._raw_chat_entity(channel_guid)
            user = await self._raw_user_entity(user_guid)
            fn = getattr(raw, "get_permissions", None) if raw is not None else None
            if callable(fn) and chat is not None and user is not None:
                result = fn(chat, user)
                if inspect.isawaitable(result):
                    result = await result
                status = _val(result, "status", default=None)
                cls = (type(status).__name__ if status is not None else type(result).__name__).lower()
                if any(bad in cls for bad in ("banned", "kicked", "left")):
                    return False
                # Member/Restricted/Creator/Admin/Participant all mean the user is
                # present. An explicit status object is enough; absence/exception is
                # not treated as a membership denial yet.
                if status is not None or result is not None:
                    return True
        except Exception as exc:
            log.debug("channel permissions membership lookup failed channel=%s user=%s: %r", channel_guid, user_guid, exc)

        # مسیر سوم: فهرست اعضا. این fallback برای زمانی است که GetParticipantRequest
        # روی یک کانال خاص پاسخ مناسبی نمی‌دهد.
        for chat_ref in (channel_guid, "@FBIbotdd"):
            try:
                rows = await self.call("get_chat_members", self._chat_arg(chat_ref) if not str(chat_ref).startswith("@") else chat_ref, limit=5000, filter_admins=False)
                for row in members_of(rows):
                    rid = guid_of(row)
                    if rid is not None and str(rid) == uid:
                        return True
            except Exception as exc:
                log.debug("channel member-list lookup failed channel=%s user=%s: %r", chat_ref, user_guid, exc)

        return False

    async def is_channel_admin(self, channel_guid, user_guid):
        """بررسی می‌کند حساب موردنظر در کانال ادمین/سازنده است."""
        try:
            role = await self.user_role(channel_guid, user_guid, fresh=True)
            return bool(role.get("is_admin") or role.get("is_owner"))
        except Exception as exc:
            log.debug("channel admin lookup failed channel=%s user=%s: %r", channel_guid, user_guid, exc)
            return False

    async def user_role(self, guid, user_guid, *, fresh=True):
        uid = str(_id(user_guid))

        # SUDO is a global FBI creator identity, not a group participant role.
        # Never ask the group participant API to resolve it.
        sudo_ids = {str(x) for x in getattr(config, "SUDOS", [])}
        if uid in sudo_ids:
            return {
                "is_owner": True,
                "is_admin": True,
                "is_sudo": True,
                "source": "global_sudo",
                "participant_class": "GlobalSUDO",
            }

        # PRIMARY: use one fresh participant-admin scan for the chat.  This is
        # important because SplusLib's public ``is_admin()`` path only compares
        # User dictionaries and its exact GetParticipantRequest can return
        # NOT_SUPPORTED for some real members (notably the creator).  The raw
        # admins response keeps ChannelParticipantCreator/Admin classes.
        snap = await self._role_snapshot(guid, fresh=fresh)
        if snap and snap.get("role_scan_ok"):
            is_owner = bool(snap.get("owner_verified") and snap.get("owner_source") == "live_creator" and snap.get("owner") is not None and uid == str(snap.get("owner")))
            is_admin = uid in {str(x) for x in snap.get("admins", [])} or is_owner
            log.info("snapshot role chat=%s user=%s owner=%s admin=%s source=admin-scan", guid, uid, is_owner, is_admin)
            return {"is_owner": is_owner, "is_admin": is_admin, "source": "admin-scan"}

        # FALLBACK: only when the chat-wide role scan itself is unavailable.
        role = await self._participant_role(guid, uid)
        if role is not None:
            log.info("live role chat=%s user=%s source=%s class=%s owner=%s admin=%s", guid, uid, role.get("source"), role.get("participant_class"), role.get("is_owner"), role.get("is_admin"))
            return {"is_owner": bool(role.get("is_owner")), "is_admin": bool(role.get("is_admin")), "source": role.get("source")}

        # Last, stale cache snapshot. This is deliberately only a read fallback
        # and is never written back as a newly verified owner.
        is_owner = bool(snap and snap.get("owner_verified") and snap.get("owner_source") == "live_creator" and snap.get("owner") is not None and uid == str(snap.get("owner")))
        is_admin = uid in {str(x) for x in (snap.get("admins", []) if snap else [])} or is_owner
        log.info("snapshot role chat=%s user=%s owner=%s admin=%s source=cache-fallback", guid, uid, is_owner, is_admin)
        return {"is_owner": is_owner, "is_admin": is_admin, "source": "cache-fallback"}

    async def is_admin(self, guid, user_guid=None, *, fresh=False):
        target = user_guid if user_guid is not None else await self._self_id()
        if target is None:
            return False
        role = await self.user_role(guid, target, fresh=fresh)
        result = bool(role.get("is_admin") or role.get("is_owner"))
        log.info("admin final chat=%s user=%s fresh=%s => %s", guid, target, fresh, result)
        return result

    async def _self_id(self):
        me = await self.call("get_me")
        return guid_of(me)

    async def chat_info_light(self, guid):
        try:
            info = await self.call("get_chat_info", self._chat_arg(guid))
        except Exception:
            return None
        return info or None

    async def admins(self, guid, start_id=None):
        snap = await self._role_snapshot(guid, fresh=True)
        owner = snap.get("owner")
        out = []
        for row in snap.get("members", []):
            rid = str(row.get("id"))
            if rid in {str(x) for x in snap.get("admins", [])}:
                item = dict(row)
                item["id"] = rid
                item["is_admin"] = True
                item["is_owner"] = rid == str(owner)
                out.append(item)
        known = {str(x.get("id")) for x in out}
        for rid in snap.get("admins", []):
            if str(rid) not in known:
                out.append({"id": str(rid), "name": "کاربر", "is_admin": True, "is_owner": str(rid) == str(owner)})
        return {"members": out, "owner_id": owner}

    async def members(self, guid, start_id=None):
        rows = await self._cache_chat_members(guid, force=True)
        return {"members": rows}

    async def mention_list(self, guid, search=None):
        rows = members_of(await self.members(guid))
        if search:
            term = str(search).lower()
            rows = [m for m in rows if term in name_of(m).lower() or term in str(guid_of(m)).lower()]
        return {"members": rows}

    # ---------------- Moderation ----------------
    async def ban(self, guid, member_guid, action="Set"):
        if str(action).lower().startswith("unset"):
            return await self.unban(guid, member_guid)
        result = await self.call("ban_member", self._chat_arg(guid), self._user_arg(member_guid))
        self._set_ban_record(guid, member_guid, True)
        return result

    async def unban(self, guid, member_guid):
        result = await self.call("unban_member", self._chat_arg(guid), self._user_arg(member_guid))
        self._set_ban_record(guid, member_guid, False)
        return result

    @staticmethod
    def _set_ban_record(guid, member_guid, banned):
        """Keep the bot's own record of bans so «لیست بن» has something true to show.

        The Splus API offers no server-side ban list, so only bans made through the bot
        are recorded here; bans made in the Splus app are not.
        """
        g = db.group(str(guid))
        record = g.setdefault("ban_record", {})
        if banned:
            record[str(member_guid)] = time.time()
        else:
            record.pop(str(member_guid), None)
        db.save("groups")

    async def add_member(self, guid, member_guid):
        """Re-add/invite a user after unban using the underlying Splus TL calls.

        SplusLib 3.0.1 does not expose a public add-member helper. Its documented
        escape hatch exposes ``client._client`` for native requests.
        """
        raw = getattr(self.client, "_client", None)
        if raw is None:
            return False
        chat = await self._raw_chat_entity(guid)
        user = await self._raw_user_entity(member_guid)
        if chat is None or user is None:
            return False
        try:
            from spluslib._base.tl import functions, types
            chat_cls = type(chat).__name__.lower()
            if "channel" in chat_cls:
                req_cls = getattr(getattr(functions, "channels", None), "InviteToChannelRequest", None)
                if req_cls is None:
                    return False
                input_channel = await raw.get_input_entity(chat)
                input_user = await raw.get_input_entity(user)
                result = raw(req_cls(channel=input_channel, users=[input_user]))
            else:
                req_cls = getattr(getattr(functions, "messages", None), "AddChatUserRequest", None)
                if req_cls is None:
                    return False
                result = raw(req_cls(chat_id=int(getattr(chat, "id")), user_id=await raw.get_input_entity(user), fwd_limit=0))
            if inspect.isawaitable(result):
                result = await result
            return True
        except Exception as exc:
            log.debug("add member native failed chat=%s user=%s: %r", guid, member_guid, exc)
            return False

    async def banned_members(self, guid, start_id=None):
        # SplusLib exposes banned participants through an active call, but there
        # is no generic group-level public banned-list method in the documented API.
        # FBI's persistent local "بلاک"/"کیل" lists remain available instead.
        g = db.group(str(guid))
        return {"members": [{"id": x} for x in g.get("block", [])]}

    async def set_admin(self, guid, member_guid, access=None):
        """Promote a member with explicit default/full access profiles.

        ``default`` is a normal management profile; ``full`` gives every
        administrator right supported by the installed Splus/TL build.
        Unknown/legacy callers continue to work with the full profile.
        """
        access_name = str(access or "full").lower() if not isinstance(access, dict) else "dict"
        common = {
            "change_info": True,
            "delete_messages": True,
            "ban_users": True,
            "invite_users": True,
            "pin_messages": True,
            "manage_call": True,
            "anonymous": False,
        }
        full_extra = {
            "post_messages": True,
            "edit_messages": True,
            "add_admins": True,
            "manage_topics": True,
            "post_stories": True,
            "edit_stories": True,
            "delete_stories": True,
        }
        if isinstance(access, dict):
            kwargs = {k: v for k, v in dict(access).items() if k != "profile"}
        elif access_name in {"default", "normal", "admin", "دیفالت", "پیشفرض"}:
            kwargs = dict(common)
        else:
            kwargs = dict(common)
            kwargs.update(full_extra)
        kwargs.setdefault("rank", "FBI")

        chat_arg = self._chat_arg(guid)
        user_arg = self._user_arg(member_guid)

        # Public SplusLib path first. For a DEFAULT admin we deliberately use
        # the library's native set_admin(chat,user) call with no custom rights,
        # so the server applies its real default admin profile.
        if access_name in {"default", "normal", "admin", "دیفالت", "پیشفرض"}:
            try:
                result = await self.call("set_admin", chat_arg, user_arg)
                self._role_cache.pop(str(_id(guid)), None)
                self._member_cache.pop(str(_id(guid)), None)
                return result
            except Exception as public_exc:
                log.debug("default set_admin failed chat=%s user=%s: %r", guid, member_guid, public_exc)
        else:
            try:
                result = await self.call("set_admin", chat_arg, user_arg, **kwargs)
                self._role_cache.pop(str(_id(guid)), None)
                self._member_cache.pop(str(_id(guid)), None)
                return result
            except TypeError:
                log.debug("profile set_admin does not accept custom rights chat=%s user=%s", guid, member_guid)
            except Exception as public_exc:
                log.debug("profile set_admin failed chat=%s user=%s access=%s: %r", guid, member_guid, access_name, public_exc)

        # Low-level fallback: dynamically construct the rights object using
        # exactly the fields exposed by the installed SplusLib TL schema.
        try:
            raw = getattr(self.client, "_client", None)
            chat = await self._raw_chat_entity(guid)
            user = await self._raw_user_entity(member_guid)
            from spluslib._base.tl import functions as f, types as t
            edit_cls = getattr(getattr(f, "channels", None), "EditAdminRequest", None)
            rights_cls = getattr(t, "ChatAdminRights", None)
            if raw is None or chat is None or user is None or edit_cls is None or rights_cls is None:
                raise RuntimeError("TL admin API unavailable")

            try:
                rparams = inspect.signature(rights_cls).parameters
            except Exception:
                rparams = {}
            supported = {
                "change_info", "post_messages", "edit_messages", "delete_messages",
                "ban_users", "invite_users", "pin_messages", "add_admins",
                "manage_call", "anonymous", "manage_topics", "post_stories",
                "edit_stories", "delete_stories",
            }
            desired = set(k for k,v in kwargs.items() if k in supported and v is not None)
            rights_kwargs = {}
            for field in supported:
                if not rparams or field in rparams:
                    if field == "anonymous":
                        rights_kwargs[field] = bool(kwargs.get(field, False))
                    else:
                        rights_kwargs[field] = bool(field in desired and kwargs.get(field))
            rights = rights_cls(**rights_kwargs)

            req_kwargs = {"channel": chat, "user_id": user, "admin_rights": rights}
            try:
                eparams = inspect.signature(edit_cls).parameters
                if "rank" in eparams:
                    req_kwargs["rank"] = "FBI"
            except Exception:
                req_kwargs["rank"] = "FBI"
            try:
                req = edit_cls(**req_kwargs)
            except Exception:
                req = edit_cls(channel=chat, user_id=user, admin_rights=rights, rank="FBI")
            result = raw(req)
            if inspect.isawaitable(result):
                result = await result
            self._role_cache.pop(str(_id(guid)), None)
            self._member_cache.pop(str(_id(guid)), None)
            return result
        except Exception as exc:
            raise RuntimeError(f"اعطای دسترسی ادمین در Splus انجام نشد: {exc}") from exc

    async def unset_admin(self, guid, member_guid):
        result = await self.call("remove_admin", self._chat_arg(guid), self._user_arg(member_guid))
        self._role_cache.pop(str(_id(guid)), None)
        self._member_cache.pop(str(_id(guid)), None)
        return result

    # ------------------------------------------------------------------
    # Message-right writes (4.7.31)
    #
    # A member mute or a group close writes EVERY send_*/embed_* right the
    # installed SplusLib build declares: text, photos, videos, files, links,
    # stickers, GIFs, voice, audio, polls, contacts, locations, stories...
    # Names the build does not declare are reported back, never assumed.
    # ------------------------------------------------------------------

    @staticmethod
    def _signature_params(fn):
        """Parameters of ``fn``, or None when its signature cannot be read."""
        try:
            return inspect.signature(fn).parameters
        except (TypeError, ValueError):
            return None

    @classmethod
    def _message_right_names(cls, params=None):
        """Every send_*/embed_* right: the known SplusLib list plus any the build declares."""
        names = {n for n in cls._group_permission_fields() if n.startswith(("send_", "embed_"))}
        if params:
            names.update(str(n) for n in params if str(n).startswith(("send_", "embed_")))
        return tuple(sorted(names))

    async def _write_rights(self, edit_permissions, chat, user, rights, *, until_date=None):
        """Call ``edit_permissions(chat, user, **rights)`` and return the names the build accepted.

        ``rights`` maps a right name to True (allowed) or False (restricted).
        A readable signature drops names the build does not declare before the
        call. An unreadable signature gets the full set; a name the build rejects
        with "unexpected keyword argument" is dropped and the call is retried.
        Any other error is raised.
        """
        params = self._signature_params(edit_permissions)
        accepts_kwargs = bool(params) and any(
            p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values()
        )
        kwargs = dict(rights)
        if params and not accepts_kwargs:
            kwargs = {k: v for k, v in kwargs.items() if k in params}
        if params is None or accepts_kwargs or "until_date" in params:
            kwargs["until_date"] = until_date
        elif until_date is not None:
            log.warning("edit_permissions takes no until_date; a timed restriction is written as indefinite")
        for _ in range(len(kwargs) + 1):
            try:
                result = edit_permissions(chat, user, **kwargs)
                if inspect.isawaitable(result):
                    await result
                break
            except TypeError as exc:
                match = re.search(r"unexpected keyword argument '([^']+)'", str(exc))
                if not match or match.group(1) not in kwargs:
                    raise
                log.debug("edit_permissions build does not accept %s; dropped", match.group(1))
                kwargs.pop(match.group(1))
        else:
            raise TypeError("edit_permissions rejected every right name")
        return {k for k in kwargs if k in rights}

    async def _member_edit_permissions(self, guid, member_guid, *, muted, minutes=None, rights=None):
        """Write every message right for ONE member. The chat defaults are never touched.

        Muting restricts every right. Releasing a mute writes each right back to what
        ordinary members have right now (see ``_group_default_rights``).
        Returns the set of right names the build accepted; empty means nothing was written.
        """
        raw = getattr(self.client, "_client", None)
        if raw is None:
            return set()
        chat = await self._raw_chat_entity(guid)
        user = await self._raw_user_entity(member_guid)
        if chat is None or user is None:
            return set()

        # Hard guards: never write against the chat entity or a non-user entity.
        chat_id = _id(guid)
        user_id = _id(member_guid)
        if chat_id is not None and user_id is not None and str(chat_id) == str(user_id):
            log.error("REFUSED member permission operation with identical chat/user ids: %s", guid)
            return set()
        user_type = type(user).__name__.lower()
        if any(token in user_type for token in ("channel", "chat", "megagroup", "group")):
            log.error("REFUSED member permission operation on non-user entity %s (%s)", member_guid, user_type)
            return set()

        edit_permissions = getattr(raw, "edit_permissions", None)
        if not callable(edit_permissions):
            return set()
        if rights is None:
            names = self._message_right_names(self._signature_params(edit_permissions))
            if muted:
                rights = {name: False for name in names}
            else:
                rights = await self._group_default_rights(guid, names)
        until = None
        if muted and minutes is not None:
            until = datetime.fromtimestamp(time.time() + int(minutes) * 60, tz=timezone.utc)
        try:
            applied = await self._write_rights(edit_permissions, chat, user, rights, until_date=until)
        except Exception as exc:
            log.debug("member edit_permissions failed chat=%s user=%s muted=%s: %r", guid, member_guid, muted, exc)
            return set()
        self._role_cache.pop(str(_id(guid)), None)
        self._member_cache.pop(str(_id(guid)), None)
        missing = sorted(set(rights) - applied)
        if missing:
            log.info("build did not accept member rights %s chat=%s", missing, guid)
        return applied

    async def _verify_group_defaults_unchanged(self, guid, before):
        """Return True only when member moderation did not alter group defaults."""
        if not before or not isinstance(before, dict) or not before.get("fields"):
            # No reliable baseline means we cannot prove a global leak.
            return True
        after = await self.get_group_default_permissions(guid)
        if not after or not after.get("fields"):
            return True
        b = before.get("fields") or {}
        a = after.get("fields") or {}
        common = set(b) & set(a)
        changed = {k: (b[k], a[k]) for k in common if bool(b[k]) != bool(a[k])}
        if not changed:
            return True
        log.error("MEMBER MUTE LEAK DETECTED chat=%s changed default permissions=%s", guid, changed)
        try:
            restored = await self.restore_group_default_permissions(guid, before)
            if not restored:
                log.error("Could not restore group defaults after member mute leak: %s", guid)
        except Exception as exc:
            log.error("Group default rollback failed chat=%s: %r", guid, exc)
        return False

    async def _member_rights_state(self, guid, member_guid):
        """Read ONE member's message rights back from the server as {right: allowed}.

        Empty when the build does not expose that member's permissions.
        """
        raw = getattr(self.client, "_client", None)
        fn = getattr(raw, "get_permissions", None) if raw is not None else None
        if not callable(fn):
            return {}
        chat = await self._raw_chat_entity(guid)
        user = await self._raw_user_entity(member_guid)
        if chat is None or user is None:
            return {}
        try:
            result = fn(chat, user)
            if inspect.isawaitable(result):
                result = await result
        except Exception as exc:
            log.debug("member permission read failed chat=%s user=%s: %r", guid, member_guid, exc)
            return {}
        state = {}
        for name in self._message_right_names():
            banned = self._find_nested(result, (name,))
            if banned is not None:
                state[name] = not bool(banned)
        return state

    async def _tl_user_permissions(self, guid, member_guid, *, muted, minutes=None, default_snapshot=None):
        """Compatibility wrapper around the member writer. True when send_messages was accepted."""
        applied = await self._member_edit_permissions(guid, member_guid, muted=muted, minutes=minutes)
        if "send_messages" not in applied:
            return False
        if default_snapshot is not None:
            return await self._verify_group_defaults_unchanged(guid, default_snapshot)
        return True

    async def _unrestricted_rights(self, guid, member_guid):
        """Rights the server still reports as allowed for a member that was just muted."""
        state = await self._member_rights_state(guid, member_guid)
        return sorted(name for name, allowed in state.items() if allowed)

    async def _group_default_rights(self, guid, names):
        """Per right, what an ordinary member may do in this group right now.

        Read from the live chat defaults. If they cannot be read, a closed group
        keeps every right closed and an open group leaves them open.
        """
        snap = await self.get_group_default_permissions(guid)
        fields = (snap or {}).get("fields") or {}
        closed = bool((db.group(str(guid)) or {}).get("closed"))
        return {name: (bool(fields[name]) if name in fields else not closed) for name in names}

    async def _high_level_text_mute(self, guid, member_guid, seconds):
        """Legacy SplusClient.mute_member. It restricts only what the build implements,
        so it is a last resort and is never reported as a full mute."""
        fn = getattr(self.client, "mute_member", None)
        if not callable(fn):
            return False
        try:
            result = fn(self._chat_arg(guid), self._user_arg(member_guid), seconds=seconds)
            if inspect.isawaitable(result):
                await result
            return True
        except Exception as exc:
            log.debug("legacy text mute failed chat=%s user=%s: %r", guid, member_guid, exc)
            return False

    async def ensure_mute_permission(self, guid, member_guid, seconds=None, minutes=None):
        """Re-apply a one-member mute. True only when ``mute_member`` fully succeeded."""
        try:
            await self.mute_member(guid, member_guid, seconds=seconds, minutes=minutes)
        except Exception as exc:
            log.debug("ensure mute permission failed chat=%s user=%s: %r", guid, member_guid, exc)
            return False
        return True

    async def mute_member(self, guid, member_guid, seconds=None, minutes=None):
        """Mute ONE member across every message right.

        Raises unless the write was accepted and ``send_messages`` is restricted.
        Rights the build does not declare come back in ``skipped``; the bot still
        blocks them locally. Rights the read-back still shows open come back in
        ``open`` (logged; see the comment in the body).
        """
        if minutes is None and seconds is not None:
            minutes = max(1, int((int(seconds) + 59) // 60))
        if minutes is not None:
            minutes = max(1, int(minutes))
        if str(guid) == str(member_guid):
            raise RuntimeError("سکوت کاربر نامعتبر است؛ شناسه گروه و کاربر یکسان است.")

        default_snapshot = None
        try:
            default_snapshot = await self.get_group_default_permissions(guid)
        except Exception:
            default_snapshot = None

        applied = await self._member_edit_permissions(guid, member_guid, muted=True, minutes=minutes)
        if default_snapshot is not None and not await self._verify_group_defaults_unchanged(guid, default_snapshot):
            raise RuntimeError("سکوت روی تنظیمات پیش‌فرض گروه اثر گذاشت؛ تغییر برگردانده شد.")
        if "send_messages" not in applied:
            legacy_seconds = None if minutes is None else minutes * 60
            if await self._high_level_text_mute(guid, member_guid, legacy_seconds):
                raise RuntimeError("فقط ارسال متن بسته شد؛ رسانه و لینک روی سرور بسته نشد.")
            raise RuntimeError("Permission کاربر روی سرور تغییر نکرد.")

        # Read-back is advisory. Builds report a permission flag either as "banned" or
        # as "allowed", and only the accepted write is certain, so a read-back that
        # still looks open after one retry is logged and returned, never raised.
        open_rights = [n for n in await self._unrestricted_rights(guid, member_guid) if n in applied]
        if open_rights:
            await self._member_edit_permissions(guid, member_guid, muted=True, minutes=minutes)
            open_rights = [n for n in await self._unrestricted_rights(guid, member_guid) if n in applied]
        if open_rights:
            log.warning("mute read-back still shows open rights chat=%s user=%s: %s", guid, member_guid, open_rights)
        return {
            "ok": True,
            "server": "member_permissions",
            "minutes": minutes,
            "rights": len(applied),
            "skipped": sorted(set(self._message_right_names()) - applied),
            "open": open_rights,
        }

    async def unmute_member(self, guid, member_guid):
        """Release ONE member: each right goes back to the live group default.

        A release in a closed group therefore keeps media closed for that member too.
        """
        if str(guid) == str(member_guid):
            raise RuntimeError("رفع سکوت نامعتبر است؛ شناسه گروه و کاربر یکسان است.")
        applied = await self._member_edit_permissions(guid, member_guid, muted=False)
        if "send_messages" in applied:
            return {"ok": True, "server": "member_permissions"}
        try:
            fn = getattr(self.client, "unmute_member", None)
            if callable(fn):
                result = fn(self._chat_arg(guid), self._user_arg(member_guid))
                if inspect.isawaitable(result):
                    await result
                return {"ok": True, "server": "spluslib_legacy"}
        except Exception as exc:
            log.debug("legacy unmute failed chat=%s user=%s: %r", guid, member_guid, exc)
        raise RuntimeError("رفع سکوت سروری اعمال نشد؛ Permission کاربر تغییر نکرد.")

    async def default_access(self, guid, access_list):
        """Persist the access list only. The ``ارسال پیام`` switch is applied by set_group_closed.

        Before 4.7.32 every toggle called set_group_closed(not "SendMessages" in list), so
        switching any other access item on a group whose list did not yet contain
        SendMessages closed the whole group.
        """
        access = list(access_list or [])
        db.group(str(guid))["default_access"] = access
        db.save("groups")
        return True

    async def get_default_access(self, guid):
        return {"access_list": list(db.group(str(guid)).get("default_access", []))}

    async def group_link(self, guid):
        info = await self.group_info(guid)
        link = str((info or {}).get("invite_link") or "")
        if not link:
            # Group info does not always carry the invite link; ask for it directly.
            try:
                fresh = await self.new_link(guid)
                link = str((fresh or {}).get("link") or "")
            except Exception:
                link = ""
        return {"link": link}

    async def new_link(self, guid):
        res = await self.call("get_chat_invite_link", self._chat_arg(guid))
        link = _val(res, "link", "join_link", "invite_link", default=None)
        if link is None and isinstance(res, str):
            link = res
        return {"link": link or ""}

    async def set_timer(self, guid, seconds):
        db.group(str(guid))["server_timer"] = int(seconds or 0)
        db.save("groups")
        return True

    async def edit_info(self, guid, **kwargs):
        results = []
        if "title" in kwargs:
            results.append(await self.call("set_chat_title", self._chat_arg(guid), kwargs["title"]))
        if "description" in kwargs or "about" in kwargs:
            results.append(await self.call("set_chat_description", self._chat_arg(guid), kwargs.get("description", kwargs.get("about", ""))))
        return all(r is not None for r in results) if results else True

    async def join_group(self, link):
        return await self.call("join_group_by_invite", str(link).strip())

    async def leave_group(self, guid):
        return await self.call("leave_chat", self._chat_arg(guid))

    async def set_profile(self, **kwargs):
        return await self.call("update_profile", **kwargs)

    async def privacy(self, **kwargs):
        db.data["users"].setdefault("_privacy", {}).update(kwargs)
        db.save("users")
        return True

    # ---------------- Join requests ----------------
    async def join_requests(self, guid, start_id=None):
        return {"requests": await self.call("get_join_requests", self._chat_arg(guid), limit=100) or []}

    async def answer_join(self, guid, user_guid, action="Accept"):
        if str(action).lower().startswith(("accept", "approve")):
            return await self.call("approve_join_request", self._chat_arg(guid), self._user_arg(user_guid))
        return await self.call("decline_join_request", self._chat_arg(guid), self._user_arg(user_guid))

    # ---------------- Media / profile ----------------
    async def _full_user_profile(self, guid, live=True):
        """Return a user profile with the real ``about``/bio field.

        SplusLib's own ``get_me()`` implementation documents that bio requires
        a ``users.GetFullUserRequest`` call; the basic user object alone does
        not contain it. We therefore use that exact request for moderation.
        The current message's ``get_sender()`` entity is preferred because it
        is already resolved and avoids the ``GetUsersRequest NOT_FOUND`` path
        seen with some numeric Splus IDs.
        """
        uid = str(guid)
        ref = self._user_refs.get(self._user_key(uid))

        # On a forced/live check, never trust a stale DB/cache bio first.
        # This is what makes a newly-added URL in the bio detectable on the
        # user's next message.
        if not live:
            for source in (ref, self._user_cache.get(uid), db.data.get("users", {}).get(uid)):
                if source is None:
                    continue
                bio = _val(source, "bio", "about", default="")
                if bio:
                    return {
                        "id": uid,
                        "first_name": _val(source, "first_name", "first", "name", default=""),
                        "last_name": _val(source, "last_name", "last", default=""),
                        "username": _val(source, "username", "user_name", default=""),
                        "bio": str(bio),
                    }

        raw = getattr(self.client, "_client", None)
        if raw is not None:
            try:
                from spluslib._base.tl import functions
                req_cls = getattr(getattr(functions, "users", None), "GetFullUserRequest", None)
                if req_cls is not None:
                    target = ref
                    if target is None:
                        # Only resolve numerically when there was no entity from
                        # the incoming Message. This is the last-resort path.
                        target = await self._raw_user_entity(uid)
                    if target is None:
                        target = _id(uid)

                    result = raw(req_cls(id=target))
                    if inspect.isawaitable(result):
                        result = await result

                    full_user = _val(result, "full_user", default=None)
                    bio = str(_val(full_user, "about", "bio", default="") or "")
                    users = _val(result, "users", default=None)
                    row = None
                    if isinstance(users, (list, tuple)) and users:
                        row = users[0]
                    elif users:
                        row = users

                    data = {
                        "id": uid,
                        "first_name": _val(row, "first_name", "first", "name", default=""),
                        "last_name": _val(row, "last_name", "last", default=""),
                        "username": _val(row, "username", "user_name", default=""),
                        "bio": bio,
                    }
                    if not data["first_name"] and ref is not None:
                        data["first_name"] = _val(ref, "first_name", "first", "name", default="")
                    if not data["last_name"] and ref is not None:
                        data["last_name"] = _val(ref, "last_name", "last", default="")
                    if not data["username"] and ref is not None:
                        data["username"] = _val(ref, "username", "user_name", default="")
                    self._remember(data)
                    return data
            except Exception as exc:
                log.debug("full user profile lookup failed user=%s: %r", uid, exc)

        # Network/profile lookup failed: use the freshest locally-known value
        # rather than claiming we inspected a bio we could not fetch.
        for source in (ref, self._user_cache.get(uid), db.data.get("users", {}).get(uid)):
            if source is None:
                continue
            bio = _val(source, "bio", "about", default="")
            if bio:
                return {
                    "id": uid,
                    "first_name": _val(source, "first_name", "first", "name", default=""),
                    "last_name": _val(source, "last_name", "last", default=""),
                    "username": _val(source, "username", "user_name", default=""),
                    "bio": str(bio),
                }
        return None

    async def user_bio(self, guid, force=False, chat_hint=None):
        key = str(guid)
        now = asyncio.get_running_loop().time()
        cached = self._bio_cache.get(key)
        if cached and not force and now - cached[0] < config.BIO_CHECK_CACHE_SECONDS:
            return cached[1]
        lock = self._bio_locks.setdefault(key, asyncio.Lock())
        async with lock:
            cached = self._bio_cache.get(key)
            if cached and not force and now - cached[0] < config.BIO_CHECK_CACHE_SECONDS:
                return cached[1]

            # Full-user is the authoritative path for bio/about. With force=True
            # (used by message moderation) always attempt the live lookup first.
            info = await self._full_user_profile(guid, live=force)
            if not info:
                info = await self.user_info(guid, chat_hint=chat_hint)

            bio = str(_val(info, "bio", "about", default="") or "") if info else ""
            user = db.user(key)
            user["bio"] = bio
            user["username"] = str(_val(info, "username", "user_name", default="") or user.get("username", "")) if info else user.get("username", "")
            user["name"] = name_of(info) if info else user.get("name", "")
            user["bio_checked_at"] = time.time()
            db.save("users")
            self._bio_cache[key] = (now, bio)
            return bio

    async def download_avatar(self, guid, path, chat_hint=None):
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        # Prefer any current SplusLib public profile-photo helper.
        for getter_name in ("get_profile_photos", "get_user_profile_photos"):
            fn = getattr(self.client, getter_name, None)
            if not callable(fn):
                continue
            try:
                photos = fn(self._user_arg(guid), limit=1)
                if inspect.isawaitable(photos):
                    photos = await photos
                photos = members_of(photos)
                photo = photos[0] if photos else None
                if photo:
                    result = await self.call("download_media", photo, file_path=path)
                    if os.path.isfile(path) and os.path.getsize(path) > 100:
                        return result or path
            except Exception:
                continue
        return None

    async def download_chat_avatar(self, guid, path):
        return await self.download_avatar(guid, path, chat_hint=guid)

    async def download_message_file(self, guid, message_id, dest_dir, default_ext="ogg"):
        os.makedirs(dest_dir, exist_ok=True)
        msg = await self.call("get_message_by_id", self._chat_arg(guid), _as_int(message_id, message_id))
        if not msg:
            return None
        filename = _val(msg, "file_name", default=None) or _val(_val(msg, "file"), "name", "file_name", default=None)
        if filename and "." in str(filename):
            safe = re.sub(r"[^\w.\-]+", "_", str(filename))
            path = os.path.join(dest_dir, safe)
        else:
            path = os.path.join(dest_dir, f"{message_id}.{default_ext}")
        try:
            result = await self.call("download_media", msg, file_path=path)
            if result and os.path.exists(result):
                return result
            if os.path.exists(path):
                return path
        except Exception:
            pass
        return None

    async def me(self):
        return await self.call("get_me")

    # ---------------- Calls ----------------
    def _active_call_rows(self, result):
        if isinstance(result, list):
            return result
        rows = _val(result, "calls", "results", "data", "active_calls", default=[])
        return rows if isinstance(rows, list) else []

    def _call_slug(self, row):
        return _val(row, "slug", "call_slug", "id", "call_id", default=None)

    def _call_link(self, row):
        for key in ("link", "join_link", "invite_link", "web_link", "url"):
            value = _val(row, key, default=None)
            if isinstance(value, str) and value.startswith(("https://", "http://")):
                return value
        return ""

    async def active_voice_id(self, guid):
        key = str(guid)
        if self._voice_ids.get(key):
            return self._voice_ids[key]
        result = await self.call("get_active_group_calls")
        for row in self._active_call_rows(result):
            cid = _val(row, "chat_id", "peer_id", "group_id", default=None)
            if cid is not None and str(cid) == str(_id(guid)):
                slug = self._call_slug(row)
                if slug:
                    self._voice_ids[key] = str(slug)
                    link = self._call_link(row)
                    if link:
                        self._voice_links[key] = link
                    return str(slug)
        return None

    async def voice_create(self, guid):
        key = str(guid)
        existing = await self.active_voice_id(guid)
        if existing:
            return True, {"slug": existing, "link": self._voice_links.get(key, ""), "already_active": True}
        result = await self.call("create_group_call", title="FBI", chat_id=self._chat_arg(guid))
        slug = self._call_slug(result)
        if not slug:
            # Some builds accept title only.
            result = await self.call("create_group_call", title="FBI")
            slug = self._call_slug(result)
        if not slug:
            return False, RuntimeError("SplusLib کال را ساخت اما slug کال برنگرداند.")
        self._voice_ids[key] = str(slug)
        link = self._call_link(result)
        if not link:
            try:
                info = await self.call("get_group_call_info", slug=str(slug))
                link = self._call_link(info)
                # Some responses return the slug nested inside the call object.
                nested_slug = self._call_slug(info)
                if nested_slug and not self._voice_ids.get(key):
                    self._voice_ids[key] = str(nested_slug)
            except Exception:
                pass
        self._voice_links[key] = link
        return True, {"raw": result, "slug": str(slug), "link": link}

    async def voice_join(self, guid):
        """واقعاً وارد LiveKit تماس شو، نه اینکه فقط تماس را بسازیم."""
        key = str(guid)
        slug = await self.active_voice_id(guid)
        created = None
        if not slug:
            ok, created = await self.voice_create(guid)
            if not ok:
                return False, created
            slug = created.get("slug") if isinstance(created, dict) else None
        if not slug:
            return False, RuntimeError("شناسه تماس پیدا نشد.")

        old = self._voice_sessions.get(key)
        if old is not None and getattr(old, "is_connected", False):
            return True, {
                "slug": str(slug),
                "link": self._voice_links.get(key, ""),
                "session": old,
                "already_joined": True,
            }

        try:
            from spluslib.call_audio import CallAudioSession
            call = await self.call(
                "join_group_call",
                slug=str(slug),
                muted=True,
                video_stopped=True,
            )
            if not call:
                return False, RuntimeError("join_group_call پاسخ معتبری نداد.")
            url = _val(call, "url", "livekit_url", default=None)
            token = _val(call, "token", "livekit_token", default=None)
            if not url or not token:
                return False, RuntimeError("اطلاعات LiveKit (url/token) از join_group_call دریافت نشد.")

            if old:
                try:
                    await old.stop()
                except Exception:
                    pass
                try:
                    await old.disconnect()
                except Exception:
                    pass

            session = CallAudioSession()
            await session.connect(url, token)
            self._voice_sessions[key] = session
            log.info("[VoiceChat] joined LiveKit chat=%s slug=%s connected=%s", guid, slug, getattr(session, "is_connected", False))
            return True, {
                "slug": str(slug),
                "link": self._voice_links.get(key, ""),
                "session": session,
                "call": call,
                "already_joined": False,
            }
        except Exception as exc:
            log.exception("[VoiceChat] join failed chat=%s slug=%s", guid, slug)
            return False, exc

    async def voice_discard(self, guid, voice_chat_id=None):
        key = str(guid)
        session = self._voice_sessions.pop(key, None)
        if session:
            try:
                await session.stop()
            except Exception:
                pass
            try:
                await session.disconnect()
            except Exception:
                pass
        slug = voice_chat_id or self._voice_ids.get(key)
        if not slug:
            return False, RuntimeError("کال فعالی پیدا نشد.")
        result = await self.call("end_group_call", slug=slug)
        self._voice_ids.pop(key, None)
        self._voice_links.pop(key, None)
        task = self._voice_tasks.pop(key, None)
        if task and not task.done():
            task.cancel()
        return True, result

    async def voice_info(self, guid):
        slug = await self.active_voice_id(guid)
        if not slug:
            return None
        return await self.call("get_group_call_info", slug=slug)

    async def voice_mute(self, guid, voice_chat_id, participant_guid, muted=True):
        # Current SplusLib uses the same mute_participant method for both states.
        # Calling a non-existent unmute_participant would silently report success.
        return await self.call(
            "mute_participant",
            str(voice_chat_id),
            _id(participant_guid),
            muted=bool(muted),
        )

    async def voice_participants(self, guid, voice_chat_id):
        return await self.call("get_group_call_info", slug=voice_chat_id)

    async def voice_play(self, guid, path, keep_alive=None):
        if not path or not os.path.isfile(path):
            return False, FileNotFoundError(path or "مسیر فایل مشخص نیست")
        key = str(guid)
        try:
            from spluslib.call_audio import CallAudioSession
        except Exception as exc:
            return False, exc
        slug = await self.active_voice_id(guid)
        if not slug:
            ok, created = await self.voice_create(guid)
            if not ok:
                return False, created
            slug = created.get("slug")
        try:
            session = self._voice_sessions.get(key)
            if session is None or not getattr(session, "is_connected", False):
                ok, joined = await self.voice_join(guid)
                if not ok:
                    return False, joined
                session = joined.get("session")
            if session is None:
                return False, RuntimeError("جلسهٔ صوتی ساخته نشد.")
            await session.play(path)
            if keep_alive:
                task = self._voice_tasks.get(key)
                if task and not task.done():
                    task.cancel()
            return True, session
        except Exception as exc:
            return False, exc

    async def _voice_keep_alive(self, key, seconds):
        try:
            await asyncio.sleep(max(seconds, 0.1))
        except asyncio.CancelledError:
            return

    def find_methods(self, keyword):
        word = str(keyword or "").lower()
        return sorted([name for name in dir(self.client) if word in name.lower()])
