# -*- coding: utf-8 -*-
"""پیکربندی FBI SPLUS — نسخه‌ی SplusLib."""
from __future__ import annotations

import os
import shutil
from pathlib import Path

BASE_DIR = os.path.dirname(os.path.abspath(__file__))


def _load_local_env(path: str) -> None:
    """Load simple KEY=VALUE pairs from a project-local .env file.

    This avoids requiring python-dotenv and, importantly, makes the bundled
    .env.example workflow actually usable when the user creates .env.
    Existing process environment variables always win.
    """
    env_path = Path(path)
    if not env_path.is_file():
        return
    try:
        for raw in env_path.read_text(encoding="utf-8-sig").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip()
            if not key or key in os.environ:
                continue
            if len(value) >= 2 and value[0] == value[-1] and value[0] in ("\"", "'"):
                value = value[1:-1]
            os.environ[key] = value
    except Exception:
        pass


_load_local_env(os.path.join(BASE_DIR, ".env"))

# ========================= Soroush Plus / SplusLib =========================
# می‌توانید شماره را همین‌جا قرار دهید؛ در صورت خالی بودن، هنگام اجرا پرسیده می‌شود.
# نمونه: PHONE_NUMBER = "+989123456789"
try:
    from local_settings import PHONE_NUMBER as _LOCAL_PHONE  # private file; not in the shared package
except ImportError:
    _LOCAL_PHONE = ""
PHONE_NUMBER = os.getenv("FBI_PHONE", "").strip() or str(_LOCAL_PHONE or "").strip()
# نام سشن بدون پسوند؛ SplusLib محل فایل سشن را خودش مدیریت می‌کند.
SESSION_NAME = os.getenv(
    "FBI_SESSION",
    os.path.join(BASE_DIR, "sessions", "fbi_splus"),
)
DEVICE_MODEL = os.getenv("FBI_DEVICE_MODEL", "FBI SPlus Manager")
PASSWORD = os.getenv("FBI_2FA_PASSWORD", "")
PROXY = None  # در صورت نیاز، در لایه‌ی SplusLib/MTProto تنظیم کنید.

SPLUSLIB_MIN_VERSION = "4.7.13-REPLY-FINAL-FIX"

# شناسه حساب خود FBI بعد از ورود؛ خودکار در boot تنظیم می‌شود.
# اگر متغیر محیطی خالی باشد، شناسه‌ی پیش‌فرض «سودو بات» استفاده می‌شود.
BOT_GUID = os.getenv("FBI_BOT_GUID", "").strip() or "62989530"

# ========================= برند =========================
BOT_NAME = "FBI SPLUS"
MAKER = "رادوین"
VERSION = "4.7.46-GLOBAL"

# شناسه‌های سازنده را با ID عددی حساب Splus خودتان پر کنید. (فقط یک سازنده: 56597421)
# شناسه‌های قدیمی Rubika (مثل u...) در این نسخه معتبر نیستند.
# اگر متغیر محیطی خالی باشد، دو شناسه‌ی سازنده‌ی پیش‌فرض استفاده می‌شوند؛
# این حساب‌ها در همه‌ی گروه‌ها هم‌تراز سازنده و بالاتر از مالک داخلی عمل می‌کنند.
_env_sudos = [x.strip() for x in os.getenv("FBI_SUDOS", "").split(",") if x.strip()]
SUDOS = _env_sudos or ["56597421"]
CREATOR_IDS = tuple(dict.fromkeys(str(x) for x in SUDOS))

CHANNEL_ID = ""
# نام کاربری سازنده فقط در صورت وجود واقعی در Splus قرار دهید؛ از ساخت URL جعلی با ID جلوگیری می‌شود.
MAKER_USERNAME = os.getenv("FBI_MAKER_USERNAME", "").strip().lstrip("@").strip()

# ========================= مسیرها =========================
DATA_DIR = os.path.join(BASE_DIR, "data")
DB_DIR = os.path.join(DATA_DIR, "db")
JSON_DB_DIR = os.path.join(DATA_DIR, "json")
DB_SQLITE_PATH = os.path.join(DATA_DIR, "fbi.sqlite3")
BACKUP_DIR = os.path.join(DATA_DIR, "backups")
SESSION_DIR = os.path.dirname(os.path.abspath(SESSION_NAME))
TEMP_DIR = os.path.join(BASE_DIR, "temp")
MUSIC_DIR = os.path.join(BASE_DIR, "music")
DOWNLOAD_DIR = os.path.join(BASE_DIR, "downloads")
FFMPEG_DIR = os.path.join(BASE_DIR, "ffmpeg")
FFMPEG_BIN_DIR = os.path.join(FFMPEG_DIR, "bin")

# backend = json برای سازگاری با پروژه‌ی قبلی؛ sqlite برای دیتابیس تک‌فایل.
DB_BACKEND = os.getenv("FBI_DB_BACKEND", "json").strip().lower() or "json"
DB_SCHEMA_VERSION = 18

