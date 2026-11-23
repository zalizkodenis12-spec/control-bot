from __future__ import annotations

import asyncio
import html
import logging
import os
import sys
from typing import Any

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramAPIError, TelegramUnauthorizedError
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
)

import config
import database
from config import DB_PATH

logger = logging.getLogger(__name__)

MAX_MESSAGE_LENGTH = 3800

router = Router(name="contest_router")


# ---------------------------------------------------------------------------
# Keyboards
# ---------------------------------------------------------------------------


def get_participant_inline_keyboard() -> InlineKeyboardMarkup:
    """Inline keyboard with the '🎉 Участвовать' button for participants."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🎉 Участвовать",
                    callback_data="participate",
                )
            ]
        ]
    )


def get_participant_reply_keyboard(is_user_admin: bool = False) -> ReplyKeyboardMarkup:
    """Reply keyboard with '🎉 Участвовать' (and Admin Panel shortcut for the single admin)."""
    rows = [[KeyboardButton(text="🎉 Участвовать")]]
    if is_user_admin:
        rows.append([KeyboardButton(text="⚙️ Админ-панель")])
    return ReplyKeyboardMarkup(
        keyboard=rows,
        resize_keyboard=True,
    )


def get_admin_inline_keyboard(db_path: str = DB_PATH) -> InlineKeyboardMarkup:
    """Inline keyboard for the Author/Admin Panel."""
    count = database.get_participants_count(db_path=db_path)
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🏁 Закончить конкурс",
                    callback_data="admin_finish",
                )
            ],
            [
                InlineKeyboardButton(
                    text=f"📋 Список участников ({count})",
                    callback_data="admin_list",
                )
            ],
            [
                InlineKeyboardButton(
                    text="🔄 Новый конкурс (сброс)",
                    callback_data="admin_reset",
                )
            ],
            [
                InlineKeyboardButton(
                    text="📢 Пост для канала",
                    callback_data="admin_post",
                )
            ],
            [
                InlineKeyboardButton(
                    text="🎉 Участвовать",
                    callback_data="participate",
                )
            ],
        ]
    )


def get_admin_reset_confirm_keyboard() -> InlineKeyboardMarkup:
    """Confirmation inline keyboard before resetting the contest."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✅ Да, начать новый конкурс",
                    callback_data="admin_reset_confirm",
                )
            ],
            [
                InlineKeyboardButton(
                    text="❌ Отмена",
                    callback_data="admin_panel",
                )
            ],
        ]
    )


def get_channel_post_keyboard(bot_username: str) -> InlineKeyboardMarkup:
    """Inline URL button 'Участвовать' for the channel contest post."""
    clean_username = (bot_username or "your_bot").lstrip("@").strip()
    deep_link = f"https://t.me/{clean_username}?start=contest"
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Участвовать",
                    url=deep_link,
                )
            ]
        ]
    )


# ---------------------------------------------------------------------------
# Pure Helpers & Business Logic (Unit-Testable without Network)
# ---------------------------------------------------------------------------


def format_new_participant_admin_notification(
    number: int,
    first_name: str,
    last_name: str | None = None,
    username: str | None = None,
) -> str:
    """
    Build the HTML notification sent to the admin when a new participant joins.
    Uses database.format_participant_line so names are HTML-escaped and
    @username is hidden inside <tg-spoiler>...</tg-spoiler>.
    """
    participant_line = database.format_participant_line(
        {
            "number": number,
            "first_name": first_name,
            "last_name": last_name,
            "username": username,
        },
        include_number=False,
    )
    return f"🔔 <b>Новый участник конкурса!</b>\n\n№{number} — {participant_line}"


