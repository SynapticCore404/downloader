from __future__ import annotations

import asyncio
import hashlib
import logging
import os
from typing import Optional
import re

from aiogram import Bot, Dispatcher, F
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import TelegramAPIServer
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode, ChatAction, MessageEntityType
from aiogram.filters import CommandStart, Command
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
from .audio_tools import apply_effect, trim_audio_segment, convert_to_voice, extract_audio_mp3


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


async def _send_media(bot: Bot, chat_id: int, filepath: str, caption: Optional[str], *, audio_title: Optional[str] = None) -> Message:
    force_document = settings.force_document
    ext = os.path.splitext(filepath)[1].lower().lstrip(".")
    if (ext in {"mp3", "m4a", "aac", "flac", "wav", "ogg", "opus"}) and not force_document:
        return await bot.send_audio(chat_id, audio=FSInputFile(filepath), caption=caption, title=audio_title)
    if (ext in {"mp4", "mov", "mkv", "webm"}) and not force_document:
        return await bot.send_video(chat_id, video=FSInputFile(filepath), caption=caption, supports_streaming=True)
    return await bot.send_document(chat_id, document=FSInputFile(filepath), caption=caption)


def _user_tmp_dir(user_id: int) -> str:
    return os.path.join(settings.download_dir, "audio_tmp", str(user_id))


def _extract_audio_obj(msg: Message):
    if msg.audio:
        return msg.audio, "audio"
    if msg.voice:
        return msg.voice, "voice"
    if msg.document and (
        (msg.document.mime_type and msg.document.mime_type.startswith("audio/"))
        or (msg.document.file_name and os.path.splitext(msg.document.file_name)[1].lower() in {".mp3", ".m4a", ".aac", ".flac", ".wav", ".ogg", ".opus"})
    ):
        return msg.document, "document"
    return None


async def _download_by_id(bot: Bot, file_id: str, dest_path: str) -> None:
    try:
        await bot.download(file=file_id, destination=dest_path)
        return
    except Exception:
        pass
    f = await bot.get_file(file_id)
    await bot.download(file=f, destination=dest_path)


def _display_title_from_media(context_msg: Message, media) -> str:
    title = getattr(media, "title", None)
    if title:
        return title
    file_name = getattr(media, "file_name", None)
    if file_name:
        return os.path.splitext(file_name)[0]
    performer = getattr(media, "performer", None)
    if performer:
        return performer
    return "Audio"


def _effect_caption_suffix(effect: str) -> str:
    if effect == "8d":
        return "8d"
    if effect == "reverb":
        return "Reverb"
    if effect == "slow":
        return "Slowed"
    return effect


