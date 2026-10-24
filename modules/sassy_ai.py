# -*- coding: utf-8 -*-
"""FBI SPLUS — سخنگوی داخلی و قانون‌محور (بدون سرویس خارجی).

پیام‌ها با واژه‌نامهٔ داخلی تطبیق داده می‌شوند و بر اساس «قصد» پیام، پاسخ مناسب
انتخاب می‌شود. برای گفت‌وگو نیازی به دستور نیست:
- اگر کسی به پیام FBI ریپلای کند، FBI را صدا بزند، یا جمله‌ای با قصد شناخته‌شده بنویسد، پاسخ می‌گیرد.
- پیام‌های بی‌ربط و بدون خطاب مستقیم بی‌جواب می‌مانند تا گروه شلوغ نشود.
- پاسخ‌ها تکراری نمی‌شوند و به زمینهٔ گفت‌وگو توجه می‌کنند (مثلاً «آره» بعد از یک سؤال).
- هیچ درخواست شبکه‌ای انجام نمی‌شود و هیچ متن «هوش مصنوعی» در پاسخ‌ها نیست.
"""
from __future__ import annotations

import asyncio
import ast
import operator
import random
import re
import time
from dataclasses import dataclass

import config
from core import api as api_mod
from core import utils


# ------------------------------------------------------------------ نرمال‌سازی
_DIACRITICS = re.compile("[\u064b-\u065f\u0670\u0640]")
_EMOJI = re.compile("[\U0001F000-\U0001FAFF\U00002600-\U000027BF\U0000FE00-\U0000FE0F\U0000200D]")
_SYMBOLS = re.compile(r"[!\"#$%&'()*+,\-./:;<=>?@\[\\\]^_`{|}~«»…،؛؟٪×÷“”‘’]")
_REPEAT = re.compile(r"(.)\1+")


def normalize(text: str) -> str:
    """یکدست‌سازی برای تطبیق: حروف عربی، نیم‌فاصله، اعراب، ایموجی، علامت و تکرار حروف."""
    t = utils.normalize(text or "").lower()
    t = _DIACRITICS.sub("", t).replace("ة", "ه")
    t = _EMOJI.sub(" ", t)
    t = _SYMBOLS.sub(" ", t)
    t = _REPEAT.sub(r"\1", t)
    return " ".join(t.split())


# ------------------------------------------------------------------ قصدها
@dataclass(frozen=True)
class Intent:
    name: str
    phrases: tuple
    replies: tuple = ()
    kind: str = "text"          # text | followup | time | date | joke | crisis
    sub: str = ""               # برای followup: yes | no | why | ok
    max_words: int = 0          # بدون خطاب مستقیم، فقط اگر پیام کوتاه‌تر از این باشد
    direct_only: bool = False   # فقط وقتی FBI خطاب شده یا ریپلای شده