def chunk_messages(
    header: str,
    lines: list[str],
    max_length: int = MAX_MESSAGE_LENGTH,
) -> list[str]:
    """
    Combine a header and numbered participant lines into one or more message chunks,
    guaranteeing that no chunk exceeds max_length (default 3800 < 4096).
    """
    if not lines:
        return [header]

    chunks: list[str] = []
    current = header

    for line in lines:
        separator = "\n" if not current.endswith("\n") else ""
        candidate = f"{current}{separator}{line}"
        if len(candidate) <= max_length:
            current = candidate
        else:
            if current.strip():
                chunks.append(current)
            if len(line) > max_length:
                for i in range(0, len(line), max_length):
                    chunks.append(line[i : i + max_length])
                current = ""
            else:
                current = line

    if current.strip():
        chunks.append(current)

    return chunks


def build_participants_list_messages(
    db_path: str = DB_PATH,
    finished: bool = False,
    max_length: int = MAX_MESSAGE_LENGTH,
) -> list[str]:
    """
    Build the HTML message(s) showing all participants from 1 to N.
    - If finished=True: formats the final contest completion list.
    - If finished=False: formats the current participant list with contest status.
    """
    participants = database.get_all_participants(db_path=db_path)
    count = len(participants)

    if finished:
        if count == 0:
            return ["🏁 Конкурс завершён. Участников не было."]
        header = f"🏁 <b>Конкурс завершён! Список всех участников ({count}):</b>\n\n"
    else:
        active = database.is_contest_active(db_path=db_path)
        status_text = "🟢 Активен" if active else "🔴 Завершён"
        if count == 0:
            return [
                f"📋 <b>Список участников пуст (0).</b>\n"
                f"Статус конкурса: {status_text}"
            ]
        header = (
            f"📋 <b>Список участников ({count}):</b>\n"
            f"Статус конкурса: {status_text}\n\n"
        )

    lines = [
        database.format_participant_line(p, include_number=True)
        for p in participants
    ]
    return chunk_messages(header=header, lines=lines, max_length=max_length)


def finish_contest_and_get_messages(
    db_path: str = DB_PATH,
    max_length: int = MAX_MESSAGE_LENGTH,
) -> list[str]:
    """
    Mark the contest as finished (is_active=False) and return the chunked
    numbered list of all participants from 1 to last.
    """
    database.set_contest_active(False, db_path=db_path)
    return build_participants_list_messages(
        db_path=db_path,
        finished=True,
        max_length=max_length,
    )


def handle_participation(
    user_id: int,
    first_name: str,
    last_name: str | None = None,
    username: str | None = None,
    db_path: str = DB_PATH,
) -> dict[str, Any]:
    """
    Process a user's request to participate in the contest.
    Returns a dict with:
      - active (bool)
      - is_new (bool)
      - number (int | None)
      - user_message (str)
      - admin_notification (str | None)
      - admin_ids (list[int])
    """
    if username:
        database.is_admin(user_id, username=username, db_path=db_path)

    if not database.is_contest_active(db_path=db_path):
        return {
            "active": False,
            "is_new": False,
            "number": None,
            "user_message": "⛔ Конкурс уже завершён! Ожидайте начала нового конкурса.",
            "admin_notification": None,
            "admin_ids": [],
        }

    is_new, number = database.register_participant(
        user_id=user_id,
        first_name=first_name or "",
        last_name=last_name,
        username=username,
        db_path=db_path,
    )

    if is_new:
        user_message = (
            "✅ Вы успешно зарегистрированы в конкурсе!\n\n"
            f"🎟 Ваш порядковый номер: <b>№{number}</b>"
        )
        admin_notification = format_new_participant_admin_notification(
            number=number,
            first_name=first_name or "",
            last_name=last_name,
            username=username,
        )
        admin_ids = database.get_all_admins(db_path=db_path)
    else:
        user_message = (
            "ℹ️ Вы уже участвуете в этом конкурсе!\n\n"
            f"🎟 Ваш порядковый номер: <b>№{number}</b>"
        )
        admin_notification = None
        admin_ids = []

    return {
        "active": True,
        "is_new": is_new,
        "number": number,
        "user_message": user_message,
        "admin_notification": admin_notification,
        "admin_ids": admin_ids,
    }


