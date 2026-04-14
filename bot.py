"""
Telegram bot for generating daily sales reports.

UX logic:
- After each user answer, bot deletes the previous helper message
- Then sends a new helper message with the next prompt
- Final report is sent as a separate message
- Bottom reply keyboard stays visible

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

from telegram import ReplyKeyboardMarkup, Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)
import zoneinfo

logging.basicConfig(format="%(asctime)s - %(levelname)s - %(message)s", level=logging.INFO)
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

BOTTOM_MENU = ReplyKeyboardMarkup(
    [
        ["План", "Предварительный отчёт"],
        ["Итоговый отчёт", "Настройки"],
    ],
    resize_keyboard=True,
)

SETTINGS_MENU = ReplyKeyboardMarkup(
    [
        ["Хештег сотрудника", "Хештег города"],
        ["Упоминание", "Плановый трафик"],
        ["Показать настройки", "✅ Готово"],
        ["Отмена"],
    ],
    resize_keyboard=True,
)

CANCEL_MENU = ReplyKeyboardMarkup([["Отмена"]], resize_keyboard=True)


# ---------------- Prompt message helpers ----------------

async def delete_previous_prompt(context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = context.user_data.get("prompt_chat_id")
    message_id = context.user_data.get("prompt_message_id")
    if not chat_id or not message_id:
        return
    try:
        await context.bot.delete_message(chat_id=chat_id, message_id=message_id)
    except Exception:
        pass
    context.user_data.pop("prompt_chat_id", None)
    context.user_data.pop("prompt_message_id", None)


async def send_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str, keyboard) -> None:
    await delete_previous_prompt(context)
    sent = await update.effective_message.reply_text(text, reply_markup=keyboard)
    context.user_data["prompt_chat_id"] = sent.chat_id
    context.user_data["prompt_message_id"] = sent.message_id


# ---------------- Report engine ----------------

REPORT_TYPES = {
    "План": {"title": "ПЛАН", "mode": "plan"},
    "Предварительный отчёт": {"title": "ПРЕДВАРИТЕЛЬНЫЙ ОТЧЁТ", "mode": "pred"},
    "Итоговый отчёт": {"title": "ИТОГОВЫЙ ОТЧЕТ", "mode": "final"},
}

REPORT_STEPS = {
    "plan": ["pzm", "psm", "pstl", "vstl", "dozh", "traffic", "kz"],
    "pred": ["pzm", "psm", "pstl", "vstl", "dozh", "traffic", "kz"],
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
    meta = REPORT_TYPES[report_key]
    context.user_data["mode"] = "report"
    context.user_data["report"] = {
        "title": meta["title"],
        "mode": meta["mode"],
        "date": current_report_date(),
        "step_order": REPORT_STEPS[meta["mode"]],
        "step_index": 0,
        "values": {},
    }


def current_step(context: ContextTypes.DEFAULT_TYPE) -> str | None:
    report = context.user_data.get("report")
    if not report:
        return None
    idx = report["step_index"]
    order = report["step_order"]
    if idx >= len(order):
        return None
    return order[idx]


def advance_step(context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data["report"]["step_index"] += 1


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

    lines.extend(["", "Что нужно ввести сейчас:", prompt])
    return "\n".join(lines)


def build_report_text(user_id: int, report: Dict[str, Any]) -> str:
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


# ---------------- Settings engine ----------------

SETTINGS_FIELDS = {
    "Хештег сотрудника": "employee_hashtag",
    "Хештег города": "city_hashtag",
    "Упоминание": "mention",
    "Плановый трафик": "plan_traffic",
}

SETTINGS_PROMPTS = {
    "employee_hashtag": "Отправь новый хештег сотрудника, например #ГригорийСотников",
    "city_hashtag": "Отправь новый хештег города, например #СПБ",
    "mention": "Отправь новое упоминание, например @AleksandrSmirnov21",
    "plan_traffic": "Отправь плановый трафик в формате Ч:ММ:СС или ЧЧ:ММ:СС, например 4:00:00",
}


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
    get_user_settings(update.effective_user.id)
    context.user_data.clear()
    text = (
        "Привет 👋\n\n"
        "Этот бот помогает быстро формировать отчёты.\n\n"
        "Сначала зайди в «Настройки» и заполни данные один раз.\n"
        "Потом выбирай нужный тип отчёта кнопками ниже."
    )
    await send_prompt(update, context, text, BOTTOM_MENU)


async def text_router(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.message.text:
        return

    user = update.effective_user
    text = normalize_text(update.message.text)
    mode = context.user_data.get("mode")

    # Main menu entry
    if text in REPORT_TYPES:
        start_report(context, text)
        report = context.user_data["report"]
        step = current_step(context)
        await send_prompt(update, context, build_progress_text(user.id, report, STEP_PROMPTS[step]), CANCEL_MENU)
        return

    if text == "Настройки":
        context.user_data["mode"] = "settings"
        context.user_data["settings_field"] = None
        await send_prompt(update, context, build_settings_text(user.id), SETTINGS_MENU)
        return

    # Settings mode
    if mode == "settings":
        if text == "Отмена":
            context.user_data.clear()
            await send_prompt(update, context, "Отменил. Выбери действие кнопками ниже.", BOTTOM_MENU)
            return

        if text == "✅ Готово":
            context.user_data.clear()
            await send_prompt(update, context, "Сохранил и вернул в меню.", BOTTOM_MENU)
            return

        if text == "Показать настройки":
            await send_prompt(update, context, build_settings_text(user.id), SETTINGS_MENU)
            return

        if text in SETTINGS_FIELDS:
            field = SETTINGS_FIELDS[text]
            context.user_data["settings_field"] = field
            await send_prompt(update, context, SETTINGS_PROMPTS[field], CANCEL_MENU)
            return

        field = context.user_data.get("settings_field")
        if field:
            if field == "plan_traffic":
                normalized = normalize_time_hms(text)
                if not normalized:
                    await send_prompt(update, context, "Нужен формат Ч:ММ:СС или ЧЧ:ММ:СС, например 4:00:00", CANCEL_MENU)
                    return
                text = normalized

            settings = get_user_settings(user.id)
            settings[field] = text
            USER_SETTINGS[str(user.id)] = settings
            save_settings()
            context.user_data["settings_field"] = None

            await send_prompt(
                update,
                context,
                build_settings_text(user.id, "Сохранено. Выбери следующий пункт или нажми ✅ Готово."),
                SETTINGS_MENU,
            )
            return

        await send_prompt(update, context, build_settings_text(user.id), SETTINGS_MENU)
        return

    # Report mode
    if mode == "report":
        if text == "Отмена":
            context.user_data.clear()
            await send_prompt(update, context, "Отменил. Выбери действие кнопками ниже.", BOTTOM_MENU)
            return

        report = context.user_data.get("report")
        if not report:
            context.user_data.clear()
            await send_prompt(update, context, "Что-то пошло не так. Выбери действие кнопками ниже.", BOTTOM_MENU)
            return

        step = current_step(context)
        if not step:
            context.user_data.clear()
            await send_prompt(update, context, "Что-то пошло не так. Выбери действие кнопками ниже.", BOTTOM_MENU)
            return

        if step == "traffic_fact":
            normalized = normalize_time_hms(text)
            if not normalized:
                await send_prompt(
                    update, context,
                    build_progress_text(user.id, report, "Нужен формат Ч:ММ:СС или ЧЧ:ММ:СС, например 3:20:28"),
                    CANCEL_MENU,
                )
                return
            text = normalized

        report["values"][step] = text
        advance_step(context)

        next_step = current_step(context)
        if next_step is None:
            final_text = build_report_text(user.id, report)
            context.user_data.clear()
            await update.message.reply_text(final_text, reply_markup=BOTTOM_MENU)
            await send_prompt(update, context, "Готово. Выбери следующее действие кнопками ниже.", BOTTOM_MENU)
            return

        await send_prompt(update, context, build_progress_text(user.id, report, STEP_PROMPTS[next_step]), CANCEL_MENU)
        return

    # default fallback
    await send_prompt(update, context, "Выбери действие кнопками ниже.", BOTTOM_MENU)


def build_application():
    token = os.environ.get("BOT_TOKEN")
    if not token:
        raise RuntimeError("BOT_TOKEN не задан.")
    app = ApplicationBuilder().token(token).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_router))
    return app


def main():
    app = build_application()
    logger.info("Бот запущен. Data dir: %s", DATA_DIR)
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
