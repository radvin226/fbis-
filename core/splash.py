# -*- coding: utf-8 -*-
"""
اسپلش اسکرین ربات FBI
موقع اجرا (python main.py) یک بنر رنگی و جعبه‌ی وضعیت چاپ می‌کند.
"""

import platform
import sys
from datetime import datetime


# ============================================================
# رنگ‌ها (ANSI) — روی اکثر ترمینال‌ها (لینوکس/مک/ویندوز ۱۰+) کار می‌کند
# ============================================================

class C:
    RESET = "\033[0m"
    BOLD = "\033[1m"
    DIM = "\033[2m"

    RED = "\033[91m"
    GREEN = "\033[92m"
    YELLOW = "\033[93m"
    BLUE = "\033[94m"
    MAGENTA = "\033[95m"
    CYAN = "\033[96m"
    WHITE = "\033[97m"


# بنر ASCII حرف‌های F B I
_BANNER = r"""
 ███████╗██████╗ ██╗
 ██╔════╝██╔══██╗██║
 █████╗  ██████╔╝██║
 ██╔══╝  ██╔══██╗██║
 ██║     ██████╔╝██║
 ╚═╝     ╚═════╝ ╚═╝
"""


def _supports_color() -> bool:
    if sys.platform == "win32":
        # ویندوز ۱۰ به بعد از ANSI پشتیبانی می‌کند؛ برای اطمینان
        # ترمینال‌های قدیمی رنگ را غیرفعال می‌کنیم مگر NO_COLOR ست نشده باشد.
        return True
    return sys.stdout.isatty()


def show(
    bot_name: str,
    maker: str,
    version: str = "1.0.0",
    modules: int = 0,
    session_name: str = "",
    sudos: int = 0,
) -> None:
    """بنر خوشامد + جعبه‌ی وضعیت را در کنسول چاپ می‌کند."""

    colorize = _supports_color()

    def wrap(code: str, text: str) -> str:
        if not colorize:
            return text
        return f"{code}{text}{C.RESET}"

    banner_lines = _BANNER.strip("\n").split("\n")
    for line in banner_lines:
        print(wrap(C.BOLD + C.CYAN, line))

    print(
        wrap(
            C.BOLD + C.MAGENTA,
            f"        {bot_name} — ربات مدیریت گروه سروش‌پلاس",
        )
    )
    print(wrap(C.DIM, f"        ساخته شده توسط {maker} 🌹"))
    print()

    rows = [
        ("نسخه", version),
        ("ماژول‌های بارگذاری‌شده", str(modules)),
        ("سشن", session_name or "—"),
        ("تعداد سازنده‌ها (SUDOS)", str(sudos)),
        ("پایتون", platform.python_version()),
        ("سیستم‌عامل", platform.system()),
        ("زمان اجرا", datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
    ]

    label_w = max(len(r[0]) for r in rows)
    box_w = label_w + 28

    top = "╔" + "═" * box_w + "╗"
    bot = "╚" + "═" * box_w + "╝"

    print(wrap(C.GREEN, top))
    for label, value in rows:
        line = f"║  {label.ljust(label_w)} : {value}"
        line = line.ljust(box_w + 1) + "║"
        print(wrap(C.GREEN, line))
    print(wrap(C.GREEN, bot))
    print()
    print(wrap(C.YELLOW, "  ⏳ در حال اتصال به سروش‌پلاس ..."))
    print()