def get_welcome_text(first_name: str | None = None, db_path: str = DB_PATH) -> str:
    """Build the Russian welcome message shown to users on /start."""
    name_part = f", <b>{html.escape(first_name.strip())}</b>" if first_name and first_name.strip() else ""
    active = database.is_contest_active(db_path=db_path)
    if active:
        return (
            f"👋 Привет{name_part}!\n\n"
            "🎁 Добро пожаловать в бот для участия в конкурсе!\n"
            "Нажмите кнопку <b>«🎉 Участвовать»</b> ниже, чтобы зарегистрироваться "
            "и получить свой порядковый номер."
        )
    return (
        f"👋 Привет{name_part}!\n\n"
        "⛔ В данный момент активный конкурс уже завершён. "
        "Ожидайте анонса нового конкурса!"
    )


def get_admin_panel_text(db_path: str = DB_PATH) -> str:
    """Build the status overview text for the Author/Admin Panel."""
    active = database.is_contest_active(db_path=db_path)
    count = database.get_participants_count(db_path=db_path)
    status_str = "🟢 Активен (идёт набор участников)" if active else "🔴 Завершён"
    return (
        "⚙️ <b>Панель управления конкурсом (Автор)</b>\n\n"
        f"📊 Статус конкурса: <b>{status_str}</b>\n"
        f"👥 Зарегистрировано участников: <b>{count}</b>\n\n"
        "Выберите нужное действие кнопками ниже:"
    )


async def notify_admins_new_participant(
    bot: Bot | None,
    admin_ids: list[int],
    notification_text: str,
) -> None:
    """Send new participant HTML notification to the configured admin."""
    if not bot or not notification_text or not admin_ids:
        return
    for admin_id in admin_ids:
        try:
            await bot.send_message(
                chat_id=admin_id,
                text=notification_text,
                parse_mode=ParseMode.HTML,
            )
        except Exception as exc:
            logger.warning("Не удалось отправить уведомление админу %s: %s", admin_id, exc)


# ---------------------------------------------------------------------------
# Aiogram v3 Handlers
# ---------------------------------------------------------------------------


@router.message(CommandStart())
async def cmd_start(
    message: Message,
    command: CommandObject | None = None,
    db_path: str = DB_PATH,
) -> None:
    """
    Handle /start and /start contest.
    - Shows the Russian welcome message with the '🎉 Участвовать' button (both reply and inline).
    - Strictly ONLY if the user is the configured ADMIN_ID, also displays the Admin Panel controls.
    """
    user = message.from_user
    if user is None:
        return

    user_is_admin = database.is_admin(user.id, username=getattr(user, "username", None), db_path=db_path)
    welcome_text = get_welcome_text(user.first_name, db_path=db_path)

    await message.answer(
        "👇 Вы можете нажать кнопку <b>«🎉 Участвовать»</b> под сообщением или на клавиатуре внизу:",
        parse_mode=ParseMode.HTML,
        reply_markup=get_participant_reply_keyboard(is_user_admin=user_is_admin),
    )

    if user_is_admin:
        combined_text = f"{welcome_text}\n\n{'─' * 20}\n\n{get_admin_panel_text(db_path=db_path)}"
        await message.answer(
            combined_text,
            parse_mode=ParseMode.HTML,
            reply_markup=get_admin_inline_keyboard(db_path=db_path),
        )
    else:
        await message.answer(
            welcome_text,
            parse_mode=ParseMode.HTML,
            reply_markup=get_participant_inline_keyboard(),
        )


@router.message(Command("id"))
async def cmd_id(message: Message) -> None:
    """Return the user's Telegram ID so the owner can set ADMIN_ID in .env."""
    user = message.from_user
    if user is None:
        return
    await message.answer(
        f"🆔 Ваш Telegram ID: <code>{user.id}</code>",
        parse_mode=ParseMode.HTML,
    )


@router.message(Command("admin"))
async def cmd_admin(
    message: Message,
    command: CommandObject | None = None,
    db_path: str = DB_PATH,
) -> None:
    """
    Handle /admin.
    Strictly only responds if user.id == config.ADMIN_ID.
    If not admin, silently ignores the command (no reply, error, or hint).
    """
    user = message.from_user
    if user is None or not database.is_admin(user.id, username=getattr(user, "username", None), db_path=db_path):
        return

    await message.answer(
        get_admin_panel_text(db_path=db_path),
        parse_mode=ParseMode.HTML,
        reply_markup=get_admin_inline_keyboard(db_path=db_path),
    )


