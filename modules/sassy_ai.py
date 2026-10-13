# -*- coding: utf-8 -*-
"""FBI SPLUS — behavioural Hugging Face speaker.

- Uses Hugging Face Inference Providers through ``huggingface_hub.InferenceClient``.
- Default model: openai/gpt-oss-120b.
- Remembers short per-user conversations plus the recent group context.
- Detects when a message is actually addressed to FBI (reply/mention/greeting).
- Keeps commands/moderation outside the AI path.
"""
from __future__ import annotations

import asyncio
import random
import re
import os
from typing import Any

import config
from core.db import db
from core import api as api_mod

try:
    from huggingface_hub import InferenceClient
except ImportError:  # optional dependency
    InferenceClient = None

_client = None
_user_locks: dict[str, asyncio.Lock] = {}

FALLBACK = [
    "درود 😎 بگو ببینم چه خبر شده؟",
    "جانم؟ FBI اینجاست 👀",
    "به‌به 😄 گوشم با توئه.",
    "اوکی رفیق، بگو ببینم چی توی سرت می‌گذره 😂",
    "من حواسم هست؛ ادامه بده 🫡",
]

SYSTEM_PROMPT = """
تو FBI SPLUS هستی؛ یک ربات مدیریت گروه و دستیار گفت‌وگویی فارسی.
لحن: طبیعی، کوتاه، باهوش، شوخ و محاوره‌ای؛ نه خشک و کتابی.
وقتی مخاطب سلام می‌کند، طبیعی سلام و احوالپرسی کن. وقتی درباره خود FBI، مدیریت گروه،
دستورها، هوش مصنوعی یا پیام قبلی خودت سؤال می‌شود، دقیق جواب بده. وقتی پیام فنی است،
جواب فنی و کاربردی بده. شوخی داشته باش، ولی به افراد توهین تحقیرآمیز، نفرت‌پراکنی،
تهدید یا حمله به ویژگی‌های شخصی نکن.
پاسخ‌ها معمولاً ۱ تا ۵ جمله باشند. از توضیح اضافه و تکرار سؤال کاربر دوری کن.
هیچ‌وقت وانمود نکن که انسان هستی؛ هویتت FBI BOT است.
""".strip()

GREETING_PATTERNS = (
    "سلام", "درود", "درود بر", "سلام علیک", "علیک سلام", "سلام و علیک",
    "صبح بخیر", "عصر بخیر", "شب بخیر", "خوش اومدی", "خوش آمدی", "hello", "hi",
)
DIRECT_PATTERNS = (
    "ربات", "بات", "fbi", "اف بی آی", "هوش مصنوعی", "هوش مصنوعی fbi",
)


def normalize(t: str) -> str:
    t = (t or "").lower()
    t = t.replace("ي", "ی").replace("ك", "ک").replace("ۀ", "ه")
    t = re.sub(r"(.)\1{2,}", r"\1", t)
    return re.sub(r"\s+", " ", t).strip()


def is_greeting(text: str) -> bool:
    n = normalize(text)
    return any(n == p or n.startswith(p + " ") for p in GREETING_PATTERNS)


def mentions_bot(text: str) -> bool:
    n = normalize(text)
    return any(p in n for p in DIRECT_PATTERNS)


def reply_for(text: str) -> str:
    n = normalize(text)
    if is_greeting(n):
        return random.choice(["درود 😎", "به‌به، درود رفیق 👋", "علیک سلام 😄", "درود بر شما 🌹"])
    if any(x in n for x in ("خوبی", "چطوری", "چه خبر")):
        return random.choice(["من که سرحالم 😎 تو چه خبر؟", "آنلاین و آماده‌ام 😂 تو چطوری؟"])
    if any(x in n for x in ("مرسی", "ممنون", "تشکر", "دمت گرم")):
        return random.choice(["قربانت 😎", "خواهش می‌کنم رفیق ✌️", "مرسی از تو 🌹"])
    return random.choice(FALLBACK)


def _get_client():
    global _client
    if _client is None and InferenceClient is not None and config.HF_TOKEN:
        try:
            kwargs = {"token": config.HF_TOKEN}
            if config.HF_PROVIDER:
                kwargs["provider"] = config.HF_PROVIDER
            try:
                _client = InferenceClient(timeout=45, **kwargs)
            except TypeError:
                _client = InferenceClient(**kwargs)
        except Exception:
            _client = None
    return _client