INTENTS: tuple = (
    # ---------------------------------------------------------- سلام و احوال‌پرسی
    Intent("morning", ("صبح بخیر", "صبحتون بخیر", "صبح شما بخیر", "صبح به خیر", "صبحت بخیر",
                       "صبح بخیر دوستان", "good morning"),
           ("صبح بخیر ☀️", "صبح به خیر، روز خوبی داشته باشی 🌞", "صبحتون بخیر، امروز قشنگ شروع بشه 🌸"),
           max_words=5),
    Intent("noon", ("ظهر بخیر", "ظهرتون بخیر", "ظهر به خیر", "ظهرت بخیر"),
           ("ظهر بخیر 🌤", "ظهرت بخیر، ناهار خوردی؟ 🍲"), max_words=5),
    Intent("evening", ("عصر بخیر", "عصرتون بخیر", "عصر به خیر", "عصرت بخیر"),
           ("عصر بخیر 🌇", "عصرت بخیر، روز خوبی بوده؟ ☕"), max_words=5),
    Intent("night", ("شب بخیر", "شب به خیر", "شبتون بخیر", "شبت بخیر", "شب خوش", "good night"),
           ("شب بخیر 🌙", "شب خوش، خواب خوبی داشته باشی ⭐", "شبت بخیر، فردا روز بهتری خواهد بود 😴"),
           max_words=5),
    Intent("greet", ("سلام", "سلم", "سلام علیک", "علیک سلام", "سلام علیکم", "سلام و علیکم",
                     "سلام به همه", "سلام به همگی", "سلام بر همه", "درود", "درود بر شما", "درود بر همه",
                     "هلو", "hi", "hello", "hey", "salam"),
           ("سلام 🌹", "سلام، خوش اومدی ☘️", "علیک سلام 😄", "درود بر شما 🌱",
            "سلام به روی ماهت 🌞", "به به، سلام رفیق 😎", "سلام، چطوری؟ 👋"),
           max_words=6),
    Intent("farewell", ("خداحافظ", "خدا حافظ", "خدافظ", "بای", "بای بای", "bye", "بدرود",
                        "فعلا", "به امید دیدار", "من برم", "باید برم", "میرم دیگه", "گود بای"),
           ("خدانگهدار 🌹", "به سلامت ☘️", "فعلاً، مراقب خودت باش 👋", "به امید دیدار 🌱",
            "برو به سلامت، مواظب خودت باش 🤍", "بای بای 😊"),
           max_words=6),
    Intent("thanks", ("ممنون", "ممنونم", "مرسی", "مرسی ازت", "مرسی از تو", "ممنون ازت", "ممنون از تو",
                      "ممنونم ازت", "تشکر", "متشکرم", "سپاس", "سپاسگزارم", "دمت گرم", "دستت درد نکنه",
                      "دستتون درد نکنه", "قربانت", "تشکر میکنم", "thanks", "thank you"),
           ("خواهش می‌کنم 🌱", "قابلی نداشت ❤️", "قربانت 😎", "خوشحالم که کمکی کردم 🌹",
            "نوش جان 🙏", "خواهش، همیشه در خدمتم ☘️", "مرسی از تو که هستی 😊"),
           max_words=6),
    Intent("sorry", ("ببخشید", "ببخشین", "معذرت", "معذرت میخوام", "متاسفم", "sorry"),
           ("اشکالی نداره 🙂", "خواهش می‌کنم، پیش میاد 🌱", "عیبی نداره، رد شو 😊",
            "اشکالی نیست، خوبی؟"),
           max_words=6),
    Intent("how", ("خوبی", "خوبین", "خوبید", "چطوری", "چطورید", "چطورین", "حالت چطوره",
                   "حال شما چطوره", "حالتون چطوره", "حالت خوبه", "احوالت", "احوال شما", "چه خبر",
                   "چه خبرا", "چخبر", "خبر چیه", "خوب هستی", "خوب هستید", "تو خوبی", "شما خوبید",
                   "خوبی تو", "how are you"),
           ("من که سرحالم 😎 تو چطوری؟", "آنلاینم و آماده‌ام 😂 تو خوبی؟",
            "خوبم، ممنون که پرسیدی 🌹 تو چطوری؟", "بد نیستم 🙂 تو چه خبر؟",
            "عالی‌ام، تو خوبی؟ ☘️", "هستم و حواسم به گروه هست 👀 تو چی؟"),
           max_words=5),

    # ---------------------------------------------------------- هویت و سازنده
    Intent("identity", ("تو کی هستی", "کی هستی", "تو کیستی", "اسمت چیه", "اسمت چیست", "اسم تو چیه",
                        "اسمتو بگو", "اسمت چی", "چی هستی", "تو چی هستی", "چیکاره ای", "چیکارهای",
                        "چیکاره هستی", "اسم داری"),
           ("من FBI هستم؛ ربات مدیریت گروه 🕵️", "اسمم FBI هست؛ نگهبان گروهتون 🕵️",
            "من FBI هستم، هم مدیر گروهم و هم همراهت 😎"),
           max_words=6),
    Intent("identity_bot", ("آدمی", "انسانی", "تو آدمی", "تو انسانی", "انسان هستی", "تو انسان هستی",
                            "واقعی هستی", "ربات هستی", "تو ربات هستی", "تو یه ربات هستی",
                            "تو یک ربات هستی", "بات هستی", "تو بات هستی"),
           ("نه، من یه ربات هستم 🤖 ولی بد نیستم، قول میدم 😄", "درست حدس زدی، ربات هستم 🤖",
            "آره، ربات مدیریت گروه هستم 🤖"),
           max_words=5, direct_only=True),
    Intent("creator", ("کی ساختت", "کی ساخته", "کی تو رو ساخته", "کی درستت کرده", "کی درستت کرد",
                       "سازندت کیه", "سازندت کیست", "سازنده ات کیه", "سازندهات کیه", "سازنده کیه",
                       "سازنده کیست", "کی برنامه نویست", "کی برنامهنویست", "کی برنامهنویستت",
                       "کی نوشتت", "کی طراحت کرده", "رادوین کیه", "مالکت کیه"),
           ("من توسط {maker} ساخته شدم 🌹", "سازنده‌ام {maker} است 🌹",
            "{maker} منو ساخته؛ افتخار می‌کنم 😎"),
           max_words=6),
    Intent("age", ("چند سالته", "چندسالته", "چند سالت", "سنت چنده", "سنت چقدره", "سنت چقدر"),
           ("من سن ندارم 😄 ولی همیشه جوونم!", "سن که ندارم، ولی سرعتم بالاست ⚡",
            "توی دنیای ربات‌ها هنوز بچه‌ام 😂"),
           max_words=5),
    Intent("location", ("کجایی", "کجا هستی", "کجایید", "کجایین", "کجا زندگی میکنی",
                        "اهل کجایی", "کجا بودی", "کجا میمونی"),
           ("همین‌جا توی گروهم 😎", "گوشه‌ی گروه نشستم و حواسم بهتونه 👀",
            "همین نزدیکی‌ها، توی همین گروه 🕵️"),
           max_words=5),

    # ---------------------------------------------------------- راهنمایی و ابزارها
    Intent("abilities", ("چیکار میکنی", "چه کاری میکنی", "چه کار میکنی", "چه کارایی میکنی",
                         "چه کارهایی میکنی", "چیکار بلدی", "چه کاری بلدی", "چی بلدی",
                         "چه کاری انجام میدی", "کمکم میکنی", "کمک میکنی", "چه دستوری داری",
                         "دستوراتت", "دستورهات", "دستورات", "دستورها", "راهنمایی کن", "راهنما بده"),
           ("گروه رو مدیریت می‌کنم، آمار می‌گیرم، سرگرمی دارم و کنارتم 😎 برای دیدن همه دستورها «راهنما» رو بفرست 📖",
            "کارهای زیادی بلدم؛ مدیریت، قفل، آمار و سرگرمی 🕵️ «راهنما» رو بفرست تا همه‌شون رو ببینی 📖"),
           max_words=6),
    Intent("time", ("ساعت چنده", "ساعت چند", "ساعت چنده الان", "الان ساعت چنده", "ساعت الان چنده",
                    "ساعت چنده امروز", "الان ساعت چند است"),
           kind="time", max_words=5),
    Intent("date", ("امروز چندمه", "امروز چند مه", "امروز چه تاریخیه", "تاریخ امروز", "امروز چه روزیه",
                    "تاریخ چنده", "امروز چندشنبه", "امروز چه روزی", "چندمه امروز", "امروز چه روزی هست"),
           kind="date", max_words=6),
    Intent("weather", ("هوا چطوره", "هوا چجوریه", "هوا چطوریه", "آب و هوا", "آب و هوا چطوره",
                       "بارون میاد", "باران میاد", "برف میاد"),
           ("برای هوای لحظه‌ای باید از یه سایت یا اپ هواشناسی بپرسی 🌤 ولی امیدوارم هوای دلت خوب باشه 🙂",
            "هوا رو دقیق نمی‌تونم چک کنم ☂️ اگه بارونه، یه چتر همراهت باشه 😄"),
           max_words=5),
    Intent("price", ("قیمت دلار", "دلار چنده", "قیمت دلار چنده", "دلار امروز", "دلار امروز چنده",
                     "قیمت طلا", "طلا چنده", "قیمت طلا چنده", "قیمت سکه", "سکه چنده", "قیمت سکه چنده",
                     "قیمت بیت کوین", "بیت کوین چنده", "قیمت ارز", "قیمت ارز دیجیتال", "قیمت ارزدیجیتال"),
           ("برای قیمت لحظه‌ای «قیمت ارز» یا «قیمت طلا» رو بفرست 💵",
            "قیمت ارز، طلا و ارز دیجیتال رو با دستورهای «قیمت ارز»، «قیمت طلا» و «ارزدیجیتال» می‌گیری 🥇"),
           max_words=5),

    # ---------------------------------------------------------- سرگرمی و حال‌وهوا
    Intent("joke", ("جوک بگو", "یه جوک بگو", "یک جوک بگو", "جوک", "یه جوک", "بخندونم", "منو بخندون",
                    "یه لطیفه بگو", "لطیفه بگو", "لطیفه", "یه لطیفه"),
           kind="joke", max_words=5),
    Intent("bored", ("بیکارم", "حوصلم سر رفته", "حوصلم سر رفت", "حوصلم سرفته", "حوصله ندارم",
                     "کسی نیست", "هیچکس نیست", "خیلی خستم", "خستهام", "خسته ام", "حوصله‌ام سر رفته"),
           ("حوصله‌ت سر رفته؟ بیا یه بازی کنیم: «بازی کلمات» یا «بازی ریاضی» 🎮",
            "اگه حوصله داری، «لطیفه» یا «فال حافظ» رو بفرست 😄",
            "یه چالش بده خودت: «چالش» رو بفرست و ببین چی میشه 😎",
            "استراحت هم جزو کاره؛ یه چای بخور و برگرد ☕"),
           max_words=6),
    Intent("sad", ("ناراحتم", "غمگینم", "غمگین هستم", "حالم بده", "حالم خوب نیست", "حالم خوش نیست",
                   "افسرده‌ام", "افسرده ام", "افسردم", "دلم گرفته", "دلم گرفتهست", "تنهام", "تنهایی",
                   "گریه‌ام گرفته", "گریه میکنم", "بدبختم", "خیلی بدم"),
           ("ناراحتیت رو می‌فهمم 🤍 اگه دوست داری حرف بزن، گوش می‌دم.",
            "امیدوارم زود بهتر بشی 🌱 گاهی فقط حرف زدن کمک می‌کنه.",
            "حالت که خوب نیست، کنارتیم 🤍 مواظب خودت باش."),
           max_words=10),
    Intent("happy", ("خوشحالم", "خیلی خوشحالم", "خوبم", "خیلی خوبم", "عالیم", "حالم خوبه",
                     "حالم خوب است", "امروز خوبم", "خوشحال هستم", "خیلی خوشحال هستم"),
           ("چه خوب! این حس خوب رو نگه دار 🌞", "خوشحالم که خوبی 😊", "عالیه، ادامه بده 🌹",
            "قشنگه، امیدوارم روزت همین‌طور پر از خوبی باشه ☘️"),
           max_words=6),
    Intent("compliment", ("باحالی", "خیلی باحالی", "تو باحالی", "عالی هستی", "تو عالی هستی",
                          "عالی بودی", "باهوشی", "خیلی باهوشی", "تو باهوشی", "خوشگلی", "آفرین",
                          "افرین", "عاشقتم", "دوستت دارم", "تو بهترینی", "خیلی خوبی", "تو خیلی خوبی",
                          "تو ماهی"),
           ("ممنون 😊 تو هم عالی هستی!", "لطف داری 🌹", "قربانت، تو هم خیلی باحالی 😎",
            "ممنونم ❤️ منم این گروه رو دوست دارم 🌱", "آفرین به خودت که اینجایی ☘️"),
           max_words=6),
    Intent("insult", ("احمق", "خنگ", "عوضی", "گمشو", "برو گمشو", "خفه شو", "بی شعور", "بیشعور",
                      "الاغ", "خرف", "چرت", "مزخرف", "دیوانه", "کودن", "نفهم", "گاو"),
           ("با احترام حرف بزن، لطفاً 🙂", "این حرف‌ها به گروه کمکی نمی‌کنه 🌱",
            "باشه، ولی بیا محترمانه حرف بزنیم 🙏", "من همیشه محترمانه باهات حرف می‌زنم؛ تو هم همین‌طور 😊"),
           direct_only=True),
    Intent("crisis", ("خودکشی", "خودکشی کنم", "میخوام خودمو بکشم", "خودمو بکشم", "میخوام بمیرم",
                      "دیگه نمیخوام زنده باشم", "زندگی نمیخوام", "خودزنی"),
           kind="crisis"),

    # ---------------------------------------------------------- پاسخ‌های کوتاه (فقط با زمینه)
    Intent("fu_yes", ("آره", "اره", "بله", "اوهوم", "دقیقا", "درسته", "صحیح"), kind="followup", sub="yes"),
    Intent("fu_no", ("نه", "نخیر", "نچ", "نیست", "نمیخوام", "نمیخواد"), kind="followup", sub="no"),
    Intent("fu_why", ("چرا", "چطور", "چجوری", "چی", "چیه", "واقعا", "جدی", "یعنی چی", "یعنی", "چه"),
           kind="followup", sub="why"),
    Intent("fu_ok", ("اوکی", "باشه", "ok", "okay", "oke", "خب"), kind="followup", sub="ok"),
)