# ========================= FFmpeg =========================
# اول این متغیر را می‌خواند، بعد مسیرهای داخل پروژه و در آخر PATH سیستم.
FFMPEG_PATH = os.getenv("FBI_FFMPEG", "").strip()
FFMPEG_CANDIDATES = [
    FFMPEG_PATH,
    os.path.join(FFMPEG_BIN_DIR, "ffmpeg.exe"),
    os.path.join(FFMPEG_BIN_DIR, "ffmpeg"),
    os.path.join(BASE_DIR, "tools", "ffmpeg", "bin", "ffmpeg.exe"),
    os.path.join(BASE_DIR, "tools", "ffmpeg", "bin", "ffmpeg"),
    os.path.join(BASE_DIR, "bin", "ffmpeg.exe"),
    os.path.join(BASE_DIR, "bin", "ffmpeg"),
]

# ========================= رفتارهای جدید =========================
AUTO_JOIN_PV_LINKS = True
AUTO_ACTIVATION_POLL_SECONDS = 5
ROLE_CACHE_SECONDS = 20
SUDO_PROMOTE_TTL = 60
ROLE_MEMBER_SCAN_LIMIT = 5000
# وقتی اسکن خودکار تازه شروع می‌شود، این مقدار زمان بیشتری برای همگام‌شدن چت‌ها می‌دهد.
AUTO_ACTIVATION_MAX_CHATS = 200
PV_REQUIRE_LINK_MESSAGE = True
PV_QUOTE_REPLIES = True
PV_SPLUS_ONLY_DOMAINS = ("splus.ir", "splusapp.ir", "splusplus.ir")

# ========================= سخنگوی داخلی و خروج خودکار =========================
SPEAKER_COOLDOWN_SECONDS = 8        # حداقل فاصلهٔ پاسخ‌های بدون خطاب (ثانیه)
ADMIN_WATCH_INTERVAL_SECONDS = 600  # هر چند ثانیه وضعیت ادمین بودن FBI در گروه‌ها بررسی شود
AUTO_LEAVE_NO_ADMIN_DAYS = 7        # اگر FBI این تعداد روز در گروهی ادمین نباشد، گروه را ترک می‌کند

# ========================= پنل وب گروه‌ها و سازنده =========================
# روی همهٔ رابط‌های شبکه گوش می‌دهد؛ برای دسترسی عمومی HTTPS/تونل لازم است.
WEB_PANEL_ENABLED = os.getenv("FBI_WEB_PANEL_ENABLED", "1").strip().lower() not in {"0", "false", "no", "off"}
WEB_PANEL_HOST = os.getenv("FBI_WEB_PANEL_HOST", "0.0.0.0").strip() or "0.0.0.0"
WEB_PANEL_PORT = int(os.getenv("FBI_WEB_PANEL_PORT", "8080"))
WEB_PANEL_BASE_URL = os.getenv("FBI_WEB_PANEL_BASE_URL", "").strip().rstrip("/")
WEB_PANEL_TOKEN_TTL_HOURS = int(os.getenv("FBI_WEB_PANEL_TOKEN_TTL_HOURS", "168"))
WEB_PANEL_CREATOR_TOKEN_TTL_HOURS = int(os.getenv("FBI_WEB_PANEL_CREATOR_TOKEN_TTL_HOURS", "24"))
WEB_PANEL_TUNNEL_ENABLED = os.getenv("FBI_WEB_PANEL_TUNNEL", "1").strip().lower() in {"1", "true", "yes", "on"}
WEB_PANEL_AUTO_INSTALL_CLOUDFLARED = os.getenv("FBI_WEB_PANEL_AUTO_INSTALL_CLOUDFLARED", "1").strip().lower() in {"1", "true", "yes", "on"}

# ========================= عضویت اجباری PV =========================
FORCE_JOIN_ENABLED = True
FORCE_JOIN_CHANNEL_LINK = "https://splus.ir/FBIbotdd"
FORCE_JOIN_CHANNEL_ID = "-10024639591"
FORCE_JOIN_CACHE_SECONDS = 5
FORCE_JOIN_REQUIRE_BOT_ADMIN = True
FORCE_JOIN_SUPPORT = "@Radvinhha"  # فقط نمایش داده می‌شود؛ عضویت در یک کاربر قابل بررسی نیست.

# ========================= گزارش پیام =========================
REPORT_MUTE_MINUTES = None  # None = سکوت دائم؛ «سکوت» گزارش همان رفتار سکوت معمولی را دارد.

