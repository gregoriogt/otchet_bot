"""
Telegram bot for generating daily sales reports with one editable helper message.

Fixes:
- main action buttons remain visible after final report
- helper message explicitly says what to enter next
- settings stay open until user presses ✅ Готово
- time input accepts H:MM:SS and HH:MM:SS

Env vars:
- BOT_TOKEN (required)
- APP_TIMEZONE (optional, default: Europe/Moscow)
- APP_DATA_DIR (optional)
- RAILWAY_VOLUME_MOUNT_PATH (optional)
"""

from __future__ import annotations

import json
import logging
import os
import re
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from threading import Lock
from typing import Any, Dict

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, Update
from telegram.ext import (
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)
import zoneinfo

logging.basicConfig(
    format="%(asctime)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ---------------- Storage ----------------

def get_data_dir() -> Path:
    for candidate in (os.environ.get("APP_DATA_DIR"), os.environ.get("RAILWAY_VOLUME_MOUNT_PATH")):
        if candidate:
            path = Path(candidate)
            path.mkdir(parents=True, exist_ok=True)
            return path
    fallback = Path(__file__).parent / "data"
    fallback.mkdir(parents=True, exist_ok=True)
    return fallback


DATA_DIR = get_data_dir()
SETTINGS_PATH = DATA_DIR / "user_settings.json"
SETTINGS_LOCK = Lock()

DEFAULT_SETTINGS = {
    "employee_hashtag": "#ИмяФамилия",
    "city_hashtag": "#Город",
    "mention": "@username",
    "plan_traffic": "04:00:00",
}


def load_settings() -> Dict[str, Dict[str, str]]:
    if not SETTINGS_PATH.exists():
        return {}
    try:
        with SETTINGS_PATH.open("r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception as e:
        logger.warning("Не удалось прочитать settings file: %s", e)
        return {}


USER_SETTINGS: Dict[str, Dict[str, str]] = load_settings()


def save_settings() -> None:
    with SETTINGS_LOCK:
        with SETTINGS_PATH.open("w", encoding="utf-8") as f:
            json.dump(USER_SETTINGS, f, ensure_ascii=False, indent=2)


def get_user_settings(user_id: int) -> Dict[str, str]:
    key = str(user_id)
    if key not in USER_SETTINGS:
        USER_SETTINGS[key] = deepcopy(DEFAULT_SETTINGS)
        save_settings()
    return USER_SETTINGS[key]


# ---------------- Time / formatting ----------------

def app_now() -> datetime:
    tz_name = os.environ.get("APP_TIMEZONE", "Europe/Moscow")
    try:
        tz = zoneinfo.ZoneInfo(tz_name)
    except Exception:
        tz = zoneinfo.ZoneInfo("Europe/Moscow")
    return datetime.now(tz)


def current_report_date() -> str:
    return app_now().strftime("%d.%m.%y")


def normalize_text(value: str) -> str:
    return " ".join(value.strip().split())


def normalize_time_hms(value: str) -> str | None:
    value = normalize_text(value)
    if not re.fullmatch(r"\d{1,2}:\d{2}:\d{2}", value):
        return None
    hh, mm, ss = value.split(":")
    return f"{int(hh):02d}:{mm}:{ss}"


# ---------------- Keyboards ----------------

MAIN_MENU_INLINE = InlineKeyboardMarkup([
    [InlineKeyboardButton("План", callback_data="report:plan")],
    [InlineKeyboardButton("Предварительный отчёт", callback_data="report:pred")],
    [InlineKeyboardButton("Итоговый отчёт", callback_data="report:final")],
    [InlineKeyboardButton("Настройки", callback_data="settings:menu")],
])

SETTINGS_INLINE = InlineKeyboardMarkup([
    [
        InlineKeyboardButton("Хештег сотрудника", callback_data="settings:employee_hashtag"),
        InlineKeyboardButton("Хештег города", callback_data="settings:city_hashtag"),
    ],
    [
        InlineKeyboardButton("Упоминание", callback_data="settings:mention"),
        InlineKeyboardButton("Плановый трафик", callback_data="settings:plan_traffic"),
    ],
    [
        InlineKeyboardButton("Показать настройки", callback_data="settings:show"),
        InlineKeyboardButton("✅ Готово", callback_data="settings:done"),
    ],
    [
        InlineKeyboardButton("Отмена", callback_data="menu:main"),
    ],
])

CANCEL_INLINE = InlineKeyboardMarkup([
    [InlineKeyboardButton("Отмена", callback_data="cancel")],
])

# Persistent reply keyboard at the bottom
BOTTOM_MENU = ReplyKeyboardMarkup(
    [
        ["План", "Предварительный отчёт"],
        ["Итоговый отчёт", "Настройки"],
    ],
    resize_keyboard=True,
)

# ---------------- Prompt message helpers ----------------

async def ensure_prompt_message(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str, markup: InlineKeyboardMarkup) -> None:
    prompt_chat_id = context.user_data.get("prompt_chat_id")
    prompt_message_id = context.user_data.get("prompt_message_id")

    try:
        if prompt_chat_id and prompt_message_id:
            await context.bot.edit_message_text(
                chat_id=prompt_chat_id,
                message_id=prompt_message_id,
                text=text,
                reply_markup=markup,
            )
            return
    except Exception:
        pass

    target_message = update.effective_message
    sent = await target_message.reply_text(text, reply_markup=markup)
    context.user_data["prompt_chat_id"] = sent.chat_id
    context.user_data["prompt_message_id"] = sent.message_id


async def send_bottom_menu(update: Update, text: str) -> None:
    if update.effective_message:
        await update.effective_message.reply_text(text, reply_markup=BOTTOM_MENU)


# ---------------- Report engine ----------------

REPORT_TYPES = {
    "plan": {"title": "ПЛАН", "mode": "plan"},
    "pred": {"title": "ПРЕДВАРИТЕЛЬНЫЙ ОТЧЁТ", "mode": "pred"},
    "final": {"title": "ИТОГОВЫЙ ОТЧЕТ", "mode": "final"},
}

REPORT_STEPS = {
    "common": ["pzm", "psm", "pstl", "vstl", "dozh", "traffic", "kz"],
    "final": ["pzm", "psm", "pstl", "vstl", "dozh", "traffic_fact", "kz", "arrival", "departure"],
}

STEP_PROMPTS = {
    "pzm": "Введи значение для 1 ПЗМ",
    "psm": "Введи значение для 2 ПСМ",
    "pstl": "Введи значение для 3 ПСТЛ",
    "vstl": "Введи значение для 4 ВСТЛ",
    "dozh": "Введи значение для 5 ДОЖ",
    "traffic": "Введи трафик",
    "traffic_fact": "Введи фактический трафик в формате Ч:ММ:СС или ЧЧ:ММ:СС",
    "kz": "Введи КЗ",
    "arrival": "Введи время прихода, например 8:25",
    "departure": "Введи время ухода, например 20:30",
}


def start_report(context: ContextTypes.DEFAULT_TYPE, report_key: str) -> None:
    report_meta = REPORT_TYPES[report_key]
    step_order = REPORT_STEPS["final"] if report_key == "final" else REPORT_STEPS["common"]
    context.user_data["mode"] = "report"
    context.user_data["report"] = {
        "report_key": report_key,
        "title": report_meta["title"],
        "mode": report_meta["mode"],
        "date": current_report_date(),
        "step_order": step_order,
        "step_index": 0,
        "values": {},
    }


def get_current_report(context: ContextTypes.DEFAULT_TYPE) -> Dict[str, Any] | None:
    return context.user_data.get("report")


def current_step(context: ContextTypes.DEFAULT_TYPE) -> str | None:
    report = get_current_report(context)
    if not report:
        return None
    idx = report["step_index"]
    order = report["step_order"]
    if idx >= len(order):
        return None
    return order[idx]


def advance_report_step(context: ContextTypes.DEFAULT_TYPE) -> None:
    report = get_current_report(context)
    if report:
        report["step_index"] += 1


def build_report_preview(user_id: int, report: Dict[str, Any]) -> str:
    settings = get_user_settings(user_id)
    vals = report["values"]

    lines = [
        f"1. {report['title']} {report['date']}",
        "",
        f"1 ПЗМ {vals.get('pzm', '')}",
        f"2 ПСМ {vals.get('psm', '')}",
        f"3 ПСТЛ {vals.get('pstl', '')}",
        f"4 ВСТЛ {vals.get('vstl', '')}",
        f"5 ДОЖ {vals.get('dozh', '')}",
        "",
    ]

    if report["mode"] == "final":
        lines.extend([
            f"Трафик: {vals.get('traffic_fact', '')} / {settings['plan_traffic']}",
            f"КЗ: {vals.get('kz', '')}",
            "",
            f"Приход: {vals.get('arrival', '')}",
            f"Уход: {vals.get('departure', '')}",
            "",
        ])
    else:
        lines.extend([
            f"Трафик: {vals.get('traffic', '')}",
            f"КЗ: {vals.get('kz', '')}",
            "",
        ])

    lines.extend([
        settings["employee_hashtag"],
        settings["city_hashtag"],
        settings["mention"],
    ])
    return "\n".join(lines)


def build_progress_text(user_id: int, report: Dict[str, Any], prompt: str) -> str:
    settings = get_user_settings(user_id)
    vals = report["values"]

    lines = [
        f"{report['title']} {report['date']}",
        "",
        "Уже введено:",
        f"1 ПЗМ: {vals.get('pzm', '—')}",
        f"2 ПСМ: {vals.get('psm', '—')}",
        f"3 ПСТЛ: {vals.get('pstl', '—')}",
        f"4 ВСТЛ: {vals.get('vstl', '—')}",
        f"5 ДОЖ: {vals.get('dozh', '—')}",
    ]

    if report["mode"] == "final":
        lines.extend([
            f"Трафик факт: {vals.get('traffic_fact', '—')}",
            f"КЗ: {vals.get('kz', '—')}",
            f"Приход: {vals.get('arrival', '—')}",
            f"Уход: {vals.get('departure', '—')}",
            "",
            f"Плановый трафик: {settings['plan_traffic']}",
        ])
    else:
        lines.extend([
            f"Трафик: {vals.get('traffic', '—')}",
            f"КЗ: {vals.get('kz', '—')}",
        ])

    lines.extend([
        "",
        f"Что нужно ввести сейчас:",
        prompt,
    ])
    return "\n".join(lines)


# ---------------- Settings engine ----------------

SETTINGS_PROMPTS = {
    "employee_hashtag": "Отправь новый хештег сотрудника, например #ГригорийСотников",
    "city_hashtag": "Отправь новый хештег города, например #СПБ",
    "mention": "Отправь новое упоминание, например @AleksandrSmirnov21",
    "plan_traffic": "Отправь плановый трафик в формате Ч:ММ:СС или ЧЧ:ММ:СС, например 4:00:00",
}


def start_settings(context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data["mode"] = "settings"
    context.user_data["settings_field"] = None


def build_settings_text(user_id: int, extra: str | None = None) -> str:
    s = get_user_settings(user_id)
    text = (
        "Настройки:\n\n"
        f"Хештег сотрудника: {s['employee_hashtag']}\n"
        f"Хештег города: {s['city_hashtag']}\n"
        f"Упоминание: {s['mention']}\n"
        f"Плановый трафик: {s['plan_traffic']}"
    )
    if extra:
        text += f"\n\n{extra}"
    return text


# ---------------- Handlers ----------------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if user:
        get_user_settings(user.id)

    context.user_data.clear()

    helper_text = (
        "Привет 👋\n\n"
        "Этот бот помогает быстро формировать отчёты.\n\n"
        "Сначала зайди в «Настройки» и заполни данные один раз.\n"
        "Потом выбирай нужный тип отчёта кнопками ниже."
    )
    await ensure_prompt_message(update, context, helper_text, MAIN_MENU_INLINE)
    await send_bottom_menu(update, "Меню готово.")


async def callback_router(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    data = query.data or ""
    user = update.effective_user

    if data == "menu:main":
        context.user_data["mode"] = None
        context.user_data.pop("report", None)
        context.user_data.pop("settings_field", None)
        await ensure_prompt_message(update, context, "Главное меню. Выбери действие кнопками ниже.", MAIN_MENU_INLINE)
        return

    if data == "cancel":
        context.user_data["mode"] = None
        context.user_data.pop("report", None)
        context.user_data.pop("settings_field", None)
        await ensure_prompt_message(update, context, "Отменил. Выбери действие кнопками ниже.", MAIN_MENU_INLINE)
        return

    if data == "settings:menu":
        start_settings(context)
        await ensure_prompt_message(update, context, build_settings_text(user.id), SETTINGS_INLINE)
        return

    if data == "settings:show":
        start_settings(context)
        await ensure_prompt_message(update, context, build_settings_text(user.id), SETTINGS_INLINE)
        return

    if data == "settings:done":
        context.user_data["mode"] = None
        context.user_data["settings_field"] = None
        await ensure_prompt_message(update, context, "Сохранил и вернул в главное меню.", MAIN_MENU_INLINE)
        return

    if data.startswith("settings:"):
        field = data.split(":", 1)[1]
        if field in SETTINGS_PROMPTS:
            start_settings(context)
            context.user_data["settings_field"] = field
            await ensure_prompt_message(update, context, SETTINGS_PROMPTS[field], CANCEL_INLINE)
        return

    if data.startswith("report:"):
        report_key = data.split(":", 1)[1]
        if report_key in REPORT_TYPES:
            start_report(context, report_key)
            report = get_current_report(context)
            step = current_step(context)
            await ensure_prompt_message(
                update,
                context,
                build_progress_text(user.id, report, STEP_PROMPTS[step]),
                CANCEL_INLINE,
            )
        return


async def text_router(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.message.text:
        return

    text = normalize_text(update.message.text)
    user = update.effective_user

    # Allow bottom menu buttons to behave same as inline buttons
    if text == "План":
        start_report(context, "plan")
        report = get_current_report(context)
        step = current_step(context)
        await ensure_prompt_message(update, context, build_progress_text(user.id, report, STEP_PROMPTS[step]), CANCEL_INLINE)
        return

    if text == "Предварительный отчёт":
        start_report(context, "pred")
        report = get_current_report(context)
        step = current_step(context)
        await ensure_prompt_message(update, context, build_progress_text(user.id, report, STEP_PROMPTS[step]), CANCEL_INLINE)
        return

    if text == "Итоговый отчёт":
        start_report(context, "final")
        report = get_current_report(context)
        step = current_step(context)
        await ensure_prompt_message(update, context, build_progress_text(user.id, report, STEP_PROMPTS[step]), CANCEL_INLINE)
        return

    if text == "Настройки":
        start_settings(context)
        await ensure_prompt_message(update, context, build_settings_text(user.id), SETTINGS_INLINE)
        return

    mode = context.user_data.get("mode")

    if mode == "settings":
        field = context.user_data.get("settings_field")
        if not field:
            await ensure_prompt_message(update, context, build_settings_text(user.id), SETTINGS_INLINE)
            return

        if field == "plan_traffic":
            normalized = normalize_time_hms(text)
            if not normalized:
                await ensure_prompt_message(
                    update,
                    context,
                    "Нужен формат Ч:ММ:СС или ЧЧ:ММ:СС, например 4:00:00",
                    CANCEL_INLINE,
                )
                return
            text = normalized

        settings = get_user_settings(user.id)
        settings[field] = text
        USER_SETTINGS[str(user.id)] = settings
        save_settings()
        context.user_data["settings_field"] = None

        await ensure_prompt_message(
            update,
            context,
            build_settings_text(user.id, "Сохранено. Выбери следующий пункт или нажми ✅ Готово."),
            SETTINGS_INLINE,
        )
        return

    if mode == "report":
        report = get_current_report(context)
        if not report:
            await ensure_prompt_message(update, context, "Что-то пошло не так. Возвращаю в меню.", MAIN_MENU_INLINE)
            context.user_data.clear()
            return

        step = current_step(context)
        if not step:
            await ensure_prompt_message(update, context, "Что-то пошло не так. Возвращаю в меню.", MAIN_MENU_INLINE)
            context.user_data.clear()
            return

        if step == "traffic_fact":
            normalized = normalize_time_hms(text)
            if not normalized:
                await ensure_prompt_message(
                    update,
                    context,
                    build_progress_text(user.id, report, "Нужен формат Ч:ММ:СС или ЧЧ:ММ:СС, например 3:20:28"),
                    CANCEL_INLINE,
                )
                return
            text = normalized

        report["values"][step] = text
        advance_report_step(context)

        next_step = current_step(context)
        if next_step is None:
            final_text = build_report_preview(user.id, report)
            context.user_data["mode"] = None
            context.user_data.pop("report", None)
            await update.message.reply_text(final_text, reply_markup=BOTTOM_MENU)
            await ensure_prompt_message(update, context, "Готово. Выбери следующее действие кнопками ниже.", MAIN_MENU_INLINE)
            return

        await ensure_prompt_message(
            update,
            context,
            build_progress_text(user.id, report, STEP_PROMPTS[next_step]),
            CANCEL_INLINE,
        )
        return

    await ensure_prompt_message(update, context, "Выбери действие кнопками ниже.", MAIN_MENU_INLINE)


def build_application():
    token = os.environ.get("BOT_TOKEN")
    if not token:
        raise RuntimeError("BOT_TOKEN не задан.")

    app = ApplicationBuilder().token(token).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(callback_router))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_router))
    return app


def main() -> None:
    app = build_application()
    logger.info("Бот запущен. Data dir: %s", DATA_DIR)
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