# پاسخ‌های مخصوص پاسخ کوتاه، بر اساس قصد پیام قبلی
FOLLOWUP_REPLIES = {
    "how": {
        "yes": ("خوبه، خوشحالم 😊", "عالیه، همین حس خوب رو نگه دار 🌱"),
        "no": ("ناراحتم که حالت خوب نیست 🤍 چی شده؟",),
        "why": ("همین‌طوری پرسیدم، ولی دوست دارم بدونم حالت چطوره 🙂",),
        "ok": ("👍", "عالیه 🙂"),
    },
    "joke": {
        "yes": ("پس یکی دیگه؟ 😄 «لطیفه» رو بفرست",),
        "no": ("باشه، هر وقت خواستی بگو 🙂",),
        "why": ("چون گفتی بخندونمت 😂",),
        "ok": ("👍 بخند!",),
    },
    "bored": {
        "yes": ("پس بیا یه بازی کنیم: «بازی کلمات» 🎮",),
        "no": ("باشه، پس یه کم استراحت کن ☕",),
        "why": ("یه بازی کوچیک می‌تونه حالت رو عوض کنه 🎲",),
        "ok": ("👍 پس «بازی ریاضی» رو بفرست 🎮",),
    },
    "sad": {
        "yes": ("خوبه که حرف می‌زنی 🤍 بگو چی شده؟",),
        "no": ("باشه، هر وقت خواستی حرف بزن 🌱",),
        "why": ("گاهی دلیلش مشخص نیست؛ فقط بدون که کنارتیم 🤍",),
        "ok": ("باشه 🤍 مراقب خودت باش.",),
    },
}
FOLLOWUP_DEFAULT = {
    "yes": ("عالیه 👍", "خوبه 🙂"),
    "no": ("باشه 🙂", "اشکالی نداره 🌱"),
    "why": ("سوالت رو کامل‌تر بگو تا بهتر جواب بدم 🙂",),
    "ok": ("👍", "عالیه 🙂"),
}

