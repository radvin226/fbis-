# -*- coding: utf-8 -*-
"""بازی‌های گروه: «میو» با امتیاز تصادفی، و کوییز خودکار که اولین پاسخ درست جایزه می‌گیرد."""
from __future__ import annotations

import asyncio
import logging
import math
import os
import random
import re
import shutil
import subprocess
import tempfile
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
    top = top_points(ctx.g, 10)
    if not top:
        await ctx.reply("هنوز کسی امتیاز نگرفته است. «میو» بگو یا منتظر کوییز بعدی باش 🎯")
        return True
    lines = ["🏆 جدول امتیاز گروه"]
    for i, (uid, n) in enumerate(top, 1):
        name = await ctx.target_link(uid)
        lines.append(f"{_fa(i)}. {name} — {_fa(n)} امتیاز")
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


TITLE_STEPS = ((0, "تازه‌وارد"), (50, "فعال"), (150, "ستاره"), (400, "افسانه"), (1000, "اسطوره"))
VOICE_CMD = re.compile(r"ویس\s+(?!قفل)(.+)")


def title_for(points):
    label = TITLE_STEPS[0][1]
    for threshold, name in TITLE_STEPS:
        if int(points or 0) >= threshold:
            label = name
    return label


def top_points(g, n=5):
    pts = g.get("points") or {}
    rows = [(uid, int(v)) for uid, v in pts.items() if int(v or 0) > 0]
    return sorted(rows, key=lambda item: (-item[1], str(item[0])))[:n]


async def _bonus(ctx):
    g = ctx.g
    uid = str(ctx.author)
    now = time.time()
    last = g.setdefault("bonus_last", {})
    period = int(getattr(config, "BONUS_COOLDOWN_HOURS", 24)) * 3600
    waited = now - float(last.get(uid, 0) or 0)
    if waited < period:
        left_h = max(1, math.ceil((period - waited) / 3600))
        await ctx.reply(f"🎁 بونوس امروزت را گرفته‌ای. {_fa(left_h)} ساعت دیگر دوباره بیا.")
        return True
    amount = int(getattr(config, "BONUS_POINTS", 20))
    last[uid] = now
    total = _add_points(g, uid, amount)
    await ctx.reply(f"🎁 بونوس روزانه! {_fa(amount)} امتیاز گرفتی. مجموع: {_fa(total)} • عنوان: {title_for(total)}")
    return True


_LOG = logging.getLogger("fbi.games")


class NoVoiceEngine(RuntimeError):
    """هیچ موتور صوتی روی سرور نصب نیست."""


def _espeak_bin():
    name = str(getattr(config, "ESPEAK_BIN", "") or "espeak-ng")
    return shutil.which(name) or shutil.which("espeak")


def _synth_default(text, base):
    """ویس می‌سازد: اول espeak-ng (آفلاین، بدون اینترنت)، وگرنه gTTS (نیاز به اینترنت)."""
    exe = _espeak_bin()
    if exe:
        wav = base + ".wav"
        subprocess.run([exe, "-v", "fa", "-w", wav, text], check=True, timeout=60, capture_output=True)
        return wav
    try:
        from gtts import gTTS  # نصب: pip install gtts
    except ImportError as exc:
        raise NoVoiceEngine("espeak-ng و gtts هیچ‌کدام نصب نیستند") from exc
    mp3 = base + ".mp3"
    gTTS(text=text, lang="fa").save(mp3)
    return mp3


SYNTH = _synth_default


def _temp_dir():
    directory = str(getattr(config, "TEMP_DIR", "") or "") or tempfile.gettempdir()
    try:
        os.makedirs(directory, exist_ok=True)
        return directory
    except OSError:
        return tempfile.gettempdir()


def _voice_error(what, exc):
    detail = " ".join(str(exc).split())[:120]
    text = f"🎙 {what} ناموفق بود: {type(exc).__name__}"
    return text + (f" — {detail}" if detail else "")


async def _voice(ctx, speech):
    speech = speech.strip()[:int(getattr(config, "VOICE_MAX_CHARS", 200))]
    if not speech:
        return False
    base = os.path.join(_temp_dir(), f"voice_{int(time.time() * 1000)}")
    try:
        path = await asyncio.to_thread(SYNTH, speech, base)
    except NoVoiceEngine:
        await ctx.reply("🎙 موتور ساخت ویس روی سرور نصب نیست. یکی را نصب کنید: espeak-ng (بدون اینترنت) یا pip install gtts (با اینترنت).")
        return True
    except Exception as exc:
        _LOG.exception("voice synthesis failed")
        await ctx.reply(_voice_error("ساخت ویس", exc))
        return True
    try:
        sent = await ctx.api.send_music(ctx.guid, path, reply_to=ctx.message_id)
        if sent is None:
            _LOG.warning("voice send returned nothing chat=%s", ctx.guid)
            await ctx.reply("🎙 ارسال ویس انجام نشد: سرور پاسخی نداد.")
    except Exception as exc:
        _LOG.exception("voice send failed")
        await ctx.reply(_voice_error("ارسال ویس", exc))
    finally:
        try:
            os.remove(path)
        except OSError:
            pass
    return True


