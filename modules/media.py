# -*- coding: utf-8 -*-
"""
FBI — سیستم پخش موزیک Voice Chat

FFmpeg:
    تبدیل فایل به PCM 16bit / 48kHz / stereo

Voice Chat:
    RubPy voice_chat_player

نکته:
    Connection برگشتی از voice_chat_player توسط API نگه داشته می‌شود.
"""

import os
import glob
import difflib
import asyncio
import logging
import subprocess
import time
import uuid
import wave
import contextlib
import shutil

import config

from core import api as api_mod
from core import ranks, utils
from core.db import db
from core import web_search


log = logging.getLogger("fbi.media")

# مسیر کش‌شدهٔ FFmpeg. config ابتدا ffmpeg\bin\ffmpeg.exe داخل پروژه را بررسی می‌کند
# و سپس سایر مسیرهای تعریف‌شده و PATH سیستم را امتحان می‌کند.
FFMPEG_EXE: str | None = config.first_existing_ffmpeg()


AUDIO_EXTS = (
    "mp3",
    "m4a",
    "ogg",
    "wav",
    "flac",
    "aac",
    "opus",
    "webm",
)


# ================================================================
# FFmpeg
# ================================================================

def _find_ffmpeg():
    # مسیر صریح config، مسیر پروژه و در نهایت PATH سیستم.
    configured = getattr(config, "FFMPEG_CANDIDATES", ())
    for path in configured:
        if path and os.path.isfile(path):
            return os.path.abspath(path)
    found = shutil.which("ffmpeg")
    return found



def _require_ffmpeg():

    global FFMPEG_EXE

    if FFMPEG_EXE:
        return FFMPEG_EXE

    FFMPEG_EXE = _find_ffmpeg()

    if FFMPEG_EXE:
        return FFMPEG_EXE

    raise RuntimeError(
        "FFmpeg پیدا نشد.\n\n"
        "یکی از این مسیرها را تنظیم/استفاده کنید:\n"
        "ffmpeg\\bin\\ffmpeg.exe\n"
        "یا config.FFMPEG_PATH\n"
    )


# ================================================================
# Temp
# ================================================================

def _cleanup_old_temp():

    try:

        os.makedirs(
            config.TEMP_DIR,
            exist_ok=True,
        )

        now = time.time()

        for name in os.listdir(
            config.TEMP_DIR
        ):

            if not name.lower().endswith(
                ".wav"
            ):
                continue

            path = os.path.join(
                config.TEMP_DIR,
                name,
            )

            try:

                if (
                    now
                    - os.path.getmtime(path)
                    > 6 * 3600
                ):
                    os.remove(path)

            except OSError:
                pass

    except Exception:
        log.exception(
            "temp cleanup failed"
        )


# ================================================================
# Normalize
# ================================================================

def _normalize_audio(path):

    if not os.path.isfile(path):
        raise FileNotFoundError(path)

    ffmpeg = _require_ffmpeg()

    os.makedirs(
        config.TEMP_DIR,
        exist_ok=True,
    )

    _cleanup_old_temp()

    out = os.path.join(
        config.TEMP_DIR,
        (
            f"fbi_audio_"
            f"{int(time.time() * 1000)}_"
            f"{uuid.uuid4().hex[:8]}.wav"
        ),
    )

    cmd = [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-i",
        path,
        "-vn",
        "-map",
        "0:a:0",
        "-ac",
        "2",
        "-ar",
        "48000",
        "-c:a",
        "pcm_s16le",
        out,
    ]

    log.info(
        "[VoiceChat] تبدیل صوت: %s -> %s",
        path,
        out,
    )

    try:

        proc = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=120,
        )

    except subprocess.TimeoutExpired:

        raise RuntimeError(
            "تبدیل فایل صوتی با FFmpeg "
            "بیش از 120 ثانیه طول کشید."
        )

    except FileNotFoundError:

        raise RuntimeError(
            "FFmpeg اجرا نشد."
        )

    if proc.returncode != 0:

        try:
            if os.path.exists(out):
                os.remove(out)
        except OSError:
            pass

        detail = (
            proc.stderr or ""
        ).strip()

        raise RuntimeError(
            "FFmpeg نتوانست فایل صوتی را تبدیل کند:\n"
            + detail[-1200:]
        )

    if (
        not os.path.isfile(out)
        or os.path.getsize(out) < 128
    ):

        raise RuntimeError(
            "FFmpeg خروجی صوتی معتبر تولید نکرد."
        )

    return out


