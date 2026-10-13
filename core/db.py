# -*- coding: utf-8 -*-
"""لایه‌ی دیتابیس FBI SPLUS: JSON یا SQLite، با API مشترک."""
from __future__ import annotations

import copy
import json
import os
import random
import sqlite3
import string
import threading
import time

import config

FILES = ("groups", "users", "logs", "learn", "backups", "history", "stats", "ai_memory")


class DB:
    def __init__(self, path=None):
        config.ensure_dirs()
        self.path = path or config.JSON_DB_DIR
        self.backend = config.DB_BACKEND if config.DB_BACKEND in {"json", "sqlite"} else "json"
        # When True (set by the bot at startup), save() only marks files dirty and
        # flush() writes them, so a burst of messages costs one write per interval.
        self.defer_writes = False
        self._dirty = set()
        self.sqlite_path = config.DB_SQLITE_PATH
        self._lock = threading.RLock()
        self.data: dict[str, dict] = {}
        if self.backend == "sqlite":
            self._init_sqlite()
        for name in FILES:
            self.data[name] = self._load(name)
        self.data.setdefault("meta", {})
        self.data["meta"]["schema_version"] = getattr(config, "DB_SCHEMA_VERSION", 1)
        self.data["meta"]["backend"] = self.backend

    # ------------------------- IO -------------------------
    def _file(self, name):
        return os.path.join(self.path, f"{name}.json")

    def _init_sqlite(self):
        os.makedirs(os.path.dirname(self.sqlite_path), exist_ok=True)
        with sqlite3.connect(self.sqlite_path) as conn:
            conn.execute("CREATE TABLE IF NOT EXISTS documents (name TEXT PRIMARY KEY, payload TEXT NOT NULL, updated_at REAL NOT NULL)")
            conn.commit()

    def _load(self, name):
        if self.backend == "sqlite":
            try:
                with sqlite3.connect(self.sqlite_path) as conn:
                    row = conn.execute("SELECT payload FROM documents WHERE name=?", (name,)).fetchone()
                if row:
                    value = json.loads(row[0])
                    if isinstance(value, dict):
                        return value
            except Exception:
                pass
            # مهاجرت خودکار JSON قدیمی -> SQLite
            old = self._load_json(name)
            if old:
                try:
                    self._save_sqlite(name, old)
                except Exception:
                    pass
            return old
        return self._load_json(name)

    def _load_json(self, name):
        path = self._file(name)
        tmp = path + ".tmp"
        for candidate in (path, tmp):
            try:
                if not os.path.exists(candidate):
                    continue
                with open(candidate, encoding="utf-8") as fp:
                    value = json.load(fp)
                if isinstance(value, dict):
                    return value
            except Exception:
                continue
        return {}

    def _save_sqlite(self, name, payload):
        with sqlite3.connect(self.sqlite_path) as conn:
            conn.execute(
                "INSERT INTO documents(name,payload,updated_at) VALUES(?,?,?) "
                "ON CONFLICT(name) DO UPDATE SET payload=excluded.payload, updated_at=excluded.updated_at",
                (name, json.dumps(payload, ensure_ascii=False), time.time()),
            )
            conn.commit()

    def _resolve_names(self, name):
        if name is None:
            names = list(FILES)
        elif isinstance(name, (list, tuple, set)):
            names = list(name)
        else:
            names = [name]
        return [n for n in names if n in self.data]

    def save(self, name=None):
        names = self._resolve_names(name)
        if self.defer_writes:
            with self._lock:
                self._dirty.update(names)
            return
        with self._lock:
            self._write_locked(names)

    def flush(self):
        """Write every file changed since the last flush (no-op when nothing changed)."""
        with self._lock:
            names = sorted(self._dirty)
            self._dirty.clear()
            if names:
                self._write_locked(names)

    def _write_locked(self, names):
        for n in names:
            try:
                if self.backend == "sqlite":
                    self._save_sqlite(n, self.data[n])
                else:
                    os.makedirs(self.path, exist_ok=True)
                    tmp = self._file(n) + ".tmp"
                    with open(tmp, "w", encoding="utf-8") as fp:
                        # No indent: the indented encoder is pure Python and much slower.
                        json.dump(self.data[n], fp, ensure_ascii=False, separators=(",", ":"))
                        fp.flush()
                        os.fsync(fp.fileno())
                    os.replace(tmp, self._file(n))
            except Exception:
                continue

    # ------------------------- گروه -------------------------
    def group(self, guid):
        if not guid:
            return copy.deepcopy(config.DEFAULT_SETTINGS)
        groups = self.data["groups"]
        if guid not in groups:
            groups[guid] = copy.deepcopy(config.DEFAULT_SETTINGS)
            self.save("groups")
        else:
            changed = False
            for key, value in config.DEFAULT_SETTINGS.items():
                if key not in groups[guid]:
                    groups[guid][key] = copy.deepcopy(value)
                    changed = True
            if "owner_verified" not in groups[guid]:
                groups[guid]["owner_verified"] = False
                changed = True
            if "owner_source" not in groups[guid]:
                groups[guid]["owner_source"] = ""
                changed = True
            if "live_owner" not in groups[guid]:
                groups[guid]["live_owner"] = None
                changed = True
            if "live_owner_verified" not in groups[guid]:
                groups[guid]["live_owner_verified"] = False
                changed = True
            if "activated_by" not in groups[guid]:
                groups[guid]["activated_by"] = ""
                changed = True
            # Only records explicitly written by the new live creator scanner
            # are authoritative. This deliberately invalidates owner records
            # produced by older FBI versions, even when they incorrectly marked
            # owner_verified=True.
            if groups[guid].get("owner") is not None and groups[guid].get("owner_source") not in {"live_creator", "manual_owner"}:
                groups[guid]["owner"] = None
                groups[guid]["owner_verified"] = False
                groups[guid]["owner_source"] = ""
                changed = True
            elif not groups[guid].get("owner_verified") and groups[guid].get("owner") is not None:
                groups[guid]["owner"] = None
                groups[guid]["owner_source"] = ""
                changed = True
            for key, value in (("status_messages", {}), ("guide_message_ids", {}), ("closed_until", 0),
                               ("wave_lock_notified_at", 0), ("scanned_message_ids", []), ("scan_all_last", 0), ("guide_message_meta", {}), ("mute_restore_last", 0), ("mute_server_applied", {}), ("mute_permission_last", {}), ("closed_permissions_snapshot", {}), ("group_permission_restore_last", 0), ("loyalty_surveys", {}), ("metadata_lock", False), ("metadata_lock_snapshot", {}), ("metadata_last_check", 0), ("metadata_last_restore", 0), ("advanced_replies", []), ("reply_rules", []), ("quick_replies", {})):
                if key not in groups[guid]:
                    groups[guid][key] = copy.deepcopy(value)
                    changed = True
            # nested voice/music state migration
            for key in ("locks", "music_state", "voice_state"):
                base = config.DEFAULT_SETTINGS.get(key, {})
                cur = groups[guid].setdefault(key, {})
                if isinstance(base, dict) and isinstance(cur, dict):
                    for k, v in base.items():
                        if k not in cur:
                            cur[k] = copy.deepcopy(v); changed = True
            if changed:
                self.save("groups")
        return groups[guid]

    def groups(self):
        return self.data["groups"]

    def is_active(self, guid):
        return bool(self.data["groups"].get(guid, {}).get("active"))

    def reset_group(self, guid, section="all"):
        g = self.group(guid)
        base = config.DEFAULT_SETTINGS
        if section in ("all", "locks"):
            g["locks"] = copy.deepcopy(base["locks"])
            g["lock_warn"], g["lock_ban"], g["lock_max_warn"] = {}, {}, {}
        if section in ("all", "cmds"):
            g["disabled_cmds"] = []
        if section in ("all", "dashboard"):
            for key in ("sleep", "fun", "manage_mode", "max_warn", "auto_delete",
                        "auto_delete_time", "speaker", "talkative", "learning",
                        "bio_link_mute"):
                g[key] = copy.deepcopy(base[key])
        if section in ("all", "stats"):
            self.data["stats"].pop(guid, None)
        self.save()

    # ------------------------- کاربر -------------------------
    def user(self, guid):
        users = self.data["users"]
        if guid not in users:
            users[guid] = {
                "nick": "", "title": "", "name": "", "bio": "", "username": "",
                "privacy_nick": "all", "privacy_title": "all", "started": False,
                "owner_verified": False,
                "bio_checked_at": 0,
            }
            self.save("users")
        else:
            # Group migration, including legacy owner cleanup, is performed in
            # group(); this branch only maintains user defaults.
            defaults = {
                "bio": "", "username": "", "bio_checked_at": 0,
                "privacy_nick": "all", "privacy_title": "all", "started": False,
            }
            changed = False
            for k, v in defaults.items():
                if k not in users[guid]: users[guid][k] = copy.deepcopy(v); changed = True
            if changed: self.save("users")
        return users[guid]

    # ------------------------- آمار -------------------------
    def stat(self, guid, user_guid=None):
        s = self.data["stats"].setdefault(guid, {})
        if user_guid is None:
            return s
        return s.setdefault(user_guid, {
            "total": 0, "today": 0, "day": self._today(), "week": {},
            "deleted": 0, "edited": 0, "media": 0, "text": 0,
            "joined": 0, "left": 0, "warn": 0, "last": 0,
            "clean_today": 0, "clean_day": self._today(),
            "loyalty_level": 0, "loyalty_granted_at": 0,
            "loyalty_last_prompt_day": "", "loyalty_reason": "",
            "loyalty_exempt": False,
        })

    @staticmethod
    def _today():
        return time.strftime("%Y-%m-%d")

    def learn(self, guid):
        """Learned answers of one group: {normalized word: [answers]} (the ``learn`` file)."""
        return self.data.setdefault("learn", {}).setdefault(str(guid), {})

    def top_users(self, guid, limit=10):
        """Most active members of a group by total messages: [(user_guid, total)]."""
        rows = []
        for uid, st in (self.data.get("stats", {}).get(str(guid)) or {}).items():
            if isinstance(st, dict) and int(st.get("total") or 0) > 0:
                rows.append((str(uid), int(st.get("total") or 0)))
        rows.sort(key=lambda item: item[1], reverse=True)
        return rows[:max(1, int(limit))]

    def bump(self, guid, user_guid, field="total", value=1):
        st = self.stat(guid, user_guid)
        today = self._today()
        if st.get("day") != today:
            st["day"] = today
            st["today"] = 0
            st["clean_today"] = 0
            st["clean_day"] = today
            st["week"] = {k: v for k, v in (st.get("week") or {}).items()
                          if k >= time.strftime("%Y-%m-%d", time.localtime(time.time() - 6 * 86400))}
        value = int(value or 0)
        if field == "total":
            st["total"] = int(st.get("total", 0)) + value
            st["today"] = int(st.get("today", 0)) + value
            week = st.setdefault("week", {})
            week[today] = int(week.get(today, 0)) + value
        else:
            st[field] = int(st.get(field, 0)) + value
        st["last"] = time.time()

    # ------------------------- تاریخچه/لاگ -------------------------
    def history_add(self, guid, item):
        self.data["history"].setdefault(guid, []).append(item)
        self.data["history"][guid] = self.data["history"][guid][-5000:]

    def log(self, kind, message):
        self.data["logs"].setdefault(kind, []).append({"t": time.time(), "x": str(message)[:3000]})
        self.data["logs"][kind] = self.data["logs"][kind][-2000:]

    # ------------------------- بکاپ -------------------------
    def backup_make(self, guid):
        code = "FBI-" + "".join(random.choice(string.ascii_uppercase + string.digits) for _ in range(8))
        self.data["backups"][code] = {"guid": guid, "settings": copy.deepcopy(self.group(guid)), "created": time.time()}
        self.save("backups")
        return code

    def backup_restore(self, guid, code):
        item = self.data["backups"].get(code)
        if not item: return False
        settings = copy.deepcopy(item.get("settings") or {})
        if not isinstance(settings, dict): return False
        # Group ownership is live server state, not backup configuration.
        settings["owner"] = None
        settings["owner_verified"] = False
        settings["owner_source"] = ""
        settings["live_owner"] = None
        settings["live_owner_verified"] = False
        self.data["groups"][guid] = settings
        self.group(guid)
        self.save("groups")
        return True


db = DB()
