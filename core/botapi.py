# -*- coding: utf-8 -*-
"""کلاینت کوچک API رسمی بات سروش‌پلاس (HTTP + JSON).

آدرس هر متد: https://api.splus.ir/bot<token>/METHOD
توکن فقط داخل آدرس درخواست است و هرگز در پیام خطا یا لاگ نوشته نمی‌شود.
"""
from __future__ import annotations

try:  # aiohttp همان کتابخانه‌ای است که وب‌پنل استفاده می‌کند؛ import تنبل تا تست‌ها بدون آن هم اجرا شوند.
    import aiohttp
except ImportError:  # pragma: no cover
    aiohttp = None

API_BASE = "https://api.splus.ir"


class BotApiError(Exception):
    def __init__(self, method, description="", code=None):
        super().__init__(f"{method}: {description}")
        self.method = method
        self.description = description
        self.code = code


class BotApi:
    def __init__(self, token, *, base=API_BASE, session=None, timeout=35):
        self.token = str(token or "").strip()
        self.base = str(base).rstrip("/")
        self._session = session
        self._timeout = timeout

    def _url(self, method):
        return f"{self.base}/bot{self.token}/{method}"

    async def _get_session(self):
        if self._session is None or getattr(self._session, "closed", False):
            if aiohttp is None:
                raise RuntimeError("aiohttp نصب نیست")
            self._session = aiohttp.ClientSession()
        return self._session

    def _timeout_obj(self):
        if aiohttp is None:
            return None
        return aiohttp.ClientTimeout(total=self._timeout)

    async def call(self, method, **params):
        if not self.token:
            raise BotApiError(method, "توکن بات تنظیم نشده است")
        payload = {key: value for key, value in params.items() if value is not None}
        try:
            session = await self._get_session()
            async with session.post(self._url(method), json=payload, timeout=self._timeout_obj()) as resp:
                data = await resp.json(content_type=None)
        except Exception as exc:
            # پیام خطای شبکه ممکن است آدرس (و توکن) را داشته باشد؛ فقط نوع خطا گزارش می‌شود.
            raise BotApiError(method, f"خطای شبکه: {type(exc).__name__}") from None
        if not isinstance(data, dict) or not data.get("ok"):
            if isinstance(data, dict):
                raise BotApiError(method, str(data.get("description") or "خطای ناشناخته"), data.get("error_code"))
            raise BotApiError(method, "پاسخ نامعتبر")
        return data.get("result")

    async def get_me(self):
        return await self.call("getMe")

    async def delete_webhook(self, drop_pending_updates=False):
        return await self.call("deleteWebhook", drop_pending_updates=drop_pending_updates)

    async def get_updates(self, offset=None, timeout=25, limit=100):
        return await self.call(
            "getUpdates", offset=offset, timeout=timeout, limit=limit,
            allowed_updates=["message", "callback_query"],
        )

    async def send_message(self, chat_id, text, reply_markup=None):
        return await self.call("sendMessage", chat_id=chat_id, text=text, reply_markup=reply_markup)

    async def edit_message_text(self, chat_id, message_id, text, reply_markup=None):
        return await self.call(
            "editMessageText", chat_id=chat_id, message_id=message_id, text=text, reply_markup=reply_markup,
        )

    async def answer_callback_query(self, callback_query_id, text=None, show_alert=False):
        return await self.call(
            "answerCallbackQuery", callback_query_id=callback_query_id, text=text, show_alert=show_alert,
        )

    async def close(self):
        session = self._session
        if session is not None and not getattr(session, "closed", True):
            await session.close()