CRISIS_REPLY = (
    "ناراحتم که این‌قدر سخته 🤍 لطفاً همین الان با یکی از عزیزانت یا یک فرد قابل اعتماد حرف بزن "
    "و تنها نمون. اگه احساس خطر فوری داری، با اورژانس ۱۱۵ تماس بگیر یا سریع خودت رو "
    "به نزدیک‌ترین مرکز درمانی برسون."
)

JOKES = (
    "چرا کتاب ریاضی ناراحت بود؟ چون مسئله‌های زیادی داشت 📚😂",
    "معلم پرسید: چرا دیر اومدی؟ گفتم: قربان، داشتم ساعتم رو تنظیم می‌کردم 😅",
    "چرا ماهی هیچ‌وقت دروغ نمی‌گه؟ چون همیشه تو آب‌ها شفافه 🐠",
    "یه نفر به دوستش گفت: من هیچ‌وقت دروغ نمی‌گم. دوستش گفت: خب الان دروغ گفتی 😂",
    "به دوستم گفتم یه جوک بگو؛ گفت: من فقط توی گروه‌ها خنده‌دارم، توی زندگی نه 😄",
    "یه روز یه گوشی رفت دکتر، گفت حالم گرفته‌ست. دکتر گفت: یه کم شارژ بگیر، خوب می‌شی 🔋😂",
)