def setup_dispatcher(dp: Dispatcher) -> None:
    @dp.message(CommandStart())
    async def on_start(message: Message) -> None:
        await message.answer("YouTube yoki Instagram havolasini yuboring. Mavjud sifatlarni chiqaraman.")

    @dp.message(F.text)
    async def on_text(message: Message) -> None:
        k = cache.get(f"await_trim_times:{message.from_user.id}")
        if k:
            parts = (message.text or "").split()
            if len(parts) < 2 or not _valid_time(parts[0]) or not _valid_time(parts[1]):
                await message.answer("Vaqt formati noto‘g‘ri. Masalan: 0:10 1:00")
                return
            file_id = cache.get(f"file:{k}")
            cache.set(f"await_trim_times:{message.from_user.id}", None)
            if not file_id:
                await message.answer("Ma’lumot muddati tugagan. Audio faylni qayta yuboring.")
                return
            tmp = _user_tmp_dir(message.from_user.id)
            os.makedirs(tmp, exist_ok=True)
            src_path = os.path.join(tmp, "src")
            progress = None
            try:
                progress = await message.answer("Kesish bajarilmoqda…")
            except Exception:
                pass
            async with _download_sem:
                try:
                    await _download_by_id(message.bot, file_id, src_path)
                    out_path = await trim_audio_segment(src_path, parts[0], parts[1], tmp)
                except Exception:
                    if progress:
                        try:
                            await message.bot.delete_message(message.chat.id, progress.message_id)
                        except Exception:
                            pass
                    await message.answer("Kesish muvaffaqiyatsiz.")
                    return
            try:
                title_cap = cache.get(f"title:{k}") or "Audio"
                await _send_media(message.bot, message.chat.id, out_path, None, audio_title=title_cap)
            except Exception:
                await message.answer("Yuborish muvaffaqiyatsiz. Qayta urinib ko‘ring.")
            finally:
                if progress:
                    try:
                        await message.bot.delete_message(message.chat.id, progress.message_id)
                    except Exception:
                        pass
            return
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

    def _valid_time(s: str) -> bool:
        return bool(re.fullmatch(r"\d{1,2}:\d{2}(?::\d{2})?", s))

    def _build_effect_kb(file_id: str, title: Optional[str] = None) -> InlineKeyboardMarkup:
        k = _key_for(file_id)
        cache.set(f"file:{k}", file_id)
        if title:
            cache.set(f"title:{k}", title)
        rows = [
            [InlineKeyboardButton(text="8D", callback_data=f"fx|8d|{k}")],
            [InlineKeyboardButton(text="Reverb", callback_data=f"fx|reverb|{k}")],
            [InlineKeyboardButton(text="Sekin", callback_data=f"fx|slow|{k}")],
        ]
        return InlineKeyboardMarkup(inline_keyboard=rows)

    def _build_action_kb(file_id: str, title: Optional[str] = None) -> InlineKeyboardMarkup:
        k = _key_for(file_id)
        cache.set(f"file:{k}", file_id)
        if title:
            cache.set(f"title:{k}", title)
        rows = [
            [InlineKeyboardButton(text="effekt", callback_data=f"act|fx|{k}")],
            [InlineKeyboardButton(text="trim", callback_data=f"act|trim|{k}")],
            [InlineKeyboardButton(text="voice", callback_data=f"act|voice|{k}")],
        ]
        return InlineKeyboardMarkup(inline_keyboard=rows)

    @dp.message(Command("effect"))
    async def on_effect(message: Message) -> None:
        target = None
        if message.reply_to_message:
            target = _extract_audio_obj(message.reply_to_message)
        if not target:
            target = _extract_audio_obj(message)
        if not target:
            cache.set(f"await_fx:{message.from_user.id}", True)
            await message.answer("Audio fayl yuboring, so‘ng effektni tanlang.")
            return
        media, _ = target
        file_id = media.file_id  # type: ignore
        kb = _build_effect_kb(file_id, _display_title_from_media(message, media))
        await message.answer("Effektni tanlang:", reply_markup=kb)

    @dp.callback_query(F.data.startswith("fx|"))
    async def on_effect_apply(cb: CallbackQuery) -> None:
        if not cb.message:
            await cb.answer("Kontekst yo‘q", show_alert=True)
            return
        try:
            _, fx, k = cb.data.split("|", 2)
        except Exception:
            await cb.answer("Noto‘g‘ri tanlov", show_alert=True)
            return
        file_id = cache.get(f"file:{k}")
        if not file_id:
            await cb.answer("Ma’lumot muddati tugagan. Qaytadan urinib ko‘ring.", show_alert=True)
            return
        await cb.answer("Qabul qilindi", show_alert=False)
        try:
            await cb.message.delete()
        except Exception:
            pass
        progress = None
        try:
            progress = await cb.message.bot.send_message(cb.message.chat.id, "Audio qayta ishlanmoqda…")
        except Exception:
            pass
        tmp = _user_tmp_dir(cb.from_user.id)
        os.makedirs(tmp, exist_ok=True)
        src_path = os.path.join(tmp, "src")
        out_path = None
        async with _download_sem:
            try:
                await _download_by_id(cb.message.bot, file_id, src_path)
                out_path = await apply_effect(src_path, fx, tmp)
            except Exception:
                if progress:
                    try:
                        await cb.message.bot.delete_message(cb.message.chat.id, progress.message_id)
                    except Exception:
                        pass
                await cb.message.answer("Qayta ishlashda xatolik yuz berdi.")
                return
        try:
            title = cache.get(f"title:{k}") or "Audio"
            cap = f"{title} {_effect_caption_suffix(fx)}"
            await _send_media(cb.message.bot, cb.message.chat.id, out_path, None, audio_title=cap)
        except Exception:
            await cb.message.answer("Yuborish muvaffaqiyatsiz. Qayta urinib ko‘ring.")
        finally:
            if progress:
                try:
                    await cb.message.bot.delete_message(cb.message.chat.id, progress.message_id)
                except Exception:
                    pass

    @dp.callback_query(F.data.startswith("act|"))
    async def on_action(cb: CallbackQuery) -> None:
        if not cb.message:
            await cb.answer("Kontekst yo‘q", show_alert=True)
            return
        try:
            _, act, k = cb.data.split("|", 2)
        except Exception:
            await cb.answer("Noto‘g‘ri tanlov", show_alert=True)
            return
        file_id = cache.get(f"file:{k}")
        if not file_id:
            await cb.answer("Ma’lumot muddati tugagan.", show_alert=True)
            return
        await cb.answer("Qabul qilindi", show_alert=False)
        if act == "fx":
            title = cache.get(f"title:{k}")
            kb = _build_effect_kb(file_id, title)
            await cb.message.answer("Effektni tanlang:", reply_markup=kb)
            try:
                await cb.message.delete()
            except Exception:
                pass
            return
        if act == "trim":
            cache.set(f"await_trim_times:{cb.from_user.id}", k)
            await cb.message.answer("Qaysi joyini kesib beray? Masalan: 0:10 1:00")
            try:
                await cb.message.delete()
            except Exception:
                pass
            return
        if act == "voice":
            try:
                await cb.message.delete()
            except Exception:
                pass
            progress = None
            try:
                progress = await cb.message.bot.send_message(cb.message.chat.id, "Ovoz xabari tayyorlanmoqda…")
            except Exception:
                pass
            tmp = _user_tmp_dir(cb.from_user.id)
            os.makedirs(tmp, exist_ok=True)
            src_path = os.path.join(tmp, "src")
            async with _download_sem:
                try:
                    await _download_by_id(cb.message.bot, file_id, src_path)
                    out_path = await convert_to_voice(src_path, tmp)
                except Exception:
                    if progress:
                        try:
                            await cb.message.bot.delete_message(cb.message.chat.id, progress.message_id)
                        except Exception:
                            pass
                    await cb.message.answer("Konvertatsiya muvaffaqiyatsiz.")
                    return
            try:
                await cb.message.bot.send_voice(cb.message.chat.id, voice=FSInputFile(out_path))
            except Exception:
                await cb.message.answer("Yuborish muvaffaqiyatsiz. Qayta urinib ko‘ring.")
            finally:
                if progress:
                    try:
                        await cb.message.bot.delete_message(cb.message.chat.id, progress.message_id)
                    except Exception:
                        pass

    @dp.message(Command("trim"))
    async def on_trim(message: Message) -> None:
        parts = (message.text or "").split()
        if len(parts) < 3:
            await message.answer("Foydalanish: /trim 00:30 01:10. Buyruqni audio xabariga javob sifatida yuboring yoki avval audio yuboring.")
            cache.set(f"await_trim:{message.from_user.id}", None)
            return
        start, end = parts[1], parts[2]
        if not (_valid_time(start) and _valid_time(end)):
            await message.answer("Vaqt formati noto‘g‘ri. Masalan: 00:30 01:10")
            return
        target = None
        if message.reply_to_message:
            target = _extract_audio_obj(message.reply_to_message)
        if not target:
            target = _extract_audio_obj(message)
        if not target:
            cache.set(f"await_trim:{message.from_user.id}", (start, end))
            await message.answer("Audio yuboring. Belgilangan oraliq kesib olinadi.")
            return
        media, _ = target
        file_id = media.file_id  # type: ignore
        tmp = _user_tmp_dir(message.from_user.id)
        os.makedirs(tmp, exist_ok=True)
        src_path = os.path.join(tmp, "src")
        progress = None
        try:
            progress = await message.answer("Kesish bajarilmoqda…")
        except Exception:
            pass
        async with _download_sem:
            try:
                await _download_by_id(message.bot, file_id, src_path)
                out_path = await trim_audio_segment(src_path, start, end, tmp)
            except Exception:
                if progress:
                    try:
                        await message.bot.delete_message(message.chat.id, progress.message_id)
                    except Exception:
                        pass
                await message.answer("Kesish muvaffaqiyatsiz.")
                return
        try:
            title_cap = _display_title_from_media(message, media)
            await _send_media(message.bot, message.chat.id, out_path, None, audio_title=title_cap)
        except Exception:
            await message.answer("Yuborish muvaffaqiyatsiz. Qayta urinib ko‘ring.")
        finally:
            if progress:
                try:
                    await message.bot.delete_message(message.chat.id, progress.message_id)
                except Exception:
                    pass

    @dp.message(Command("voice"))
    async def on_voice(message: Message) -> None:
        parts = (message.text or "").split()
        start = parts[1] if len(parts) > 1 else None
        end = parts[2] if len(parts) > 2 else None
        if start and not _valid_time(start):
            await message.answer("Boshlanish vaqti noto‘g‘ri.")
            return
        if end and not _valid_time(end):
            await message.answer("Tugash vaqti noto‘g‘ri.")
            return
        target = None
        if message.reply_to_message:
            target = _extract_audio_obj(message.reply_to_message)
        if not target:
            target = _extract_audio_obj(message)
        if not target:
            cache.set(f"await_voice:{message.from_user.id}", (start, end))
            await message.answer("Audio yuboring. Ovoz xabari shakliga o‘tkazaman.")
            return
        media, _ = target
        file_id = media.file_id  # type: ignore
        tmp = _user_tmp_dir(message.from_user.id)
        os.makedirs(tmp, exist_ok=True)
        src_path = os.path.join(tmp, "src")
        progress = None
        try:
            progress = await message.answer("Ovoz xabari tayyorlanmoqda…")
        except Exception:
            pass
        async with _download_sem:
            try:
                await _download_by_id(message.bot, file_id, src_path)
                out_path = await convert_to_voice(src_path, tmp, start=start, end=end)
            except Exception:
                if progress:
                    try:
                        await message.bot.delete_message(message.chat.id, progress.message_id)
                    except Exception:
                        pass
                await message.answer("Konvertatsiya muvaffaqiyatsiz.")
                return
        try:
            await message.bot.send_voice(message.chat.id, voice=FSInputFile(out_path))
        except Exception:
            await message.answer("Yuborish muvaffaqiyatsiz. Qayta urinib ko‘ring.")
        finally:
            if progress:
                try:
                    await message.bot.delete_message(message.chat.id, progress.message_id)
                except Exception:
                    pass

    @dp.message(F.audio | F.voice | F.document)
    async def on_audio_message(message: Message) -> None:
        state_fx = cache.get(f"await_fx:{message.from_user.id}")
        state_trim = cache.get(f"await_trim:{message.from_user.id}")
        state_voice = cache.get(f"await_voice:{message.from_user.id}")
        target = _extract_audio_obj(message)
        if not target:
            return
        media, _ = target
        file_id = media.file_id  # type: ignore
        if state_fx:
            cache.set(f"await_fx:{message.from_user.id}", None)
            title = _display_title_from_media(message, media)
            kb = _build_effect_kb(file_id, title)
            await message.answer("Effektni tanlang:", reply_markup=kb)
            return
        if state_trim and isinstance(state_trim, tuple) and len(state_trim) == 2:
            start, end = state_trim
            if not (start and end and _valid_time(start) and _valid_time(end)):
                await message.answer("Vaqt formati noto‘g‘ri. Masalan: 00:30 01:10")
                cache.set(f"await_trim:{message.from_user.id}", None)
                return
            cache.set(f"await_trim:{message.from_user.id}", None)
            tmp = _user_tmp_dir(message.from_user.id)
            os.makedirs(tmp, exist_ok=True)
            src_path = os.path.join(tmp, "src")
            progress = None
            try:
                progress = await message.answer("Kesish bajarilmoqda…")
            except Exception:
                pass
            async with _download_sem:
                try:
                    await _download_by_id(message.bot, file_id, src_path)
                    out_path = await trim_audio_segment(src_path, start, end, tmp)
                except Exception:
                    if progress:
                        try:
                            await message.bot.delete_message(message.chat.id, progress.message_id)
                        except Exception:
                            pass
                    await message.answer("Kesish muvaffaqiyatsiz.")
                    return
            try:
                title_cap = _display_title_from_media(message, media)
                await _send_media(message.bot, message.chat.id, out_path, None, audio_title=title_cap)
            except Exception:
                await message.answer("Yuborish muvaffaqiyatsiz. Qayta urinib ko‘ring.")
            finally:
                if progress:
                    try:
                        await message.bot.delete_message(message.chat.id, progress.message_id)
                    except Exception:
                        pass
            return
        if state_voice and isinstance(state_voice, tuple):
            start = state_voice[0]
            end = state_voice[1] if len(state_voice) > 1 else None
            cache.set(f"await_voice:{message.from_user.id}", None)
            tmp = _user_tmp_dir(message.from_user.id)
            os.makedirs(tmp, exist_ok=True)
            src_path = os.path.join(tmp, "src")
            progress = None
            try:
                progress = await message.answer("Ovoz xabari tayyorlanmoqda…")
            except Exception:
                pass
            async with _download_sem:
                try:
                    await _download_by_id(message.bot, file_id, src_path)
                    out_path = await convert_to_voice(src_path, tmp, start=start, end=end)
                except Exception:
                    if progress:
                        try:
                            await message.bot.delete_message(message.chat.id, progress.message_id)
                        except Exception:
                            pass
                    await message.answer("Konvertatsiya muvaffaqiyatsiz.")
                    return
            try:
                await message.bot.send_voice(message.chat.id, voice=FSInputFile(out_path))
            except Exception:
                await message.answer("Yuborish muvaffaqiyatsiz. Qayta urinib ko‘ring.")
            finally:
                if progress:
                    try:
                        await message.bot.delete_message(message.chat.id, progress.message_id)
                    except Exception:
                        pass

        title = _display_title_from_media(message, media)
        kb = _build_action_kb(file_id, title)
        await message.answer("Tanlang:", reply_markup=kb)

    @dp.message(F.video | F.document)
    async def on_video_message(message: Message) -> None:
        media = None
        if message.video:
            media = message.video
        elif message.document:
            d = message.document
            ext = os.path.splitext(d.file_name or "")[1].lower()
            if (d.mime_type and d.mime_type.startswith("video/")) or ext in {".mp4", ".mov", ".mkv", ".webm"}:
                media = d
        if not media:
            return
        file_id = media.file_id  # type: ignore
        tmp = _user_tmp_dir(message.from_user.id)
        os.makedirs(tmp, exist_ok=True)
        src_path = os.path.join(tmp, "src")
        progress = None
        try:
            progress = await message.answer("Audio ajratilmoqda…")
        except Exception:
            pass
        async with _download_sem:
            try:
                await _download_by_id(message.bot, file_id, src_path)
                out_path = await extract_audio_mp3(src_path, tmp)
            except Exception:
                if progress:
                    try:
                        await message.bot.delete_message(message.chat.id, progress.message_id)
                    except Exception:
                        pass
                await message.answer("Audio ajratib bo‘lmadi.")
                return
        try:
            base_title = getattr(media, "file_name", None)
            if base_title:
                base_title = os.path.splitext(base_title)[0]
            else:
                base_title = "Audio"
            await _send_media(message.bot, message.chat.id, out_path, None, audio_title=base_title)
        except Exception:
            await message.answer("Yuborish muvaffaqiyatsiz. Qayta urinib ko‘ring.")
        finally:
            if progress:
                try:
                    await message.bot.delete_message(message.chat.id, progress.message_id)
                except Exception:
                    pass

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