@router.message(F.text.in_({"⚙️ Админ-панель", "Админ-панель"}))
async def on_admin_panel_text(message: Message, db_path: str = DB_PATH) -> None:
    """
    Open Admin Panel from the reply keyboard button.
    Strictly only responds if user.id == config.ADMIN_ID; silently ignores non-admins.
    """
    user = message.from_user
    if user is None or not database.is_admin(user.id, username=getattr(user, "username", None), db_path=db_path):
        return

    await message.answer(
        get_admin_panel_text(db_path=db_path),
        parse_mode=ParseMode.HTML,
        reply_markup=get_admin_inline_keyboard(db_path=db_path),
    )


@router.message(F.text.in_({"🎉 Участвовать", "Участвовать", "🎉 Участвовать (тест)"}))
async def on_participate_text(message: Message, db_path: str = DB_PATH) -> None:
    """Handle participation when user taps the reply keyboard button '🎉 Участвовать'."""
    user = message.from_user
    if user is None:
        return

    result = handle_participation(
        user_id=user.id,
        first_name=user.first_name or "",
        last_name=user.last_name,
        username=user.username,
        db_path=db_path,
    )

    await message.answer(
        result["user_message"],
        parse_mode=ParseMode.HTML,
    )

    if result["is_new"] and result["admin_notification"]:
        await notify_admins_new_participant(
            bot=message.bot,
            admin_ids=result["admin_ids"],
            notification_text=result["admin_notification"],
        )


@router.callback_query(F.data == "participate")
async def on_participate_callback(
    callback: CallbackQuery,
    db_path: str = DB_PATH,
) -> None:
    """Handle participation when user taps the inline button '🎉 Участвовать'."""
    user = callback.from_user
    if user is None:
        return

    result = handle_participation(
        user_id=user.id,
        first_name=user.first_name or "",
        last_name=user.last_name,
        username=user.username,
        db_path=db_path,
    )

    if callback.message is not None:
        await callback.message.answer(
            result["user_message"],
            parse_mode=ParseMode.HTML,
        )

    if not result["active"]:
        await callback.answer("⛔ Конкурс уже завершён!", show_alert=False)
    elif result["is_new"]:
        await callback.answer(f"✅ Ваш номер: №{result['number']}", show_alert=False)
    else:
        await callback.answer(f"ℹ️ Вы уже участвуете (№{result['number']})", show_alert=False)

    if result["is_new"] and result["admin_notification"]:
        await notify_admins_new_participant(
            bot=callback.bot,
            admin_ids=result["admin_ids"],
            notification_text=result["admin_notification"],
        )


@router.callback_query(F.data == "admin_panel")
async def on_admin_panel_callback(
    callback: CallbackQuery,
    db_path: str = DB_PATH,
) -> None:
    """Return to the main Admin Panel view. Silently ignored for non-admins."""
    user = callback.from_user
    if user is None or not database.is_admin(user.id, username=getattr(user, "username", None), db_path=db_path):
        await callback.answer()
        return

    if callback.message is not None:
        await callback.message.answer(
            get_admin_panel_text(db_path=db_path),
            parse_mode=ParseMode.HTML,
            reply_markup=get_admin_inline_keyboard(db_path=db_path),
        )
    await callback.answer()


