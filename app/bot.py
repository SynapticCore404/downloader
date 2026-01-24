from __future__ import annotations

import asyncio
import hashlib
import logging
import os
from typing import Optional

from aiogram import Bot, Dispatcher, F
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import TelegramAPIServer
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode, ChatAction, MessageEntityType
from aiogram.filters import CommandStart
from aiogram.types import (
    CallbackQuery,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from .cache import TTLCache
from .config import get_settings
from .yt_helper import ProbeResult, download, find_cached_file, probe, find_cached_audio_file, download_audio


settings = get_settings()
logger = logging.getLogger("bot")
logging.basicConfig(level=getattr(logging, settings.log_level.upper(), logging.INFO))

cache = TTLCache(default_ttl=3600)
_download_sem = asyncio.Semaphore(settings.max_concurrent_downloads)


def _make_bot() -> Bot:
    server = TelegramAPIServer.from_base(settings.api_base_url)
    session = AiohttpSession(timeout=60)
    return Bot(
        token=settings.bot_token,
        session=session,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
        server=server,
    )


def _extract_url(message: Message) -> Optional[str]:
    candidates: list[tuple[Optional[str], Optional[list]]]=[
        (message.text, message.entities),
        (message.caption, message.caption_entities),
    ]
    for text, entities in candidates:
        if not text:
            continue
        if entities:
            for e in entities:
                if e.type == MessageEntityType.TEXT_LINK and getattr(e, "url", None):
                    return e.url
                if e.type == MessageEntityType.URL:
                    start = e.offset
                    end = start + e.length
                    return text[start:end]
        if text.startswith("http://") or text.startswith("https://"):
            return text.strip()
    return None


def _key_for(url: str) -> str:
    return hashlib.sha1(url.encode("utf-8")).hexdigest()[:16]


def _is_youtube(url: str) -> bool:
    u = url.lower()
    return ("youtube.com" in u) or ("youtu.be" in u)

def _quality_keyboard(pr: ProbeResult) -> InlineKeyboardMarkup:
    rows = []
    for opt in pr.options:
        key = _key_for(pr.url)
        cache.set(f"url:{key}", pr.url)
        cache.set(f"vid:{key}", pr.id)
        data = f"d|{key}|{opt.height}"
        label = f"{opt.label}"
        rows.append([InlineKeyboardButton(text=label, callback_data=data)])
    if _is_youtube(pr.url):
        key = _key_for(pr.url)
        cache.set(f"url:{key}", pr.url)
        cache.set(f"vid:{key}", pr.id)
        rows.append([InlineKeyboardButton(text="Audio", callback_data=f"a|{key}|mp3")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _send_media(bot: Bot, chat_id: int, filepath: str, caption: Optional[str]) -> Message:
    force_document = settings.force_document
    ext = os.path.splitext(filepath)[1].lower().lstrip(".")
    as_video = (ext in {"mp4", "mov", "mkv", "webm"}) and not force_document
    if as_video:
        return await bot.send_video(chat_id, video=FSInputFile(filepath), caption=caption, supports_streaming=True)
    return await bot.send_document(chat_id, document=FSInputFile(filepath), caption=caption)


def setup_dispatcher(dp: Dispatcher) -> None:
    @dp.message(CommandStart())
    async def on_start(message: Message) -> None:
        await message.answer("YouTube yoki Instagram havolasini yuboring. Mavjud sifatlarni chiqaraman.")

    @dp.message(F.text)
    async def on_text(message: Message) -> None:
        url = _extract_url(message)
        if not url:
            await message.answer("Iltimos, to‘g‘ri YouTube yoki Instagram havolasini yuboring.")
            return
        try:
            await message.bot.send_chat_action(chat_id=message.chat.id, action=ChatAction.TYPING)
        except Exception:
            logger.debug("chat action failed", exc_info=True)
        try:
            pr = await probe(url, settings.download_dir, settings.cookies_file)
        except Exception as e:
            logger.exception("probe failed")
            await message.answer("Video ma’lumotlarini o‘qib bo‘lmadi. Havola noto‘g‘ri yoki xususiy bo‘lishi mumkin.")
            return
        if not pr.options:
            await message.answer("Bu havola uchun yuklab olinadigan formatlar topilmadi.")
            return
        kb = _quality_keyboard(pr)
        caption = f"<b>{pr.title}</b>\nSifatni tanlang:"
        await message.answer(caption, reply_markup=kb)

    @dp.message(F.caption)
    async def on_caption(message: Message) -> None:
        url = _extract_url(message)
        if not url:
            await message.answer("Iltimos, to‘g‘ri YouTube yoki Instagram havolasini yuboring.")
            return
        try:
            await message.bot.send_chat_action(chat_id=message.chat.id, action=ChatAction.TYPING)
        except Exception:
            logger.debug("chat action failed", exc_info=True)
        try:
            pr = await probe(url, settings.download_dir, settings.cookies_file)
        except Exception as e:
            logger.exception("probe failed")
            await message.answer("Video ma’lumotlarini o‘qib bo‘lmadi. Havola noto‘g‘ri yoki xususiy bo‘lishi mumkin.")
            return
        if not pr.options:
            await message.answer("Bu havola uchun yuklab olinadigan formatlar topilmadi.")
            return
        kb = _quality_keyboard(pr)
        caption = f"<b>{pr.title}</b>\nSifatni tanlang:"
        await message.answer(caption, reply_markup=kb)

    @dp.callback_query(F.data.startswith("d|"))
    async def on_download(cb: CallbackQuery) -> None:
        if not cb.message:
            await cb.answer("Qo‘llab-quvvatlanmaydigan kontekst", show_alert=True)
            return
        try:
            _, key, h = cb.data.split("|", 2)
            height = int(h)
        except Exception:
            await cb.answer("Noto‘g‘ri tanlov", show_alert=True)
            return
        url = cache.get(f"url:{key}")
        vid = cache.get(f"vid:{key}")
        if not url or not vid:
            await cb.answer("Tanlov muddati tugagan. Havolani qayta yuboring.", show_alert=True)
            return
        await cb.answer("Qabul qilindi", show_alert=False)

        try:
            await cb.message.delete()
        except Exception:
            logger.debug("failed to delete selection message", exc_info=True)
        progress_msg = None
        try:
            progress_msg = await cb.message.bot.send_message(cb.message.chat.id, "Video yuklanmoqda…")
        except Exception:
            logger.debug("failed to send progress message", exc_info=True)
        cached = find_cached_file(settings.download_dir, vid, height)
        if cached and os.path.exists(cached):
            try:
                await _send_media(cb.message.bot, cb.message.chat.id, cached, None)
            except Exception:
                logger.exception("send cached failed")
                await cb.message.answer("Oldingi faylni yuborib bo‘lmadi. Qayta yuklab ko‘ryapman…")
            else:
                if progress_msg:
                    try:
                        await cb.message.bot.delete_message(cb.message.chat.id, progress_msg.message_id)
                    except Exception:
                        logger.debug("failed to delete progress message", exc_info=True)
                return
        async with _download_sem:
            try:
                result = await download(url, height, settings.download_dir, settings.cookies_file)
                filepath = result.get("filepath")
                if not filepath or not os.path.exists(filepath):
                    raise RuntimeError("File not found after download")
            except Exception:
                logger.exception("download failed")
                if progress_msg:
                    try:
                        await cb.message.bot.delete_message(cb.message.chat.id, progress_msg.message_id)
                    except Exception:
                        logger.debug("failed to delete progress message", exc_info=True)
                await cb.message.answer("Yuklab olish muvaffaqiyatsiz. Video xususiy yoki format mavjud emas.")
                return
        try:
            await _send_media(cb.message.bot, cb.message.chat.id, filepath, None)
        except Exception:
            logger.exception("send failed")
            await cb.message.answer("Yuborish muvaffaqiyatsiz. Boshqa sifatni tanlab ko‘ring yoki fayl sifatida yuboring.")
        finally:
            if progress_msg:
                try:
                    await cb.message.bot.delete_message(cb.message.chat.id, progress_msg.message_id)
                except Exception:
                    logger.debug("failed to delete progress message", exc_info=True)

    @dp.callback_query(F.data.startswith("a|"))
    async def on_audio(cb: CallbackQuery) -> None:
        if not cb.message:
            await cb.answer("Qo‘llab-quvvatlanmaydigan kontekst", show_alert=True)
            return
        try:
            parts = cb.data.split("|", 2)
            key = parts[1]
            codec = parts[2] if len(parts) > 2 else "mp3"
        except Exception:
            await cb.answer("Noto‘g‘ri tanlov", show_alert=True)
            return
        url = cache.get(f"url:{key}")
        vid = cache.get(f"vid:{key}")
        if not url or not vid:
            await cb.answer("Tanlov muddati tugagan. Havolani qayta yuboring.", show_alert=True)
            return
        await cb.answer("Qabul qilindi", show_alert=False)
        try:
            await cb.message.delete()
        except Exception:
            logger.debug("failed to delete selection message", exc_info=True)
        progress_msg = None
        try:
            progress_msg = await cb.message.bot.send_message(cb.message.chat.id, "Audio yuklanmoqda…")
        except Exception:
            logger.debug("failed to send progress message", exc_info=True)
        cached_audio = find_cached_audio_file(settings.download_dir, vid)
        if cached_audio and os.path.exists(cached_audio):
            try:
                await _send_media(cb.message.bot, cb.message.chat.id, cached_audio, None)
            except Exception:
                logger.exception("send cached audio failed")
                await cb.message.answer("Oldingi audio faylni yuborib bo‘lmadi. Qayta yuklab ko‘ryapman…")
            else:
                if progress_msg:
                    try:
                        await cb.message.bot.delete_message(cb.message.chat.id, progress_msg.message_id)
                    except Exception:
                        logger.debug("failed to delete progress message", exc_info=True)
                return
        async with _download_sem:
            try:
                result = await download_audio(url, settings.download_dir, settings.cookies_file, codec=codec)
                filepath = result.get("filepath")
                if not filepath or not os.path.exists(filepath):
                    raise RuntimeError("Audio file not found after download")
            except Exception:
                logger.exception("audio download failed")
                if progress_msg:
                    try:
                        await cb.message.bot.delete_message(cb.message.chat.id, progress_msg.message_id)
                    except Exception:
                        logger.debug("failed to delete progress message", exc_info=True)
                await cb.message.answer("Audio yuklab olish muvaffaqiyatsiz. Video xususiy yoki format mavjud emas.")
                return
        try:
            await _send_media(cb.message.bot, cb.message.chat.id, filepath, None)
        except Exception:
            logger.exception("send audio failed")
            await cb.message.answer("Yuborish muvaffaqiyatsiz. Qayta urinib ko‘ring.")
        finally:
            if progress_msg:
                try:
                    await cb.message.bot.delete_message(cb.message.chat.id, progress_msg.message_id)
                except Exception:
                    logger.debug("failed to delete progress message", exc_info=True)


def create_app() -> tuple[Bot, Dispatcher]:
    bot = _make_bot()
    dp = Dispatcher()
    setup_dispatcher(dp)
    return bot, dp