BIO_LINK_MODERATION = True
BIO_CHECK_CACHE_SECONDS = 10
BIO_LINK_MUTE_SECONDS = None  # None = دائمی تا unmute شود.
# ========================= پایداری و اتوماسیون =========================
AUTO_JOIN_ACTIVE_CALLS = True
AUTO_JOIN_CALL_SCAN_SECONDS = 30
SPAM_WAVE_WINDOW_SECONDS = 180
SPAM_WAVE_THRESHOLD = 6
SPAM_WAVE_LOCK_SECONDS = 3600
SPAM_WAVE_TYPES = ("link", "username", "hyperlink", "spam", "badword")
PUNISH_NOTICE_COOLDOWN_SECONDS = 30
GUIDE_NEW_MESSAGE_AFTER_SECONDS = 180
MUTE_RESTORE_INTERVAL_SECONDS = 60
MUTE_PERMISSION_RETRY_SECONDS = 15
# وفاداری / نظرسنجی فعالیت سالم
LOYALTY_DAILY_THRESHOLD = 500
LOYALTY_SURVEY_SECONDS = 600
LOYALTY_POLL_CLOSE_SECONDS = 600
GROUP_PERMISSION_RESTORE_INTERVAL_SECONDS = 30
SCAN_ALL_MAX_MESSAGES = 100000
WELCOME_INCLUDE_JOIN_DATE = True
GUIDE_EDIT_SAME_MESSAGE = True
LOCK_STATUS_EDIT_SAME_MESSAGE = True

BIO_LINK_MUTE_MINUTES = None  # legacy fallback; prefer BIO_LINK_MUTE_SECONDS.
MUTE_DEFAULT_MINUTES = None   # None = دائمی وقتی فقط «سکوت» با ریپلای زده شود.
BIO_LINK_DELETE_MESSAGE = True
BIO_LINK_NOTIFY = True

DEFAULT_MAX_FILE_MB = 20

DEFAULT_SETTINGS = {
    "active": False,
    "manual_disabled": False,
    "subscription_active": True,
    "subscription_until": 0,
    "subscription_revoked_at": 0,
    "subscription_notice_sent_at": 0,
    "opened_at": 0,
    "subordinate": False,
    "subordinate_owner": None,

    "owner": None,
    "owner_verified": False,
    "owner_source": "",
    "activated_by": "",
    "admins": [],
    "specials": [],
    "_bot_admin": False,
    "reports": {},

    "locks": {
        "link": True, "forward": True, "username": True, "hyperlink": True,
        "file_size": False, "hang_code": False,
    },
    "lock_warn": {},
    "lock_ban": {},
    "max_warn": 3,
    "lock_max_warn": {},
    "spam_level": 2,
    "max_file_mb": DEFAULT_MAX_FILE_MB,

    "sleep": False,
    "fun": True,
    "manage_mode": False,
    "allowed_message": None,
    "allowed_message_active": False,
    "closed": False,
    "closed_permissions_snapshot": {},
    "group_permission_restore_last": 0,

    "welcome": False,
    "welcome_text": "",
    "goodbye": False,
    "goodbye_text": "",
    "rules": "",
    "banner": "",
    "link": "",
    "speaker": True,
    "talkative": False,
    "sassy": False,
    "learning": False,
    "auto_delete": False,
    "auto_delete_time": 60,
    "lang": "fa",
    "disabled_cmds": [],

    # گفت‌وگوی بدون دستور سخنگوی داخلی (روشن = بدون دستور هم جواب می‌دهد).
    "ai_chat": True,
    "ai_behavior": True,

    "exempt": [],
    "block": [],
    "mute": {},
    "mute_reason": {},
    "mute_started": {},
    "mute_messages": {},
    "mute_expired_cleanup": {},
    "mute_server_applied": {},
    "mute_permission_last": {},
    "loyalty_surveys": {},
    "kill": [],
    "allowed": [],
    "filters": [],
    "warns": {},
    "timers": [],

    "bio_link_mute": True,
    "bio_link_last_scan": {},
    "bio_link_hits": 0,

    "music_state": {
        "queue": [], "current": None, "volume": 100, "speed": 1.0,
        "muted": False, "position": 0, "target": None, "recent": []
    },
    "voice_state": {"active": False, "slug": None},
    "guide_message_ids": {},
    "lock_status_messages": {},
    "status_messages": {},
    "closed_until": 0,
    "wave_lock_notified_at": 0,
    "scanned_message_ids": [],
    "scan_all_last": 0,
    "guide_message_meta": {},
    "mute_restore_last": 0,
    "metadata_lock": False,
    "metadata_lock_snapshot": {},
    "metadata_last_check": 0,
    "metadata_last_restore": 0,
    "advanced_replies": [],
    "reply_rules": [],
    "quick_replies": {},
    "advanced": {
        "duplicate_lock": False,
        "flood_lock": False,
        "flood_threshold": 5,
        "flood_window": 8,
        "edited_lock": False,
        "edited_warn": False,
        "metadata_title_lock": False,
        "slow_mode_seconds": 0,
        "slow_last": {},
        "last_messages": {},
        "flood": {},
    },
}


def first_existing_ffmpeg() -> str | None:
    for candidate in FFMPEG_CANDIDATES:
        if candidate and os.path.isfile(candidate):
            return os.path.abspath(candidate)
    return shutil.which("ffmpeg")


def ensure_dirs() -> None:
    for path in (
        DATA_DIR, DB_DIR, JSON_DB_DIR, BACKUP_DIR, SESSION_DIR,
        TEMP_DIR, MUSIC_DIR, DOWNLOAD_DIR, FFMPEG_DIR, FFMPEG_BIN_DIR,
    ):
        os.makedirs(path, exist_ok=True)