def configured() -> bool:
    return bool(config.HF_TOKEN) and (InferenceClient is not None or _has_requests())


def _has_requests() -> bool:
    try:
        import requests  # noqa: F401
        return True
    except Exception:
        return False


def _direct_router_request(messages):
    """Direct Hugging Face Router fallback for SDK/provider edge cases."""
    import requests
    url = "https://router.huggingface.co/v1/chat/completions"
    payload = {
        "model": config.HF_MODEL,
        "messages": messages,
        "max_tokens": config.HF_MAX_TOKENS,
        "temperature": config.HF_TEMPERATURE,
        "stream": False,
    }
    response = requests.post(
        url,
        headers={"Authorization": f"Bearer {config.HF_TOKEN}", "Content-Type": "application/json"},
        json=payload,
        timeout=45,
    )
    if response.status_code >= 400:
        raise RuntimeError(f"HF HTTP {response.status_code}: {response.text[:500]}")
    return response.json()


def _extract_text(result: Any) -> str:
    try:
        if isinstance(result, dict):
            content = (((result.get("choices") or [{}])[0]).get("message") or {}).get("content")
        else:
            content = result.choices[0].message.content
        if isinstance(content, str):
            return content.strip()
        if isinstance(content, list):
            parts = []
            for x in content:
                if isinstance(x, dict):
                    parts.append(str(x.get("text", "")))
                else:
                    parts.append(str(x))
            return " ".join(x for x in parts if x).strip()
    except Exception:
        pass
    return ""


async def reply_target_is_bot(ctx) -> bool:
    """Resolve whether the current message is replying to FBI itself."""
    me = str(getattr(ctx, "me", "") or "")
    if not me:
        return False
    rid = str(getattr(ctx, "reply_sender_id", "") or "")
    if rid and rid == me:
        return True

    original = getattr(getattr(ctx, "update", None), "original_update", None)
    getter = getattr(original, "get_reply_message", None) if original is not None else None
    if callable(getter):
        try:
            replied = getter()
            if asyncio.iscoroutine(replied):
                replied = await replied
            sender = api_mod.sender_guid_of(replied)
            if sender is not None and str(sender) == me:
                return True
        except Exception:
            pass

    reply_id = getattr(ctx, "reply_id", None)
    if reply_id:
        try:
            getter = getattr(ctx.api, "get_reply_message", None)
            if callable(getter):
                replied = await getter(ctx.guid, reply_id)
                sender = api_mod.sender_guid_of(replied)
                if sender is not None and str(sender) == me:
                    return True
        except Exception:
            pass
        try:
            result = await ctx.api.get_messages(ctx.guid, [reply_id])
            rows = api_mod.members_of(result) or api_mod._val(result, "messages", default=[]) or []
            for row in rows:
                sender = api_mod.sender_guid_of(row)
                if sender is not None and str(sender) == me:
                    return True
        except Exception:
            pass
    return False


async def should_respond(ctx) -> tuple[bool, bool]:
    """Return (should_respond, direct_to_bot)."""
    if not ctx.is_group or not ctx.author or str(ctx.author) == str(ctx.me):
        return False, False
    text = ctx.raw_text or ""
    direct = await reply_target_is_bot(ctx)
    greeting = is_greeting(text)
    mention = mentions_bot(text)
    if direct or greeting or mention:
        return True, direct
    if ctx.g.get("talkative"):
        # Talkative mode is intentionally conservative: don't answer obvious commands,
        # URLs, or long floods as conversational messages.
        n = normalize(text)
        if not n or n.startswith(("/", "فعال", "غیرفعال", "قفل ", "سکوت ", "ریپ ", "ری اکت ", "ری‌اکت ")):
            return False, False
        return True, False
    return False, False


def _recent_group_context(guid: str, limit: int = 10) -> list[dict[str, str]]:
    rows = db.data.get("history", {}).get(str(guid), []) or []
    out = []
    for row in rows[-limit:]:
        if row.get("e"):
            continue
        text = str(row.get("text") or row.get("x") or "").strip()
        if not text:
            continue
        uid = str(row.get("u") or "user")
        out.append({"role": "user", "content": f"کاربر {uid}: {text[:500]}"})
    return out