PET_SHOP = (
    ("گربه", 300, "🐱"),
    ("سگ", 500, "🐶"),
    ("خرگوش", 250, "🐰"),
    ("جوجه", 150, "🐥"),
    ("ماهی قرمز", 100, "🐠"),
)
PET_BUY_CMD = re.compile(r"خرید پت\s+(.+)")
PET_FOOD_COST = 10
PET_FOOD_HUNGER = 40
PET_FISH_HUNGER = 30
PET_PLAY_HAPPY = 20
PET_PET_HAPPY = 10
PET_PLAY_COOLDOWN = 3600
PET_PET_COOLDOWN = 1800
FISH_COOLDOWN = 1800
PET_RUNAWAY_HOURS = 48
HUNGER_PER_HOUR = 4
HAPPY_PER_HOUR = 3
DAY = 86400


def _clamp(value):
    return max(0, min(100, int(round(value))))


def _bar(value):
    filled = max(0, min(10, int(round(float(value) / 10))))
    return "▰" * filled + "▱" * (10 - filled)


def _pets(g):
    return g.setdefault("pets", {})


def _spend_points(g, uid, amount):
    pts = g.setdefault("points", {})
    have = int(pts.get(str(uid), 0))
    if have < amount:
        return False
    pts[str(uid)] = have - amount
    db.save("groups")
    return True