@router.callback_query(F.data == "admin_finish")
async def on_admin_finish_callback(
    callback: CallbackQuery,
    db_path: str = DB_PATH,
) -> None:
    """
    Handle '🏁 Закончить конкурс':
    - Sets contest active = False
    - Sends complete numbered list of all participants from 1 to last with hidden <tg-spoiler>@username</tg-spoiler>
    - Chunks messages safely under 3800 characters
    - Silently ignored for non-admins.
    """
    user = callback.from_user
    if user is None or not database.is_admin(user.id, username=getattr(user, "username", None), db_path=db_path):
        await callback.answer()
        return

    messages = finish_contest_and_get_messages(db_path=db_path)

    if callback.message is not None:
        for idx, text_chunk in enumerate(messages):
            is_last = idx == len(messages) - 1
            await callback.message.answer(
                text_chunk,
                parse_mode=ParseMode.HTML,
                reply_markup=get_admin_inline_keyboard(db_path=db_path) if is_last else None,
            )

    await callback.answer("🏁 Конкурс завершён!")


@router.callback_query(F.data == "admin_list")
async def on_admin_list_callback(
    callback: CallbackQuery,
    db_path: str = DB_PATH,
) -> None:
    """
    Handle '📋 Список участников ({count})':
    - Shows current numbered list 1..N and contest status without ending the contest.
    - Silently ignored for non-admins.
    """
    user = callback.from_user
    if user is None or not database.is_admin(user.id, username=getattr(user, "username", None), db_path=db_path):
        await callback.answer()
        return

    messages = build_participants_list_messages(db_path=db_path, finished=False)

    if callback.message is not None:
        for idx, text_chunk in enumerate(messages):
            is_last = idx == len(messages) - 1
            await callback.message.answer(
                text_chunk,
                parse_mode=ParseMode.HTML,
                reply_markup=get_admin_inline_keyboard(db_path=db_path) if is_last else None,
            )

    await callback.answer()


@router.callback_query(F.data == "admin_reset")
async def on_admin_reset_callback(
    callback: CallbackQuery,
    db_path: str = DB_PATH,
) -> None:
    """Ask confirmation before resetting the contest and clearing participants. Silently ignored for non-admins."""
    user = callback.from_user
    if user is None or not database.is_admin(user.id, username=getattr(user, "username", None), db_path=db_path):
        await callback.answer()
        return

    count = database.get_participants_count(db_path=db_path)
    if callback.message is not None:
        await callback.message.answer(
            "⚠️ <b>Подтверждение сброса конкурса</b>\n\n"
            f"Сейчас в списке <b>{count}</b> участник(ов).\n"
            "Вы уверены, что хотите очистить текущий список и запустить новый конкурс с №1?",
            parse_mode=ParseMode.HTML,
            reply_markup=get_admin_reset_confirm_keyboard(),
        )
    await callback.answer()


@router.callback_query(F.data == "admin_reset_confirm")
async def on_admin_reset_confirm_callback(
    callback: CallbackQuery,
    db_path: str = DB_PATH,
) -> None:
    """Confirm resetting the contest: clears participants and sets active=True. Silently ignored for non-admins."""
    user = callback.from_user
    if user is None or not database.is_admin(user.id, username=getattr(user, "username", None), db_path=db_path):
        await callback.answer()
        return

    database.reset_contest(db_path=db_path)

    if callback.message is not None:
        await callback.message.answer(
            "🔄 <b>Новый конкурс успешно запущен!</b>\n\n"
            "Список участников очищен. Нумерация новых участников начинается с <b>№1</b>.",
            parse_mode=ParseMode.HTML,
            reply_markup=get_admin_inline_keyboard(db_path=db_path),
        )
    await callback.answer("🔄 Новый конкурс запущен!")