QUESTION_FALLBACK = (
    "سوالت رو کامل‌تر بگو تا بهتر جوابت رو بدم 🙂",
    "هنوز متوجه نشدم؛ یه کم واضح‌تر بپرس 🤔",
    "این یکی رو نمی‌دونم، ولی می‌تونی «راهنما» رو بفرستی 📖",
)
GENERIC_FALLBACK = (
    "جانم؟ 👂",
    "بگو ببینم، گوشم با توئه 😄",
    "یه کم واضح‌تر بگو که بفهمم 🙂",
    "متوجه نشدم؛ برای دیدن دستورها «راهنما» رو بفرست 📖",
    "من حواسم هست، ادامه بده 🫡",
)
NAME_ONLY_REPLIES = ("جانم؟ 👂", "بله، در خدمتم ⚡", "بگو، گوشم با توئه 😄", "جانم، بفرما 🫡")
SASSY_TAILS = (" 😏", " 🙄", " 😂", " 😎")
SASSY_INTENTS = {"morning", "noon", "evening", "night", "greet", "how", "thanks", "compliment", "identity"}

MENTION_PHRASES = tuple(normalize(x) for x in (
    "ربات", "بات", "fbi", "اف بی آی", "اف بی ای", "افبیآی", "افبیای", "اف بی",
))


