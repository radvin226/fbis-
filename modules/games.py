# -*- coding: utf-8 -*-
"""بازی‌های گروه: «میو» با امتیاز تصادفی، و کوییز خودکار که اولین پاسخ درست جایزه می‌گیرد."""
from __future__ import annotations

import asyncio
import math
import random
import re
import time

import config
from core import ranks, utils
from core.db import db

QUIZ_BANK = (
    ("پایتخت ایران چیست؟", ("تهران",)),
    ("بلندترین قله ایران چیست؟", ("دماوند",)),
    ("بزرگ‌ترین سیاره منظومه شمسی کدام است؟", ("مشتری",)),
    ("سرخ‌ترین سیاره منظومه شمسی کدام است؟", ("مریخ",)),
    ("نماد شیمیایی طلا چیست؟", ("au",)),
    ("واحد اندازه‌گیری نیرو چیست؟", ("نیوتون",)),
    ("در یک هفته چند روز است؟", ("7", "هفت")),
    ("کدام قاره بزرگ‌ترین مساحت را دارد؟", ("آسیا",)),
    ("بزرگ‌ترین پستاندار خشکی‌زی کدام است؟", ("فیل",)),
    ("تنها فلزی که در دمای اتاق مایع است؟", ("جیوه",)),
    ("کوچک‌ترین عدد اول چیست؟", ("2", "دو")),
    ("تعداد ضلع‌های مثلث چند است؟", ("3", "سه")),
    ("بزرگ‌ترین اقیانوس جهان کدام است؟", ("آرام", "اقیانوس آرام")),
    ("بزرگ‌ترین رود ایران کدام است؟", ("کارون",)),
    ("پایتخت ژاپن کدام شهر است؟", ("توکیو",)),
    ("اولین رئیس‌جمهور ایران پس از انقلاب کیست؟", ("بنی‌صدر", "بنی صدر")),
    ("گاز اصلی هوای زمین کدام است؟", ("نیتروژن", "ازت")),
    ("دورترین سیاره از خورشید کدام است؟", ("نپتون",)),
    ("سریع‌ترین حیوان خشکی‌زی کدام است؟", ("یوزپلنگ", "یوز")),
)


POINT_CMD = re.compile(r"پوینت\s*(-?\d+)")
POINT_LIMIT = 10000


def _fa(value):
    return utils.fa_digits(value)


def _norm(text):
    return utils.normalize(str(text or "")).strip(" .!?؟،,").lower()


def _answer_ok(text, answers):
    t = _norm(text)
    return bool(t) and any(t == _norm(a) for a in answers)


def _add_points(g, uid, amount):
    pts = g.setdefault("points", {})
    pts[str(uid)] = int(pts.get(str(uid), 0)) + int(amount)
    db.save("groups")
    return pts[str(uid)]


def _pick_question(g):
    last = g.get("quiz_last_idx")
    choices = [i for i in range(len(QUIZ_BANK)) if i != last] or [0]
    idx = random.choice(choices)
    g["quiz_last_idx"] = idx
    question, answers = QUIZ_BANK[idx]
    return question, tuple(answers)


async def _meow(ctx):
    g = ctx.g
    uid = str(ctx.author)
    now = time.time()
    last_map = g.setdefault("meow_last", {})
    cooldown = int(getattr(config, "MEOW_COOLDOWN_SECONDS", 300))
    waited = now - float(last_map.get(uid, 0) or 0)
    if waited < cooldown:
        wait_min = max(1, math.ceil((cooldown - waited) / 60))
        await ctx.reply(f"🐱 هنوز نه! {_fa(wait_min)} دقیقهٔ دیگر می‌توانی دوباره میو بگویی.")
        return True
    lo = int(getattr(config, "MEOW_POINTS_MIN", 1))
    hi = int(getattr(config, "MEOW_POINTS_MAX", 10))
    points = random.randint(min(lo, hi), max(lo, hi))
    last_map[uid] = now
    total = _add_points(g, uid, points)
    minutes = max(1, math.ceil(cooldown / 60))
    await ctx.reply(
        f"🐱 میو! {_fa(points)} امتیاز گرفتی. مجموع امتیازت: {_fa(total)}\n"
        f"⏳ {_fa(minutes)} دقیقهٔ دیگر دوباره میو بگو."
    )
    return True


async def _check_quiz(ctx):
    """اگر کوییزی باز است و پاسخ درست است، جایزه داده و کوییز بسته می‌شود.

    بستن کوییز قبل از هر await انجام می‌شود تا دو پاسخ همزمان دو بار جایزه نگیرند.
    """
    g = ctx.g
    quiz = g.get("quiz")
    if not quiz or not _answer_ok(ctx.text, quiz.get("answers", ())):
        return
    g["quiz"] = None
    prize = int(getattr(config, "QUIZ_PRIZE", 50))
    _add_points(g, ctx.author, prize)
    await ctx.reply(f"🏆 جواب درست بود! {_fa(prize)} امتیاز برای تو. جواب: {quiz['answers'][0]}")