async def generate(ctx, *, direct_to_bot: bool = False) -> str:
    text = (ctx.raw_text or "").strip()
    if not text:
        return reply_for(text)
    if not configured():
        return reply_for(text)

    key = f"{ctx.guid}:{ctx.author or 'unknown'}"
    lock = _user_locks.setdefault(key, asyncio.Lock())
    async with lock:
        memory_root = db.data.setdefault("ai_memory", {})
        history = list(memory_root.setdefault(key, []))[-max(2, config.HF_MEMORY_MESSAGES):]

        messages = [{"role": "system", "content": SYSTEM_PROMPT}]
        if direct_to_bot:
            messages.append({"role": "system", "content": "این پیام مستقیماً در پاسخ به یکی از پیام‌های قبلی FBI BOT است؛ به همان زمینه پاسخ بده."})
        messages.extend(_recent_group_context(ctx.guid, limit=8))
        messages.extend(history)
        messages.append({"role": "user", "content": text[:2500]})

        client = _get_client()
        if client is None and not _has_requests():
            return reply_for(text) if not direct_to_bot else "❌ سرویس هوش مصنوعی در دسترس نیست؛ `huggingface_hub` یا `requests` را نصب کنید."
        try:
            def do_request():
                if client is not None:
                    return client.chat.completions.create(
                        model=config.HF_MODEL,
                        messages=messages,
                        max_tokens=config.HF_MAX_TOKENS,
                        temperature=config.HF_TEMPERATURE,
                    )
                return _direct_router_request(messages)

            result = await asyncio.to_thread(do_request)
            answer = _extract_text(result)
            if not answer:
                return reply_for(text)

            history.extend([
                {"role": "user", "content": text[:2500]},
                {"role": "assistant", "content": answer[:3000]},
            ])
            memory_root[key] = history[-max(4, config.HF_MEMORY_MESSAGES * 2):]
            db.save("ai_memory")
            return answer[:3500]
        except Exception as exc:
            try:
                db.log("ai", f"HF request failed: {type(exc).__name__}: {exc}")
            except Exception:
                pass
            try:
                result = await asyncio.to_thread(_direct_router_request, messages)
                answer = _extract_text(result)
                if answer:
                    history.extend([
                        {"role": "user", "content": text[:2500]},
                        {"role": "assistant", "content": answer[:3000]},
                    ])
                    memory_root[key] = history[-max(4, config.HF_MEMORY_MESSAGES * 2):]
                    db.save("ai_memory")
                    return answer[:3500]
            except Exception as fallback_exc:
                try:
                    db.log("ai", f"HF router fallback failed: {type(fallback_exc).__name__}: {fallback_exc}")
                except Exception:
                    pass
            return reply_for(text) if not direct_to_bot else "❌ اتصال Hugging Face برقرار نشد؛ خطای دقیق در fbi.log ثبت شد."


async def healthcheck() -> tuple[bool, str]:
    """Non-secret HF connectivity check used by tests/logging."""
    if not config.HF_TOKEN:
        return False, "HF token is not configured"
    if not _has_requests() and InferenceClient is None:
        return False, "requests/huggingface_hub is missing"
    try:
        result = await asyncio.to_thread(_direct_router_request, [
            {"role": "system", "content": "Reply with exactly OK."},
            {"role": "user", "content": "OK"},
        ])
        return bool(_extract_text(result)), "HF Router reachable"
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"


async def handle(ctx) -> bool:
    text = ctx.text
    if text in ("هوش مصنوعی روشن", "هوش مصنوعی فعال"):
        if ctx.rank < 1:
            await ctx.err("فقط ادمین می‌تواند هوش مصنوعی را روشن کند.")
            return True
        ctx.g["ai_chat"] = True
        ctx.g["speaker"] = True
        db.save("groups")
        if configured():
            await ctx.ok(f"هوش مصنوعی FBI روشن شد 🤖\nمدل: `{config.HF_MODEL}`\nپاسخ‌گویی رفتاری و Reply فعال است ✅")
        else:
            await ctx.ok("هوش مصنوعی روشن شد، ولی `FBI_HF_TOKEN` تنظیم نشده؛ فعلاً حالت محلی اجرا می‌شود.")
        return True
    if text in ("هوش مصنوعی خاموش", "هوش مصنوعی غیرفعال"):
        if ctx.rank < 1:
            await ctx.err("فقط ادمین می‌تواند هوش مصنوعی را خاموش کند.")
            return True
        ctx.g["ai_chat"] = False
        db.save("groups")
        await ctx.ok("هوش مصنوعی خاموش شد.")
        return True
    return False