# ================================================================
# مدت WAV
# ================================================================

def _audio_duration(path):

    try:

        with contextlib.closing(
            wave.open(path, "rb")
        ) as wf:

            frames = wf.getnframes()
            rate = wf.getframerate()

            if rate:
                return frames / float(rate)

    except Exception as exc:

        log.warning(
            "[VoiceChat] duration read failed: %s",
            exc,
        )

    return 0.0


# ================================================================
# Voice info
# ================================================================

async def _active_voice_chat(ctx):
    try:
        return await ctx.api.active_voice_id(ctx.guid)
    except Exception:
        return None


# ================================================================
# Create voice
# ================================================================

async def _ensure_voice_chat(ctx):

    vid = await _active_voice_chat(ctx)

    if vid:
        return vid, False, None

    ok, result = await ctx.api.voice_create(
        ctx.guid
    )

    if not ok:
        return None, False, result

    vid = None

    for obj in (
        result,
        api_mod._val(
            result,
            "data",
        ),
        api_mod._val(
            result,
            "update",
        ),
    ):

        if not obj:
            continue

        vid = (
            api_mod.deep(
                obj,
                "group_voice_chat_update",
                "voice_chat_id",
            )
            or api_mod.deep(
                obj,
                "voice_chat",
                "voice_chat_id",
            )
            or api_mod._val(
                obj,
                "voice_chat_id",
            )
        )

        if vid:
            break

    return (
        str(vid) if vid else None,
        True,
        result,
    )


# ================================================================
# Library
# ================================================================

def _library():

    os.makedirs(
        config.MUSIC_DIR,
        exist_ok=True,
    )

    files = []

    for ext in AUDIO_EXTS:

        files.extend(
            glob.glob(
                os.path.join(
                    config.MUSIC_DIR,
                    f"**/*.{ext}",
                ),
                recursive=True,
            )
        )

        files.extend(
            glob.glob(
                os.path.join(
                    config.MUSIC_DIR,
                    f"**/*.{ext.upper()}",
                ),
                recursive=True,
            )
        )

    return sorted(
        set(files)
    )


def _search(query):

    files = _library()

    if not files:
        return None, []

    names = {
        os.path.splitext(
            os.path.basename(f)
        )[0]: f
        for f in files
    }

    norm_query = utils.clean_key(
        query
    )

    exact = [
        path
        for name, path in names.items()
        if norm_query
        in utils.clean_key(name)
    ]

    if exact:
        return (
            exact[0],
            exact[:5],
        )

    close = difflib.get_close_matches(
        query,
        list(names),
        n=5,
        cutoff=0.4,
    )

    if close:

        return (
            names[close[0]],
            [
                names[c]
                for c in close
            ],
        )

    return None, []


# ================================================================
# Play
# ================================================================

