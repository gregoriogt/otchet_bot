"""
Telegram bot for generating daily sales reports.

UX:
- Main menu stays simple
- Settings are filled sequentially, like report flow
- Plan is built instantly from per-user constants
- In pred/final reports, fields whose plan value is "0" are skipped
- Helper prompt message is replaced each step to keep chat cleaner

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
from typing import Any, Dict, List

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
    "plan_pzm": "4",
    "plan_psm": "2",
    "plan_pstl": "0",
    "plan_vstl": "0",
    "plan_dozh": "0",
    "plan_traffic": "03:00:00",
    "plan_kz": "200",
}

SETTINGS_ORDER = [
    "employee_hashtag",
    "city_hashtag",
    "mention",
    "plan_pzm",
    "plan_psm",
    "plan_pstl",
    "plan_vstl",
    "plan_dozh",
    "plan_traffic",
    "plan_kz",
]

SETTINGS_LABELS = {
    "employee_hashtag": "Хештег сотрудника",
    "city_hashtag": "Хештег города",
    "mention": "Упоминание",
    "plan_pzm": "План ПЗМ",
    "plan_psm": "План ПСМ",
    "plan_pstl": "План ПСТЛ",
    "plan_vstl": "План ВСТЛ",
    "plan_dozh": "План ДОЖ",
    "plan_traffic": "План трафик",
    "plan_kz": "План КЗ",
}

SETTINGS_PROMPTS = {
    "employee_hashtag": "Введи хештег сотрудника, например #ГригорийСотников",
    "city_hashtag": "Введи хештег города, например #СПБ",
    "mention": "Введи упоминание, например @AleksandrSmirnov21",
    "plan_pzm": "Введи плановое значение для 1 ПЗМ",
    "plan_psm": "Введи плановое значение для 2 ПСМ",
    "plan_pstl": "Введи плановое значение для 3 ПСТЛ",
    "plan_vstl": "Введи плановое значение для 4 ВСТЛ",
    "plan_dozh": "Введи плановое значение для 5 ДОЖ",
    "plan_traffic": "Введи плановый трафик в формате Ч:ММ:СС или ЧЧ:ММ:СС, например 3:00:00",
    "plan_kz": "Введи плановое значение КЗ",
}


def merge_with_defaults(data: Dict[str, Dict[str, str]]) -> Dict[str, Dict[str, str]]:
    merged: Dict[str, Dict[str, str]] = {}
    for user_id, settings in data.items():
        current = deepcopy(DEFAULT_SETTINGS)
        if isinstance(settings, dict):
            current.update(settings)
        merged[user_id] = current
    return merged


def load_settings() -> Dict[str, Dict[str, str]]:
    if not SETTINGS_PATH.exists():
        return {}
    try:
        with SETTINGS_PATH.open("r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            return merge_with_defaults(data)
        return {}
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
    else:
        merged = deepcopy(DEFAULT_SETTINGS)
        merged.update(USER_SETTINGS[key])
        USER_SETTINGS[key] = merged
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

CANCEL_MENU = ReplyKeyboardMarkup([["Отмена"]], resize_keyboard=True)


# ---------------- Prompt helpers ----------------

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


# ---------------- Helpers ----------------

def is_zero_value(value: str) -> bool:
    return normalize_text(str(value)) in {"0", "0.0", "00", "000"}


def field_enabled_for_reports(settings: Dict[str, str], field: str) -> bool:
    mapping = {
        "pzm": "plan_pzm",
        "psm": "plan_psm",
        "pstl": "plan_pstl",
        "vstl": "plan_vstl",
        "dozh": "plan_dozh",
    }
    if field not in mapping:
        return True
    return not is_zero_value(settings.get(mapping[field], "0"))


# ---------------- Settings flow ----------------

def start_settings_flow(context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data["mode"] = "settings"
    context.user_data["settings_index"] = 0


def current_settings_field(context: ContextTypes.DEFAULT_TYPE) -> str | None:
    idx = context.user_data.get("settings_index", 0)
    if idx >= len(SETTINGS_ORDER):
        return None
    return SETTINGS_ORDER[idx]


def advance_settings_field(context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data["settings_index"] = context.user_data.get("settings_index", 0) + 1


def build_settings_summary(user_id: int, extra: str | None = None) -> str:
    s = get_user_settings(user_id)
    lines = [
        "Настройки сохранены:",
        "",
        f"Хештег сотрудника: {s['employee_hashtag']}",
        f"Хештег города: {s['city_hashtag']}",
        f"Упоминание: {s['mention']}",
        "",
        f"План ПЗМ: {s['plan_pzm']}",
        f"План ПСМ: {s['plan_psm']}",
        f"План ПСТЛ: {s['plan_pstl']}",
        f"План ВСТЛ: {s['plan_vstl']}",
        f"План ДОЖ: {s['plan_dozh']}",
        f"План трафик: {s['plan_traffic']}",
        f"План КЗ: {s['plan_kz']}",
    ]
    if extra:
        lines.extend(["", extra])
    return "\n".join(lines)


# ---------------- Report flow ----------------

BASE_REPORT_STEPS = {
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


def build_step_order_for_user(user_id: int, mode: str) -> List[str]:
    settings = get_user_settings(user_id)
    order = []
    for field in BASE_REPORT_STEPS[mode]:
        if field in {"pzm", "psm", "pstl", "vstl", "dozh"}:
            if field_enabled_for_reports(settings, field):
                order.append(field)
        else:
            order.append(field)
    return order


def start_report(context: ContextTypes.DEFAULT_TYPE, user_id: int, mode: str, title: str) -> None:
    context.user_data["mode"] = "report"
    context.user_data["report"] = {
        "title": title,
        "mode": mode,
        "date": current_report_date(),
        "step_order": build_step_order_for_user(user_id, mode),
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

    lines = [f"{report['title']} {report['date']}", "", "Уже введено:"]

    shown_any = False
    for field, label in [("pzm", "1 ПЗМ"), ("psm", "2 ПСМ"), ("pstl", "3 ПСТЛ"), ("vstl", "4 ВСТЛ"), ("dozh", "5 ДОЖ")]:
        if field in report["step_order"]:
            lines.append(f"{label}: {vals.get(field, '—')}")
            shown_any = True

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


def build_plan_text(user_id: int) -> str:
    s = get_user_settings(user_id)
    lines = [
        f"ПЛАН {current_report_date()}",
        "",
        f"1 ПЗМ {s['plan_pzm']}",
        f"2 ПСМ {s['plan_psm']}",
        f"3 ПСТЛ {s['plan_pstl']}",
        f"4 ВСТЛ {s['plan_vstl']}",
        f"5 ДОЖ {s['plan_dozh']}",
        "",
        f"Трафик: {s['plan_traffic']}",
        f"КЗ: {s['plan_kz']}",
        "",
        s["employee_hashtag"],
        s["city_hashtag"],
        s["mention"],
    ]
    return "\n".join(lines)


def build_report_text(user_id: int, report: Dict[str, Any]) -> str:
    settings = get_user_settings(user_id)
    vals = report["values"]

    lines = [f"{report['title']} {report['date']}", ""]

    for field, label in [("pzm", "1 ПЗМ"), ("psm", "2 ПСМ"), ("pstl", "3 ПСТЛ"), ("vstl", "4 ВСТЛ"), ("dozh", "5 ДОЖ")]:
        if field in report["step_order"]:
            lines.append(f"{label} {vals.get(field, '')}")

    lines.append("")

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


# ---------------- Handlers ----------------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    get_user_settings(update.effective_user.id)
    save_settings()
    context.user_data.clear()
    text = (
        "Привет 👋\n\n"
        "Этот бот помогает быстро формировать отчёты.\n\n"
        "Сначала зайди в «Настройки» и один раз заполни свои константы.\n"
        "После этого кнопка «План» будет сразу собирать готовый утренний отчёт.\n"
        "Предварительный и итоговый отчёты остаются пошаговыми.\n\n"
        "Если в плане у какого-то блока стоит 0, в остальных отчётах бот этот блок не спросит."
    )
    await send_prompt(update, context, text, BOTTOM_MENU)


async def text_router(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.message.text:
        return

    user = update.effective_user
    get_user_settings(user.id)
    save_settings()

    text = normalize_text(update.message.text)
    mode = context.user_data.get("mode")

    if text == "План":
        await delete_previous_prompt(context)
        await update.message.reply_text(build_plan_text(user.id), reply_markup=BOTTOM_MENU)
        await send_prompt(update, context, "Готово. Выбери следующее действие кнопками ниже.", BOTTOM_MENU)
        return

    if text == "Предварительный отчёт":
        start_report(context, user.id, "pred", "ПРЕДВАРИТЕЛЬНЫЙ ОТЧЁТ")
        report = context.user_data["report"]
        step = current_step(context)
        await send_prompt(update, context, build_progress_text(user.id, report, STEP_PROMPTS[step]), CANCEL_MENU)
        return

    if text == "Итоговый отчёт":
        start_report(context, user.id, "final", "ИТОГОВЫЙ ОТЧЕТ")
        report = context.user_data["report"]
        step = current_step(context)
        await send_prompt(update, context, build_progress_text(user.id, report, STEP_PROMPTS[step]), CANCEL_MENU)
        return

    if text == "Настройки":
        start_settings_flow(context)
        field = current_settings_field(context)
        await send_prompt(update, context, SETTINGS_PROMPTS[field], CANCEL_MENU)
        return

    # Settings mode
    if mode == "settings":
        if text == "Отмена":
            context.user_data.clear()
            await send_prompt(update, context, "Отменил. Выбери действие кнопками ниже.", BOTTOM_MENU)
            return

        field = current_settings_field(context)
        if not field:
            context.user_data.clear()
            await send_prompt(update, context, build_settings_summary(user.id, "Сохранил и вернул в меню."), BOTTOM_MENU)
            return

        value = text
        if field == "plan_traffic":
            normalized = normalize_time_hms(text)
            if not normalized:
                await send_prompt(update, context, "Нужен формат Ч:ММ:СС или ЧЧ:ММ:СС, например 3:00:00", CANCEL_MENU)
                return
            value = normalized

        settings = get_user_settings(user.id)
        settings[field] = value
        USER_SETTINGS[str(user.id)] = settings
        save_settings()

        advance_settings_field(context)
        next_field = current_settings_field(context)
        if next_field is None:
            context.user_data.clear()
            await send_prompt(update, context, build_settings_summary(user.id, "Сохранил и вернул в меню."), BOTTOM_MENU)
            return

        await send_prompt(update, context, SETTINGS_PROMPTS[next_field], CANCEL_MENU)
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

        value = text
        if step == "traffic_fact":
            normalized = normalize_time_hms(text)
            if not normalized:
                await send_prompt(
                    update, context,
                    build_progress_text(user.id, report, "Нужен формат Ч:ММ:СС или ЧЧ:ММ:СС, например 3:20:28"),
                    CANCEL_MENU,
                )
                return
            value = normalized

        report["values"][step] = value
        advance_step(context)

        next_step = current_step(context)
        if next_step is None:
            final_text = build_report_text(user.id, report)
            context.user_data.clear()
            await delete_previous_prompt(context)
            await update.message.reply_text(final_text, reply_markup=BOTTOM_MENU)
            await send_prompt(update, context, "Готово. Выбери следующее действие кнопками ниже.", BOTTOM_MENU)
            return

        await send_prompt(update, context, build_progress_text(user.id, report, STEP_PROMPTS[next_step]), CANCEL_MENU)
        return

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