# ------------------------------------------------------------------ ایندکس و تطبیق
_INDEX: list = []
for _it in INTENTS:
    for _p in _it.phrases:
        _np = normalize(_p)
        if _np:
            _INDEX.append((_np, _it))

_CONTEXT: dict = {}          # chat -> (intent name, time)
_LAST_PICK: dict = {}        # (chat, key) -> index
_LAST_UNSOLICITED: dict = {}  # chat -> time


def _find_intent(n: str):
    padded = f" {n} "
    words = len(n.split())
    best, best_key = None, (0, 0)
    for phrase, it in _INDEX:
        if it.kind == "followup" and words > 2:
            continue
        if f" {phrase} " not in padded:
            continue
        key = (len(phrase.split()), len(phrase))
        if key > best_key:
            best, best_key = it, key
    return best


def mentions_bot(text: str) -> bool:
    padded = f" {normalize(text)} "
    return any(f" {p} " in padded for p in MENTION_PHRASES)


def _pick(chat: str, key: str, options) -> str | None:
    options = tuple(options)
    if not options:
        return None
    if len(_LAST_PICK) > 10000:
        _LAST_PICK.clear()
    last = _LAST_PICK.get((chat, key))
    choices = [i for i in range(len(options)) if i != last] or [0]
    idx = random.choice(choices)
    _LAST_PICK[(chat, key)] = idx
    return options[idx].replace("{maker}", str(config.MAKER))


# ------------------------------------------------------------------ محاسبه ساده
_MATH_HINT = re.compile(r"حاصل|چند میشه|چقدر میشه|چنده|چقدره|=|\?|؟")
_MATH_WORDS = re.compile(r"حاصل|چقدر می ?شه|چند می ?شه|چقدره|چنده")
_OPS = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul, ast.Div: operator.truediv}


def _eval_node(node):
    if isinstance(node, ast.Expression):
        return _eval_node(node.body)
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)) and not isinstance(node.value, bool):
        return node.value
    if isinstance(node, ast.BinOp) and type(node.op) in _OPS:
        return _OPS[type(node.op)](_eval_node(node.left), _eval_node(node.right))
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
        value = _eval_node(node.operand)
        return -value if isinstance(node.op, ast.USub) else value
    raise ValueError("unsupported expression")


def _math_reply(raw: str) -> str | None:
    expr = utils.en_digits(raw).replace("×", "*").replace("÷", "/")
    expr = re.sub(r"[xX]", "*", expr)
    expr = _MATH_WORDS.sub(" ", expr).replace("=", " ").replace("؟", " ").replace("?", " ")
    expr = " ".join(expr.split())
    if not re.fullmatch(r"[0-9+\-*/(). ]{3,40}", expr):
        return None
    if not re.search(r"\d\s*[-+*/]\s*\(?\s*[\d(-]", expr):
        return None
    try:
        value = _eval_node(ast.parse(expr, mode="eval"))
    except ZeroDivisionError:
        return "نمیشه بر صفر تقسیم کرد 🙃"
    except Exception:
        return None
    if isinstance(value, float):
        value = int(value) if value.is_integer() else round(value, 6)
    return f"🧮 حاصل: {utils.fa_digits(f'{value:,}')}"