async def _my_points(ctx):
    pts = (ctx.g.get("points") or {}).get(str(ctx.author), 0)
    await ctx.reply(f"🏆 امتیاز تو: {_fa(pts)}")
    return True


async def _leaderboard(ctx):
    pts = ctx.g.get("points") or {}
    top = sorted(((uid, n) for uid, n in pts.items() if n > 0), key=lambda item: (-item[1], str(item[0])))[:10]
    if not top:
        await ctx.reply("هنوز کسی امتیاز نگرفته است. «میو» بگو یا منتظر کوییز بعدی باش 🎯")
        return True
    lines = ["🏆 جدول امتیاز گروه"]
    for i, (uid, n) in enumerate(top, 1):
        lines.append(f"{_fa(i)}. {uid} — {_fa(n)} امتیاز")
    await ctx.reply("\n".join(lines))
    return True


async def _give_points(ctx):
    """دستور سازنده: روی پیام کاربر ریپلای می‌کند و می‌نویسد «پوینت ۲۰» تا امتیاز بدهد (عدد منفی کم می‌کند)."""
    m = POINT_CMD.fullmatch(utils.normalize(ctx.text or "").strip())
    if not m:
        return False
    if ranks.rank(ctx.guid, ctx.author) < ranks.CREATOR:
        return False
    amount = int(m.group(1))
    if abs(amount) > POINT_LIMIT:
        await ctx.reply(f"حداکثر {_fa(POINT_LIMIT)} امتیاز در هر بار.")
        return True
    if not ctx.reply_id:
        await ctx.reply("روی پیام کاربر ریپلای کن و بنویس: پوینت ۲۰")
        return True
    target = await ctx.target()
    if not target or str(target) == str(getattr(config, "BOT_GUID", "")):
        await ctx.reply("این کاربر قابل امتیاز دادن نیست.")
        return True
    pts = ctx.g.setdefault("points", {})
    new_total = max(0, int(pts.get(str(target), 0)) + amount)
    pts[str(target)] = new_total
    db.save("groups")
    sign = "+" if amount > 0 else ""
    await ctx.reply(f"✅ {sign}{_fa(amount)} امتیاز به کاربر داده شد. مجموع: {_fa(new_total)}")
    return True


async def handle(ctx) -> bool:
    if not getattr(ctx, "is_group", False) or not ctx.author or not ctx.guid:
        return False
    await _check_quiz(ctx)
    if await _give_points(ctx):
        return True
    text = (ctx.text or "").strip()
    if text == "میو":
        return await _meow(ctx)
    if text == "امتیاز من":
        return await _my_points(ctx)
    if text in ("جدول امتیاز", "جدول امتیازها"):
        return await _leaderboard(ctx)
    return False


async def _send(api, guid, text):
    try:
        await api.send(guid, text)
    except Exception:
        pass


async def tick_once(api, guids, now=None):
    """کوییزهای خودکار را می‌فرستد و کوییزهای بی‌پاسخ را می‌بندد. خروجی: (تعداد فرستاده، تعداد بسته)."""
    now = time.time() if now is None else float(now)
    interval = max(1, int(getattr(config, "QUIZ_INTERVAL_MINUTES", 60))) * 60
    timeout = max(1, int(getattr(config, "QUIZ_TIMEOUT_MINUTES", 10))) * 60
    prize = int(getattr(config, "QUIZ_PRIZE", 50))
    posted = closed = 0
    for guid in guids:
        g = db.group(str(guid))
        if not g.get("active") or g.get("manual_disabled") or g.get("left_at"):
            continue
        if not g.get("auto_quiz", True):
            continue
        quiz = g.get("quiz")
        if quiz:
            if now - float(quiz.get("started", 0)) >= timeout:
                g["quiz"] = None
                db.save("groups")
                await _send(api, guid, f"⏰ کسی جواب نداد. جواب درست: {quiz['answers'][0]}")
                closed += 1
            continue
        if "last_quiz_at" not in g:
            g["last_quiz_at"] = now  # اولین بار: فقط زمان ثبت می‌شود و کوییزی همان لحظه فرستاده نمی‌شود
            db.save("groups")
            continue
        if now - float(g.get("last_quiz_at") or 0) < interval:
            continue
        question, answers = _pick_question(g)
        g["quiz"] = {"q": question, "answers": list(answers), "started": now}
        g["last_quiz_at"] = now
        db.save("groups")
        await _send(api, guid, f"🧠 کوییز! اولین نفری که جواب درست بدهد {_fa(prize)} امتیاز می‌گیرد.\n❓ {question}")
        posted += 1
    return posted, closed


async def loop(api, guids_fn, log=None):
    await asyncio.sleep(90)
    while True:
        try:
            await tick_once(api, await guids_fn())
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if log is not None:
                log.warning("games loop failed: %s", exc)
        await asyncio.sleep(60)
