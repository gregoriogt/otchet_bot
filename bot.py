"""
Telegram bot for generating daily sales reports with buttons and per-user settings.

Features:
- Main menu with buttons: План / Предварительный отчёт / Итоговый отчёт / Настройки
- Per-user settings stored in JSON
- Current date inserted automatically
- Report templates for three report types
- Settings stay open until user presses ✅ Готово
- Flexible time input for plan traffic: H:MM:SS or HH:MM:SS

Env vars:
- BOT_TOKEN (required)
- APP_TIMEZONE (optional, default: Europe/Moscow)
- APP_DATA_DIR (optional)
- RAILWAY_VOLUME_MOUNT_PATH (optional, auto-used if present)
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
    ConversationHandler,
    MessageHandler,
    filters,
)
import zoneinfo

logging.basicConfig(
    format="%(asctime)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# -------- Storage --------

def get_data_dir() -> Path:
    candidates = [
        os.environ.get("APP_DATA_DIR"),
        os.environ.get("RAILWAY_VOLUME_MOUNT_PATH"),
    ]
    for candidate in candidates:
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
        if isinstance(data, dict):
            return data
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


# -------- UI --------

MAIN_MENU = ReplyKeyboardMarkup(
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

# -------- States --------

(
    SETTINGS_SELECT,
    SETTINGS_INPUT,
    REPORT_PZM,
    REPORT_PSM,
    REPORT_PSTL,
    REPORT_VSTL,
    REPORT_DOZH,
    REPORT_TRAFFIC,
    REPORT_KZ,
    REPORT_ARRIVAL,
    REPORT_DEPARTURE,
) = range(11)


# -------- Report helpers --------

REPORT_TYPES = {
    "План": {"title": "ПЛАН", "mode": "plan"},
    "Предварительный отчёт": {"title": "ПРЕДВАРИТЕЛЬНЫЙ ОТЧЁТ", "mode": "pred"},
    "Итоговый отчёт": {"title": "ИТОГОВЫЙ ОТЧЕТ", "mode": "final"},
}


def app_now() -> datetime:
    tz_name = os.environ.get("APP_TIMEZONE", "Europe/Moscow")
    try:
        tz = zoneinfo.ZoneInfo(tz_name)
    except Exception:
        tz = zoneinfo.ZoneInfo("Europe/Moscow")
    return datetime.now(tz)


def current_report_date() -> str:
    return app_now().strftime("%d.%m.%y")


def normalize_time_hms(value: str) -> str | None:
    value = value.strip()
    if not re.fullmatch(r"\d{1,2}:\d{2}:\d{2}", value):
        return None
    hh, mm, ss = value.split(":")
    return f"{int(hh):02d}:{mm}:{ss}"


def init_report(context: ContextTypes.DEFAULT_TYPE, report_key: str) -> None:
    meta = REPORT_TYPES[report_key]
    context.user_data["report"] = {
        "report_key": report_key,
        "title": meta["title"],
        "mode": meta["mode"],
        "date": current_report_date(),
    }


def get_report(context: ContextTypes.DEFAULT_TYPE) -> Dict[str, Any]:
    return context.user_data["report"]


def build_report_text(user_id: int, report: Dict[str, Any]) -> str:
    settings = get_user_settings(user_id)
    lines = [
        f"1. {report['title']} {report['date']}",
        "",
        f"1 ПЗМ {report['pzm']}",
        f"2 ПСМ {report['psm']}",
        f"3 ПСТЛ {report['pstl']}",
        f"4 ВСТЛ {report['vstl']}",
        f"5 ДОЖ {report['dozh']}",
        "",
    ]

    if report["mode"] == "final":
        lines.extend([
            f"Трафик: {report['traffic_fact']} / {settings['plan_traffic']}",
            f"КЗ: {report['kz']}",
            "",
            f"Приход: {report['arrival']}",
            f"Уход: {report['departure']}",
            "",
        ])
    else:
        lines.extend([
            f"Трафик: {report['traffic']}",
            f"КЗ: {report['kz']}",
            "",
        ])

    lines.extend([
        settings["employee_hashtag"],
        settings["city_hashtag"],
        settings["mention"],
    ])

    return "\n".join(lines)


# -------- Generic helpers --------

async def safe_reply(update: Update, text: str, reply_markup=None) -> None:
    if update.message:
        await update.message.reply_text(text, reply_markup=reply_markup)


def normalize_text(value: str) -> str:
    return " ".join(value.strip().split())


async def cancel_flow(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.pop("report", None)
    context.user_data.pop("settings_field", None)
    await safe_reply(update, "Ок, отменил. Возвращаю в меню.", reply_markup=MAIN_MENU)
    return ConversationHandler.END


# -------- Main menu --------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if user:
        get_user_settings(user.id)
    text = (
        "Привет 👋\n\n"
        "Этот бот помогает быстро формировать отчёты.\n\n"
        "Как начать:\n\n"
        "1. Нажми «Настройки»\n"
        "2. Заполни:\n"
        "- хештег сотрудника (например #ГригорийСотников)\n"
        "- хештег города (например #СПБ)\n"
        "- упоминание (например @username)\n"
        "- плановый трафик (например 04:00:00)\n\n"
        "⚠️ Это делается один раз\n\n"
        "После этого:\n\n"
        "— «План» → утренний отчёт\n"
        "— «Предварительный отчёт» → дневной\n"
        "— «Итоговый отчёт» → вечерний\n\n"
        "Бот сам:\n"
        "- подставит дату\n"
        "- соберёт текст\n"
        "- оформит отчёт\n\n"
        "Просто вводи цифры по шагам и копируй готовый результат."
    )
    await safe_reply(update, text, reply_markup=MAIN_MENU)


async def menu_router(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int | None:
    if not update.message or not update.message.text:
        return None

    text = normalize_text(update.message.text)

    if text in REPORT_TYPES:
        init_report(context, text)
        await safe_reply(update, "Введи значение для 1 ПЗМ", reply_markup=CANCEL_MENU)
        return REPORT_PZM

    if text == "Настройки":
        user = update.effective_user
        if not user:
            await safe_reply(update, "Не удалось определить пользователя.", reply_markup=MAIN_MENU)
            return ConversationHandler.END
        settings = get_user_settings(user.id)
        preview = (
            "Текущие настройки:\n\n"
            f"Хештег сотрудника: {settings['employee_hashtag']}\n"
            f"Хештег города: {settings['city_hashtag']}\n"
            f"Упоминание: {settings['mention']}\n"
            f"Плановый трафик: {settings['plan_traffic']}"
        )
        await safe_reply(update, preview, reply_markup=SETTINGS_MENU)
        return SETTINGS_SELECT

    await safe_reply(update, "Нажми одну из кнопок меню.", reply_markup=MAIN_MENU)
    return ConversationHandler.END


# -------- Settings flow --------

SETTINGS_FIELDS = {
    "Хештег сотрудника": ("employee_hashtag", "Отправь новый хештег сотрудника, например #ГригорийСотников"),
    "Хештег города": ("city_hashtag", "Отправь новый хештег города, например #СПБ"),
    "Упоминание": ("mention", "Отправь новое упоминание, например @AleksandrSmirnov21"),
    "Плановый трафик": ("plan_traffic", "Отправь плановый трафик в формате Ч:ММ:СС или ЧЧ:ММ:СС, например 4:00:00"),
}

async def settings_select(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not update.message or not update.message.text:
        return SETTINGS_SELECT

    text = normalize_text(update.message.text)
    user = update.effective_user
    if not user:
        await safe_reply(update, "Не удалось определить пользователя.", reply_markup=MAIN_MENU)
        return ConversationHandler.END

    if text == "Отмена":
        return await cancel_flow(update, context)

    if text == "✅ Готово":
        await safe_reply(update, "Сохранил и вернул в меню.", reply_markup=MAIN_MENU)
        return ConversationHandler.END

    if text == "Показать настройки":
        settings = get_user_settings(user.id)
        preview = (
            "Текущие настройки:\n\n"
            f"Хештег сотрудника: {settings['employee_hashtag']}\n"
            f"Хештег города: {settings['city_hashtag']}\n"
            f"Упоминание: {settings['mention']}\n"
            f"Плановый трафик: {settings['plan_traffic']}"
        )
        await safe_reply(update, preview, reply_markup=SETTINGS_MENU)
        return SETTINGS_SELECT

    if text not in SETTINGS_FIELDS:
        await safe_reply(update, "Выбери одну из кнопок в настройках.", reply_markup=SETTINGS_MENU)
        return SETTINGS_SELECT

    field_key, prompt = SETTINGS_FIELDS[text]
    context.user_data["settings_field"] = field_key
    await safe_reply(update, prompt, reply_markup=CANCEL_MENU)
    return SETTINGS_INPUT


async def settings_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not update.message or not update.message.text:
        return SETTINGS_INPUT

    text = normalize_text(update.message.text)
    if text == "Отмена":
        await safe_reply(update, "Вернул в настройки.", reply_markup=SETTINGS_MENU)
        return SETTINGS_SELECT

    field_key = context.user_data.get("settings_field")
    user = update.effective_user
    if not user or not field_key:
        await safe_reply(update, "Что-то пошло не так. Возвращаю в меню.", reply_markup=MAIN_MENU)
        return ConversationHandler.END

    settings = get_user_settings(user.id)

    if field_key == "plan_traffic":
        normalized = normalize_time_hms(text)
        if not normalized:
            await safe_reply(update, "Нужен формат Ч:ММ:СС или ЧЧ:ММ:СС, например 4:00:00", reply_markup=CANCEL_MENU)
            return SETTINGS_INPUT
        text = normalized

    settings[field_key] = text
    USER_SETTINGS[str(user.id)] = settings
    save_settings()

    context.user_data.pop("settings_field", None)

    await safe_reply(update, "Сохранено. Можешь изменить ещё что-то или нажать ✅ Готово.", reply_markup=SETTINGS_MENU)
    return SETTINGS_SELECT


# -------- Report flow --------

async def report_pzm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = normalize_text(update.message.text)
    if text == "Отмена":
        return await cancel_flow(update, context)
    get_report(context)["pzm"] = text
    await safe_reply(update, "Введи значение для 2 ПСМ", reply_markup=CANCEL_MENU)
    return REPORT_PSM


async def report_psm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = normalize_text(update.message.text)
    if text == "Отмена":
        return await cancel_flow(update, context)
    get_report(context)["psm"] = text
    await safe_reply(update, "Введи значение для 3 ПСТЛ", reply_markup=CANCEL_MENU)
    return REPORT_PSTL


async def report_pstl(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = normalize_text(update.message.text)
    if text == "Отмена":
        return await cancel_flow(update, context)
    get_report(context)["pstl"] = text
    await safe_reply(update, "Введи значение для 4 ВСТЛ", reply_markup=CANCEL_MENU)
    return REPORT_VSTL


async def report_vstl(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = normalize_text(update.message.text)
    if text == "Отмена":
        return await cancel_flow(update, context)
    get_report(context)["vstl"] = text
    await safe_reply(update, "Введи значение для 5 ДОЖ", reply_markup=CANCEL_MENU)
    return REPORT_DOZH


async def report_dozh(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = normalize_text(update.message.text)
    if text == "Отмена":
        return await cancel_flow(update, context)
    get_report(context)["dozh"] = text

    mode = get_report(context)["mode"]
    if mode == "final":
        settings = get_user_settings(update.effective_user.id)
        await safe_reply(
            update,
            f"Введи фактический трафик в формате Ч:ММ:СС или ЧЧ:ММ:СС.\nПлановый трафик сейчас: {settings['plan_traffic']}",
            reply_markup=CANCEL_MENU,
        )
    else:
        await safe_reply(update, "Введи трафик", reply_markup=CANCEL_MENU)
    return REPORT_TRAFFIC


async def report_traffic(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = normalize_text(update.message.text)
    if text == "Отмена":
        return await cancel_flow(update, context)

    report = get_report(context)
    if report["mode"] == "final":
        normalized = normalize_time_hms(text)
        if not normalized:
            await safe_reply(update, "Нужен формат Ч:ММ:СС или ЧЧ:ММ:СС, например 3:20:28", reply_markup=CANCEL_MENU)
            return REPORT_TRAFFIC
        report["traffic_fact"] = normalized
    else:
        report["traffic"] = text

    await safe_reply(update, "Введи КЗ", reply_markup=CANCEL_MENU)
    return REPORT_KZ


async def report_kz(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = normalize_text(update.message.text)
    if text == "Отмена":
        return await cancel_flow(update, context)

    report = get_report(context)
    report["kz"] = text

    if report["mode"] == "final":
        await safe_reply(update, "Введи время прихода, например 8:25", reply_markup=CANCEL_MENU)
        return REPORT_ARRIVAL

    final_text = build_report_text(update.effective_user.id, report)
    context.user_data.pop("report", None)
    await safe_reply(update, final_text, reply_markup=MAIN_MENU)
    return ConversationHandler.END


async def report_arrival(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = normalize_text(update.message.text)
    if text == "Отмена":
        return await cancel_flow(update, context)
    get_report(context)["arrival"] = text
    await safe_reply(update, "Введи время ухода, например 20:30", reply_markup=CANCEL_MENU)
    return REPORT_DEPARTURE


async def report_departure(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = normalize_text(update.message.text)
    if text == "Отмена":
        return await cancel_flow(update, context)
    report = get_report(context)
    report["departure"] = text

    final_text = build_report_text(update.effective_user.id, report)
    context.user_data.pop("report", None)
    await safe_reply(update, final_text, reply_markup=MAIN_MENU)
    return ConversationHandler.END


# -------- App bootstrap --------

def build_application():
    token = os.environ.get("BOT_TOKEN")
    if not token:
        raise RuntimeError("BOT_TOKEN не задан.")

    app = ApplicationBuilder().token(token).build()

    report_conv = ConversationHandler(
        entry_points=[
            MessageHandler(filters.Regex("^(План|Предварительный отчёт|Итоговый отчёт|Настройки)$"), menu_router),
        ],
        states={
            SETTINGS_SELECT: [MessageHandler(filters.TEXT & ~filters.COMMAND, settings_select)],
            SETTINGS_INPUT: [MessageHandler(filters.TEXT & ~filters.COMMAND, settings_input)],
            REPORT_PZM: [MessageHandler(filters.TEXT & ~filters.COMMAND, report_pzm)],
            REPORT_PSM: [MessageHandler(filters.TEXT & ~filters.COMMAND, report_psm)],
            REPORT_PSTL: [MessageHandler(filters.TEXT & ~filters.COMMAND, report_pstl)],
            REPORT_VSTL: [MessageHandler(filters.TEXT & ~filters.COMMAND, report_vstl)],
            REPORT_DOZH: [MessageHandler(filters.TEXT & ~filters.COMMAND, report_dozh)],
            REPORT_TRAFFIC: [MessageHandler(filters.TEXT & ~filters.COMMAND, report_traffic)],
            REPORT_KZ: [MessageHandler(filters.TEXT & ~filters.COMMAND, report_kz)],
            REPORT_ARRIVAL: [MessageHandler(filters.TEXT & ~filters.COMMAND, report_arrival)],
            REPORT_DEPARTURE: [MessageHandler(filters.TEXT & ~filters.COMMAND, report_departure)],
        },
        fallbacks=[MessageHandler(filters.Regex("^Отмена$"), cancel_flow)],
        allow_reentry=True,
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(report_conv)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, menu_router))

    return app


def main() -> None:
    app = build_application()
    logger.info("Бот запущен. Data dir: %s", DATA_DIR)
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