# ------------------------------------------------------------------ پاسخ‌گویی
def _cooldown_ready(ctx, chat: str, now: float) -> bool:
    cooldown = float(getattr(config, "SPEAKER_COOLDOWN_SECONDS", 8))
    if ctx.g.get("talkative"):
        cooldown = min(cooldown, 3.0)
    return now - _LAST_UNSOLICITED.get(chat, 0.0) >= cooldown


def _compose(chat: str, intent: Intent, now: float):
    if intent.kind == "followup":
        prev = _CONTEXT.get(chat)
        if not prev or now - prev[1] > 120:
            return None
        options = FOLLOWUP_REPLIES.get(prev[0], {}).get(intent.sub) or FOLLOWUP_DEFAULT.get(intent.sub, ())
        return _pick(chat, f"fu:{prev[0]}:{intent.sub}", options)
    if intent.kind == "time":
        return utils.clock_text()
    if intent.kind == "date":
        return utils.date_text()
    if intent.kind == "joke":
        return _pick(chat, "joke", JOKES)
    return _pick(chat, intent.name, intent.replies)


def _fallback(raw: str, chat: str) -> str | None:
    if "؟" in raw or "?" in raw:
        return _pick(chat, "fallback_q", QUESTION_FALLBACK)
    return _pick(chat, "fallback", GENERIC_FALLBACK)


async def reply_target_is_bot(ctx) -> bool:
    """آیا پیام فعلی به یکی از پیام‌های FBI ریپلای شده است؟"""
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


async def answer(ctx) -> str | None:
    """پاسخ به پیام گروهی؛ اگر پاسخی لازم نیست None برمی‌گرداند."""
    raw = (ctx.raw_text or "").strip()
    if not raw or not ctx.is_group or not ctx.author or str(ctx.author) == str(ctx.me):
        return None
    n = normalize(raw)
    if not n:
        return None
    words = n.split()
    chat = str(ctx.guid)
    now = time.time()

    direct = await reply_target_is_bot(ctx)
    mention = mentions_bot(n)
    # نام FBI فقط در پیام‌های کوتاه به معنای خطاب است؛ در جمله‌های بلند فقط ریپلای خطاب حساب می‌شود.
    addressed = direct or (mention and len(words) <= 6)

    intent = _find_intent(n)
    if intent is not None and intent.kind == "crisis":
        return CRISIS_REPLY

    if addressed or _MATH_HINT.search(raw):
        math_text = _math_reply(raw)
        if math_text:
            return math_text

    # «گفت‌وگوی بدون دستور» (سخنگوی شخصی) خاموش باشد، فقط وقتی خطاب شویم جواب می‌دهیم.
    if not addressed and not ctx.g.get("ai_chat", True):
        return None
    if intent is not None and not addressed:
        if intent.direct_only or (intent.max_words and len(words) > intent.max_words):
            intent = None
    if intent is None and not addressed:
        return None
    # پاسخ کوتاهِ کاربر به سؤال خودِ FBI بخشی از گفت‌وگوست و از فاصلهٔ ضدّاسپم معاف است.
    is_followup = intent is not None and intent.kind == "followup"
    if not addressed and not is_followup and not _cooldown_ready(ctx, chat, now):
        return None

    reply = _compose(chat, intent, now) if intent is not None else None
    if reply is None:
        if not addressed:
            return None
        if mention and intent is None and len(words) <= 2:
            reply = _pick(chat, "name_only", NAME_ONLY_REPLIES)
        else:
            reply = _fallback(raw, chat)
    if not addressed:
        _LAST_UNSOLICITED[chat] = now
    if intent is not None and intent.kind != "followup":
        if len(_CONTEXT) > 5000:
            _CONTEXT.clear()
        _CONTEXT[chat] = (intent.name, now)
    if reply and ctx.g.get("sassy") and (intent is None or intent.name in SASSY_INTENTS) and random.random() < 0.4:
        reply += random.choice(SASSY_TAILS)
    return reply