@router.callback_query(F.data == "admin_post")
async def on_admin_post_callback(
    callback: CallbackQuery,
    db_path: str = DB_PATH,
) -> None:
    """
    Generate the deep-link and a ready-to-forward channel post template
    with an inline URL button 'Участвовать'. Silently ignored for non-admins.
    """
    user = callback.from_user
    if user is None or not database.is_admin(user.id, username=getattr(user, "username", None), db_path=db_path):
        await callback.answer()
        return

    bot_username = "your_bot"
    if callback.bot is not None:
        try:
            me = await callback.bot.get_me()
            if me and me.username:
                bot_username = me.username
        except Exception:
            pass

    deep_link = f"https://t.me/{bot_username}?start=contest"

    if callback.message is not None:
        await callback.message.answer(
            "📢 <b>Ссылка и шаблон поста для канала</b>\n\n"
            "🔗 Прямая ссылка для кнопки «Участвовать» в канале:\n"
            f"<code>{deep_link}</code>\n\n"
            "Ниже отправлен готовый пост с кнопкой <b>«Участвовать»</b> — "
            "вы можете переслать его в свой канал или прикрепить ссылку выше к любому посту в канале:",
            parse_mode=ParseMode.HTML,
        )
        await callback.message.answer(
            "🎁 <b>КОНКУРС!</b>\n\n"
            "Нажмите на кнопку <b>«Участвовать»</b> ниже, перейдите в бота и нажмите <b>Start</b>, "
            "чтобы получить свой порядковый номер участника!",
            parse_mode=ParseMode.HTML,
            reply_markup=get_channel_post_keyboard(bot_username),
        )

    await callback.answer()


# ---------------------------------------------------------------------------
# Dispatcher & Startup Entrypoint
# ---------------------------------------------------------------------------


def create_dispatcher() -> Dispatcher:
    """Create a new Dispatcher and attach the contest router."""
    dp = Dispatcher()
    dp.include_router(router)
    return dp


async def main() -> None:
    """Initialize database, verify BOT_TOKEN with Telegram API, and start polling."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    )

    database.init_db(db_path=DB_PATH)
    logger.info("База данных инициализирована: %s", DB_PATH)

    if config.ADMIN_ID is not None:
        logger.info("Настроен единственный администратор (ADMIN_ID): %s", config.ADMIN_ID)
    elif getattr(config, "ADMIN_USERNAME", None):
        logger.info(
            "Настроен единственный администратор по username (ADMIN_USERNAME): @%s",
            config.ADMIN_USERNAME,
        )
    else:
        logger.warning(
            "ВНИМАНИЕ: ADMIN_ID / ADMIN_USERNAME в .env не заданы! Укажите их в .env."
        )

    token = config.BOT_TOKEN
    if not token or ":" not in token:
        logger.error(
            "ОШИБКА: BOT_TOKEN не задан или имеет неверный формат в файле .env! "
            "Укажите корректный токен от @BotFather."
        )
        sys.exit(1)

    bot = Bot(
        token=token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )

    try:
        me = await bot.get_me()
        logger.info(
            "Бот успешно подключён: @%s (ID: %s, Имя: %s)",
            me.username,
            me.id,
            me.first_name,
        )
        logger.info(
            "Ссылка для кнопки в канале: https://t.me/%s?start=contest",
            me.username,
        )
    except TelegramUnauthorizedError:
        logger.error(
            "ОШИБКА АВТОРИЗАЦИИ (Unauthorized): Токен BOT_TOKEN в .env отклонён сервером Telegram! "
            "Проверьте правильность токена в @BotFather и обновите файл .env."
        )
        await bot.session.close()
        sys.exit(1)
    except Exception as exc:
        logger.warning(
            "Не удалось выполнить предварительную проверку getMe (%s). "
            "Переход к polling с автоматическим переподключением...",
            exc,
        )

    dp = Dispatcher()
    dp.include_router(router)

    # If running in cloud hosting (Render, Railway, Koyeb, etc.), start a lightweight healthcheck HTTP server
    port_str = os.getenv("PORT")
    if port_str:
        try:
            from aiohttp import web
            async def handle_health(request):
                return web.Response(text="Bot is running 24/7!", status=200)

            app = web.Application()
            app.router.add_get("/", handle_health)
            app.router.add_get("/health", handle_health)
            runner = web.AppRunner(app)
            await runner.setup()
            site = web.TCPSite(runner, "0.0.0.0", int(port_str))
            await site.start()
            logger.info("Healthcheck HTTP сервер запущен на порту %s", port_str)
        except Exception as e:
            logger.warning("Не удалось запустить healthcheck HTTP сервер: %s", e)

    logger.info("Запуск polling... Нажмите Ctrl+C для остановки.")
    try:
        await dp.start_polling(bot)
    finally:
        await bot.session.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        pass