async def _play_in_voice(
    ctx,
    path,
    title,
):

    if not os.path.isfile(path):

        await ctx.err(
            "فایل صوتی پیدا نشد."
        )

        return False

    # ------------------------------------------------------------
    # اطمینان از Voice Chat
    # ------------------------------------------------------------

    vid, created, create_result = (
        await _ensure_voice_chat(ctx)
    )

    if not created and not vid:

        await ctx.err(
            "❌ ویس‌کال فعال نیست و ساخت آن هم موفق نشد.\n"
            "حساب اجراکننده باید دسترسی مدیریت ویس‌کال گروه را داشته باشد."
        )

        return False

    # ------------------------------------------------------------
    # تبدیل صوت
    # ------------------------------------------------------------

    try:

        normalized = await asyncio.to_thread(
            _normalize_audio,
            path,
        )

    except Exception as exc:

        log.exception(
            "[VoiceChat] audio normalization failed"
        )

        await ctx.err(
            "❌ تبدیل فایل صوتی انجام نشد.\n\n"
            f"{exc}"
        )

        return False

    # ------------------------------------------------------------
    # مدت فایل
    # ------------------------------------------------------------

    duration = await asyncio.to_thread(
        _audio_duration,
        normalized,
    )

    if duration <= 0:

        # حداقل زمان نگهداری
        duration = 10.0

    # چند ثانیه حاشیه
    keep_alive = (
        duration
        + 8.0
    )

    log.info(
        "[VoiceChat] کال آماده است | "
        "guid=%s | voice_id=%s | "
        "audio=%s | size=%s | duration=%.2fs",
        ctx.guid,
        vid,
        normalized,
        os.path.getsize(normalized),
        duration,
    )

    # ------------------------------------------------------------
    # شروع واقعی پخش
    # ------------------------------------------------------------

    ok, result = await ctx.api.voice_play(
        ctx.guid,
        normalized,
        keep_alive=keep_alive,
    )

    if not ok:

        detail = (
            str(result)
            if result
            else "خطای نامشخص"
        )

        await ctx.err(
            "❌ اتصال ویس‌کال برقرار شد، "
            "اما voice_chat_player اجرا نشد.\n\n"
            f"خطای فنی: {detail}"
        )

        try:
            if os.path.exists(normalized):
                os.remove(normalized)
        except OSError:
            pass

        return False

    # ------------------------------------------------------------
    # گزارش
    # ------------------------------------------------------------

    log.info(
        "[VoiceChat] پخش شروع شد | "
        "guid=%s | duration=%.2fs | keep_alive=%.2fs",
        ctx.guid,
        duration,
        keep_alive,
    )

    if created:

        await ctx.reply(
            f"🎙 ویس‌کال آماده شد و "
            f"پخش «{title}» شروع شد."
        )

    else:

        await ctx.reply(
            f"🎙 پخش «{title}» "
            f"داخل ویس‌کال شروع شد."
        )

    # ------------------------------------------------------------
    # حذف WAV بعد از پایان احتمالی مصرف
    #
    # مهم:
    # بلافاصله حذفش نمی‌کنیم.
    # ------------------------------------------------------------

    async def _cleanup_audio():

        try:

            await asyncio.sleep(
                keep_alive
            )

            if os.path.exists(
                normalized
            ):

                os.remove(
                    normalized
                )

                log.info(
                    "[VoiceChat] فایل موقت حذف شد: %s",
                    normalized,
                )

        except asyncio.CancelledError:

            pass

        except Exception:

            log.exception(
                "[VoiceChat] audio cleanup failed"
            )

    asyncio.create_task(
        _cleanup_audio()
    )

    return True



# ================================================================
# Online search + playlist
# ================================================================

def _state(g):
    return g.setdefault("music_state", {
        "queue": [], "current": None, "current_path": None,
        "volume": 100, "speed": 1.0, "muted": False,
        "position": 0, "target": None, "recent": [], "repeat": False,
    })


def _item_path(item):
    if isinstance(item, dict):
        return item.get("path")
    return item


def _item_title(item):
    if isinstance(item, dict):
        return item.get("title") or os.path.splitext(os.path.basename(item.get("path") or ""))[0]
    return os.path.splitext(os.path.basename(str(item)))[0]


async def _online_download(ctx, query):
    try:
        results = await web_search.search(query, 6)
    except Exception as e:
        await ctx.err(f"❌ جست‌وجوی موزیک انجام نشد: {e}")
        return None, []
    if not results:
        await ctx.err("❌ نتیجه‌ای برای این آهنگ پیدا نشد.")
        return None, []
    try:
        path = await web_search.download_audio(results[0]["url"], config.MUSIC_DIR)
    except Exception as e:
        await ctx.err(f"❌ دانلود آهنگ انجام نشد: {e}")
        return None, results
    return path, results


async def play_path(ctx, path, title=None, add_to_queue=False):
    title = title or os.path.splitext(os.path.basename(path))[0]
    g = ctx.g
    m = _state(g)
    item = {"path": os.path.abspath(path), "title": title}
    if add_to_queue:
        m["queue"].append(item)
        db.save("groups")
        return True
    ok = await _play_in_voice(ctx, path, title)
    if ok:
        old = m.get("current_path")
        if old and old != path:
            m.setdefault("recent", []).append({"path": old, "title": m.get("current") or _item_title(old)})
            m["recent"] = m["recent"][-50:]
        m["current"] = title
        m["current_path"] = os.path.abspath(path)
        db.save("groups")
    return ok