def _upkeep(price):
    return max(1, int(price) // 50)


def _pet_update(g, uid, now):
    """از آخرین بررسی تا حالا: گرسنگی، خوشحالی، هزینهٔ روزانه و فرار پت را اعمال می‌کند."""
    pets = _pets(g)
    pet = pets.get(str(uid))
    if not pet:
        return []
    notes = []
    hours = max(0.0, (now - float(pet.get("checked", now))) / 3600)
    if hours > 0:
        pet["hunger"] = _clamp(pet["hunger"] - HUNGER_PER_HOUR * hours)
        pet["happiness"] = _clamp(pet["happiness"] - HAPPY_PER_HOUR * hours)
    pet["checked"] = now
    paid_until = float(pet.get("paid_until", now))
    due = 0 if now < paid_until else int((now - paid_until) // DAY) + 1
    fee = _upkeep(PET_SHOP[pet["kind"]][1])
    unpaid_noted = False
    for _ in range(min(due, 30)):
        if _spend_points(g, uid, fee):
            notes.append(f"💰 هزینهٔ روزانهٔ پت: {_fa(fee)} امتیاز")
        else:
            pet["happiness"] = _clamp(pet["happiness"] - 15)
            if not unpaid_noted:
                notes.append("😢 امتیاز کافی برای هزینهٔ پت نداشتی و پتت ناراحت شد.")
                unpaid_noted = True
        paid_until += DAY
    pet["paid_until"] = paid_until
    if pet["hunger"] == 0 and pet["happiness"] == 0:
        pet.setdefault("neglect_since", now)
        if now - float(pet["neglect_since"]) >= PET_RUNAWAY_HOURS * 3600:
            pets.pop(str(uid), None)
            db.save("groups")
            return ["🐾 پتت از بی‌توجهی فرار کرد. می‌توانی پت تازه‌ای بخری."]
    else:
        pet.pop("neglect_since", None)
    db.save("groups")
    return notes


async def _pet_shop(ctx):
    lines = ["🛒 فروشگاه پت (هر کاربر یک پت می‌تواند داشته باشد):"]
    for i, (name, price, emoji) in enumerate(PET_SHOP, 1):
        lines.append(f"{_fa(i)}. {emoji} {name} — {_fa(price)} امتیاز • هزینهٔ روزانه {_fa(_upkeep(price))}")
    lines.append("برای خرید بنویس: خرید پت [شماره یا اسم]")
    await ctx.reply("\n".join(lines))
    return True


def _shop_index(choice):
    choice = utils.normalize(choice).strip()
    if choice.isdigit():
        n = int(choice)
        return n - 1 if 1 <= n <= len(PET_SHOP) else None
    for i, (name, _price, _emoji) in enumerate(PET_SHOP):
        if utils.normalize(name) == choice:
            return i
    return None


async def _buy_pet(ctx, choice):
    g = ctx.g
    uid = str(ctx.author)
    now = time.time()
    if uid in _pets(g):
        _pet_update(g, uid, now)
        if uid in _pets(g):
            await ctx.reply("🐾 تو از قبل پت داری. اول با «پت من» وضعیتش را ببین.")
            return True
    idx = _shop_index(choice)
    if idx is None:
        await ctx.reply("این پت در فروشگاه نیست. «فروشگاه پت» را بزن.")
        return True
    name, price, emoji = PET_SHOP[idx]
    if not _spend_points(g, uid, price):
        await ctx.reply(f"💸 امتیاز کافی نداری. قیمت {name}: {_fa(price)} امتیاز.")
        return True
    _pets(g)[uid] = {"kind": idx, "name": name, "hunger": 80, "happiness": 80,
                     "bought_at": now, "checked": now, "paid_until": now + DAY,
                     "last_play": 0, "last_pet": 0}
    db.save("groups")
    await ctx.reply(f"🎉 {emoji} {name} خریدی! هر روز {_fa(_upkeep(price))} امتیاز هزینه دارد. از آن مراقبت کن.")
    return True


async def _pet_status(ctx):
    g = ctx.g
    uid = str(ctx.author)
    now = time.time()
    if uid not in _pets(g):
        await ctx.reply("🐾 هنوز پت نداری. «فروشگاه پت» را بزن و یکی بخر.")
        return True
    notes = _pet_update(g, uid, now)
    pet = _pets(g).get(uid)
    if pet is None:
        await ctx.reply("\n".join(notes))
        return True
    name, price, emoji = PET_SHOP[pet["kind"]]
    points = int((g.get("points") or {}).get(uid, 0))
    fish = int((g.get("fish") or {}).get(uid, 0))
    lines = [
        f"{emoji} پت تو: {name}",
        f"🍗 گرسنگی: {_bar(pet['hunger'])} {_fa(pet['hunger'])}٪",
        f"😊 خوشحالی: {_bar(pet['happiness'])} {_fa(pet['happiness'])}٪",
        f"💰 هزینهٔ روزانه: {_fa(_upkeep(price))} امتیاز • امتیاز تو: {_fa(points)}",
        f"🎣 ماهی‌های تو: {_fa(fish)}",
    ]
    lines += notes
    await ctx.reply("\n".join(lines))
    return True


async def _feed(ctx):
    g = ctx.g
    uid = str(ctx.author)
    now = time.time()
    if uid not in _pets(g):
        await ctx.reply("🐾 پت نداری که غذا بدهی.")
        return True
    _pet_update(g, uid, now)
    pet = _pets(g).get(uid)
    if pet is None:
        await ctx.reply("🐾 پتت فرار کرده بود.")
        return True
    if pet["hunger"] >= 90:
        await ctx.reply(f"🍗 {pet['name']} سیر است. بعداً غذا بده.")
        return True
    if not _spend_points(g, uid, PET_FOOD_COST):
        await ctx.reply(f"💸 غذا {_fa(PET_FOOD_COST)} امتیاز است و امتیاز کافی نداری.")
        return True
    pet["hunger"] = _clamp(pet["hunger"] + PET_FOOD_HUNGER)
    pet["happiness"] = _clamp(pet["happiness"] + 5)
    db.save("groups")
    await ctx.reply(f"🍗 {pet['name']} غذا خورد! گرسنگی: {_fa(pet['hunger'])}٪ (هزینه: {_fa(PET_FOOD_COST)} امتیاز)")
    return True


async def _pet_action(ctx, kind):
    g = ctx.g
    uid = str(ctx.author)
    now = time.time()
    if uid not in _pets(g):
        await ctx.reply("🐾 هنوز پت نداری.")
        return True
    _pet_update(g, uid, now)
    pet = _pets(g).get(uid)
    if pet is None:
        await ctx.reply("🐾 پتت فرار کرده بود.")
        return True
    if kind == "play":
        cooldown, gain, label = PET_PLAY_COOLDOWN, PET_PLAY_HAPPY, "بازی"
    else:
        cooldown, gain, label = PET_PET_COOLDOWN, PET_PET_HAPPY, "نوازش"
    key = f"last_{kind}"
    waited = now - float(pet.get(key, 0) or 0)
    if waited < cooldown:
        mins = max(1, math.ceil((cooldown - waited) / 60))
        await ctx.reply(f"😴 {pet['name']} کمی استراحت می‌خواهد. {_fa(mins)} دقیقهٔ دیگر.")
        return True
    pet["happiness"] = _clamp(pet["happiness"] + gain)
    if kind == "play":
        pet["hunger"] = _clamp(pet["hunger"] - 5)
    pet[key] = now
    db.save("groups")
    await ctx.reply(f"💞 {label} با {pet['name']} انجام شد! خوشحالی: {_fa(pet['happiness'])}٪")
    return True


async def _fish(ctx):
    g = ctx.g
    uid = str(ctx.author)
    now = time.time()
    last = g.setdefault("fish_last", {})
    waited = now - float(last.get(uid, 0) or 0)
    if waited < FISH_COOLDOWN:
        mins = max(1, math.ceil((FISH_COOLDOWN - waited) / 60))
        await ctx.reply(f"🎣 هنوز قلاب را جمع نکرده‌ای؛ {_fa(mins)} دقیقهٔ دیگر.")
        return True
    last[uid] = now
    roll = random.random()
    if roll < 0.50:
        await ctx.reply("🎣 هیچی نگرفتی. دفعهٔ بعد شانس بیشتری داری!")
    elif roll < 0.80:
        fish = g.setdefault("fish", {})
        fish[uid] = int(fish.get(uid, 0)) + 1
        await ctx.reply(f"🐟 یک ماهی گرفتی! ماهی‌های تو: {_fa(fish[uid])} (با «ماهی بده به پت» به پتت بده)")
    elif roll < 0.95:
        amount = random.randint(5, 30)
        total = _add_points(g, uid, amount)
        await ctx.reply(f"💰 گنج پیدا کردی! {_fa(amount)} امتیاز. مجموع: {_fa(total)}")
    elif roll < 0.99:
        have = int((g.get("points") or {}).get(uid, 0))
        lost = min(5, have)
        if lost:
            _spend_points(g, uid, lost)
        await ctx.reply(f"🥾 یک کفش قدیمی گرفتی! {_fa(lost)} امتیاز از دست رفت.")
    else:
        total = _add_points(g, uid, 100)
        await ctx.reply(f"🌟 ماهی طلایی! ۱۰۰ امتیاز گرفتی. مجموع: {_fa(total)}")
    db.save("groups")
    return True


async def _feed_fish(ctx):
    g = ctx.g
    uid = str(ctx.author)
    now = time.time()
    if uid not in _pets(g):
        await ctx.reply("🐾 پت نداری.")
        return True
    _pet_update(g, uid, now)
    pet = _pets(g).get(uid)
    if pet is None:
        await ctx.reply("🐾 پتت فرار کرده بود.")
        return True
    fish = g.setdefault("fish", {})
    if int(fish.get(uid, 0)) < 1:
        await ctx.reply("🐟 ماهی نداری. اول «ماهی گیری» کن.")
        return True
    if pet["hunger"] >= 90:
        await ctx.reply(f"🍗 {pet['name']} سیر است.")
        return True
    fish[uid] = int(fish[uid]) - 1
    pet["hunger"] = _clamp(pet["hunger"] + PET_FISH_HUNGER)
    pet["happiness"] = _clamp(pet["happiness"] + 5)
    db.save("groups")
    await ctx.reply(f"🐟 {pet['name']} ماهی خورد! گرسنگی: {_fa(pet['hunger'])}٪")
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
    if text == "بونوس":
        return await _bonus(ctx)
    m_voice = VOICE_CMD.fullmatch(text)
    if m_voice:
        return await _voice(ctx, m_voice.group(1))
    if text.startswith(("فروشگاه پت", "خرید پت", "پت من", "پتم", "غذا دادن", "بازی با پت", "نوازش پت", "ماهی")):
        _LOG.info("pet/fish command chat=%s user=%s text=%s", ctx.guid, ctx.author, text)
    if text == "فروشگاه پت":
        return await _pet_shop(ctx)
    m_buy = PET_BUY_CMD.fullmatch(text)
    if m_buy:
        return await _buy_pet(ctx, m_buy.group(1))
    if text in ("پت من", "پتم"):
        return await _pet_status(ctx)
    if text == "غذا دادن":
        return await _feed(ctx)
    if text == "بازی با پت":
        return await _pet_action(ctx, "play")
    if text == "نوازش پت":
        return await _pet_action(ctx, "pet")
    if text == "ماهی گیری":
        return await _fish(ctx)
    if text in ("ماهی بده به پت", "غذا ماهی"):
        return await _feed_fish(ctx)
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
