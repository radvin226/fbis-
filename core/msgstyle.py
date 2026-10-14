# -*- coding: utf-8 -*-
"""قالب یکدست پیام‌های FBI.

هر گروه می‌تواند بین دو حالت انتخاب کند:
- «کارتی» (پیش‌فرض): پیام داخل قاب با عنوان پررنگ و نوار کناری.
- «ساده»: همان شکل قبلی پیام‌ها، بدون قاب.

این ماژول فقط متن را می‌سازد؛ ارسال در core/api.py و دستورها در modules/beauty.py است.
پیام‌های خصوصی (PV) همیشه کارتی هستند چون تنظیم گروه را ندارند.
"""
from __future__ import annotations

CARD = "card"
PLAIN = "plain"
DEFAULT = CARD

MODES = {"کارتی": CARD, "ساده": PLAIN}
MODE_LABEL = {CARD: "کارتی", PLAIN: "ساده"}

BAR = "┃"
FOOT = "╰────────────────────╯"
STATUS_TITLE = {"ok": "انجام شد", "err": "خطا"}
STATUS_ICON = {"ok": "✅", "err": "❌"}


def mode_of(g) -> str:
    """حالت نمایش یک گروه؛ برای دیکشنری خالی (PV) همان حالت پیش‌فرض است."""
    value = g.get("msg_style") if isinstance(g, dict) else None
    return value if value in (CARD, PLAIN) else DEFAULT


def is_card(g) -> bool:
    return mode_of(g) == CARD


def card(title, body="", icon=""):
    """قاب زیبا: عنوان پررنگ بالا و هر خط متن پشت یک نوار کناری."""
    head = f"{icon} " if icon else ""
    lines = [f"╭─「 {head}**{title}** 」"]
    for raw in str(body or "").splitlines():
        line = raw.rstrip()
        lines.append(f"{BAR} {line}" if line else BAR)
    lines.append(FOOT)
    return "\n".join(lines)


def status(g, kind, text):
    """تأیید («ok») یا خطا («err») با سبک فعلی گروه."""
    if is_card(g):
        return card(STATUS_TITLE[kind], text, STATUS_ICON[kind])
    return f"{STATUS_ICON[kind]} {text}"


def block(g, title, body, *, icon="", plain_title=True):
    """پیام‌های اعلانی مثل خوش‌آمد، خداحافظی و فعال‌سازی با سبک گروه."""
    if is_card(g):
        return card(title, body, icon)
    body = str(body)
    return f"{title}\n{body}" if plain_title and title else body


def quote_header(author=None) -> str:
    return f"💬 نقل قول از {author}" if author else "💬 نقل قول"


def spoiler_prefix(author=None) -> str:
    who = f" از {author}" if author else ""
    return f"🔒 پیام مخفی{who} — روی متن بزنید تا ببینید\n"


def clip(text, limit: int) -> str:
    text = str(text or "").strip()
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"