# ================================================================
# Handler
# ================================================================

async def handle(ctx) -> bool:

    text = ctx.text

    # ------------------------------------------------------------
    # لیست
    # ------------------------------------------------------------

    if text in (
        "لیست اهنگ",
        "لیست آهنگ",
        "لیست موزیک",
    ):

        files = _library()

        if not files:

            await ctx.err(
                "کتابخانه‌ی آهنگ خالی است. "
                "فایل‌های صوتی را در پوشه‌ی "
                f"«{config.MUSIC_DIR}» بگذارید."
            )

            return True

        names = [
            os.path.splitext(
                os.path.basename(f)
            )[0]
            for f in files
        ]

        chunk, page, total = (
            utils.paginate(
                names,
                1,
            )
        )

        await ctx.reply(
            f"🎵 آهنگ‌های موجود "
            f"({utils.fa_digits(len(names))}):\n\n"
            + "\n".join(
                f"• {n}"
                for n in chunk
            )
        )

        return True

    # ------------------------------------------------------------
    # کال (ساخت ویس‌چت + ارسال لینک گروه برای پیوستن)
    # ------------------------------------------------------------

    if text == "کال":

        if ctx.rank < ranks.SPECIAL:
            await ctx.err("این دستور فقط برای ادمین ویژه و مالک است.")
            return True

        vid, created, create_result = await _ensure_voice_chat(ctx)

        if not vid:
            detail = str(create_result) if create_result else "خطای نامشخص"
            await ctx.err(
                "❌ کال ساخته نشد.\n"
                "حساب اجراکننده باید دسترسی مدیریت ویس‌کال گروه را داشته باشد.\n"
                f"جزئیات فنی: {detail}"
            )
            return True

        link = ""
        try:
            info = await ctx.api.group_link(ctx.guid)
            link = (info or {}).get("link") or ""
            if not link:
                _, new_res = await ctx.api.new_link(ctx.guid)
                link = api_mod._val(new_res, "invite_link", default="") or api_mod.deep(
                    new_res, "invite_link", "invite_link"
                ) or ""
        except Exception:
            link = ""

        if created:
            msg = "🎙 کال گروه ساخته شد و فعال است.\n"
        else:
            msg = "🎙 کال گروه از قبل فعال بود.\n"

        if link:
            msg += f"برای پیوستن، وارد گروه شوید و به ویس‌چت بپیوندید:\n{link}"
        else:
            msg += "برای پیوستن به کال، وارد گروه شده و روی نوار ویس‌چت بالای صفحه بزنید."

        await ctx.reply(msg)
        return True

    # ------------------------------------------------------------
    # قطع پخش / قطع کال (پایان ویس‌چت)
    # ------------------------------------------------------------

    if text in ("توقف", "قطع پخش", "توقف پخش", "قطع موزیک", "استاپ آهنگ", "استاپ موزیک", "پایان پخش", "قطع کال"):

        if ctx.rank < ranks.SPECIAL:
            await ctx.err("این دستور فقط برای ادمین ویژه و مالک است.")
            return True

        vid = await ctx.api.active_voice_id(ctx.guid)
        ok, res = await ctx.api.voice_discard(ctx.guid, vid)

        if not ok:
            await ctx.err("قطع پخش ناموفق بود.")
        else:
            await ctx.ok("پخش موزیک متوقف و ویس‌چت قطع شد. 🔇" if text != "قطع کال" else "کال قطع شد. 📴")

        return True

    # ------------------------------------------------------------
    # دانلود / فایل
    # ------------------------------------------------------------
    if text == "دانلود" or text.startswith("دانلود "):
        if ctx.rank < ranks.SPECIAL:
            await ctx.err("فقط ادمین ویژه و مالک."); return True
        query = text[6:].strip()
        path = None
        if ctx.reply_id and not query:
            path = await ctx.api.download_message_file(ctx.guid, ctx.reply_id, config.MUSIC_DIR)
        elif query:
            path, _ = _search(query)
        if path and os.path.isfile(path):
            await ctx.ok(f"فایل آماده شد: {os.path.basename(path)}")
        else:
            await ctx.err("فایل قابل دانلود/پیدا کردن نیست. روی فایل صوتی ریپلای کنید یا نام آن را بنویسید.")
        return True

    if text.startswith("فایل"):
        query = text[4:].strip()
        if not query:
            await ctx.err("نام یا شماره فایل را بنویسید."); return True
        path, _ = _search(query)
        if path and os.path.isfile(path):
            await ctx.api.send_music(ctx.guid, path, os.path.basename(path), ctx.message_id)
        else:
            await ctx.err("فایل پیدا نشد.")
        return True

    # ------------------------------------------------------------
    # دستور پخش
    # ------------------------------------------------------------

    if not (
        text == "پخش"
        or text.startswith("پخش ")
    ):
        return False

    # ------------------------------------------------------------
    # سطح دسترسی
    # ------------------------------------------------------------

    if (
        ctx.rank == ranks.USER
        and ctx.disabled("پخش")
    ):

        await ctx.err(
            "این دستور برای کاربران عادی غیرفعال است."
        )

        return True

    query = text[4:].strip()

    # ------------------------------------------------------------
    # پخش فایل ریپلای
    # ------------------------------------------------------------

    if not query and ctx.reply_id:

        local_path = (
            await ctx.api.download_message_file(
                ctx.guid,
                ctx.reply_id,
                config.TEMP_DIR,
            )
        )

        if not local_path:

            await ctx.err(
                "فایل ریپلای‌شده قابل دانلود نیست؛ "
                "روی یک ویس/موزیک معتبر ریپلای کنید."
            )

            return True

        await play_path(ctx, local_path, "فایل ریپلای‌شده")

        return True

    # ------------------------------------------------------------
    # بدون نام
    # ------------------------------------------------------------

    if not query:

        await ctx.err(
            "نام آهنگ را بنویسید یا روی یک ویس/موزیک ریپلای بزنید.\n"
            "مثال: پخش آهنگ من"
        )

        return True

    # ------------------------------------------------------------
    # جستجو
    # ------------------------------------------------------------

    path, matches = _search(query)
    online_results = []
    if not path:
        path, online_results = await _online_download(ctx, query)
        if not path:
            return True
        matches = [path]

    name = os.path.splitext(
        os.path.basename(path)
    )[0]

    # ------------------------------------------------------------
    # ارسال آهنگ به چت
    # ------------------------------------------------------------

    try:

        requester = await ctx.target_link(
            ctx.author
        )

        caption = (
            f"🎵 {name}\n"
            f"▶️ درخواست: {requester}"
        )

        res = await ctx.api.send_music(
            ctx.guid,
            path,
            caption,
            ctx.message_id,
        )

        if res is None:

            log.warning(
                "send_music failed for %s",
                path,
            )

    except Exception:

        log.exception(
            "send_music failed"
        )

    # ------------------------------------------------------------
    # پخش در Voice
    # ------------------------------------------------------------

    # اگر آهنگ دیگری در حال پخش است، این آهنگ وارد پلی‌لیست می‌شود.
    state = _state(ctx.g)
    active = await ctx.api.active_voice_id(ctx.guid)
    if active and state.get("current_path") and os.path.abspath(path) != os.path.abspath(state.get("current_path")):
        state["queue"].append({"path": os.path.abspath(path), "title": name})
        db.save("groups")
        await ctx.ok(f"🎵 «{name}» به پلی‌لیست اضافه شد. برای پخش آن: بعدی")
    else:
        await play_path(ctx, path, name, add_to_queue=False)

    # ------------------------------------------------------------
    # نتایج مشابه
    # ------------------------------------------------------------

    if len(matches) > 1:

        others = "\n".join(
            f"• {os.path.splitext(os.path.basename(m))[0]}"
            for m in matches[1:]
        )

        await ctx.reply(
            f"🔎 نتایج مشابه دیگر:\n"
            f"{others}"
        )

    try:

        db.bump(
            ctx.guid,
            ctx.author,
            "media",
        )

    except Exception:

        log.exception(
            "media db bump failed"
        )

    return True