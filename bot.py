
"""
Telegram bot for generating daily and call reports.

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


def normalize_time_hm(value: str) -> str | None:
    value = normalize_text(value)
    if not re.fullmatch(r"\d{1,2}:\d{2}", value):
        return None
    hh, mm = value.split(":")
    return f"{int(hh):02d}:{mm}"


# ---------------- Keyboards ----------------

MAIN_MENU = ReplyKeyboardMarkup(
    [
        ["Ежедневные отчёты", "Отчёт ПЗМ"],
        ["Отчёт ПСМ", "Настройки"],
    ],
    resize_keyboard=True,
)

DAILY_MENU = ReplyKeyboardMarkup(
    [
        ["План", "Предварительный отчёт"],
        ["Итоговый отчёт", "Назад в главное меню"],
    ],
    resize_keyboard=True,
)

NAV_MENU = ReplyKeyboardMarkup(
    [
        ["⬅️ Назад", "➡️ Вперёд"],
        ["Отмена"],
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


# ---------------- Daily report flow ----------------

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
            continue
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


def step_back(context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data["report"]["step_index"] = max(0, context.user_data["report"]["step_index"] - 1)


def build_progress_text(user_id: int, report: Dict[str, Any], prompt: str) -> str:
    settings = get_user_settings(user_id)
    vals = report["values"]
    lines = [f"{report['title']} {report['date']}", "", "Уже введено:"]

    for field, label in [("pzm", "1 ПЗМ"), ("psm", "2 ПСМ"), ("pstl", "3 ПСТЛ"), ("vstl", "4 ВСТЛ"), ("dozh", "5 ДОЖ")]:
        if field in report["step_order"]:
            lines.append(f"{label}: {vals.get(field, '—')}")

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
        "Что нужно ввести сейчас:",
        prompt,
        "",
        "Можно отправить ответ сообщением или использовать кнопки навигации.",
    ])
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


# ---------------- Call report flow ----------------

CALL_REPORT_TEMPLATES = {
    "call_pzm": {
        "title": "ПЗМ",
        "steps": [
            ("expertise", "1.ЭКСПЕРТНОСТЬ:"),
            ("request", "2.ЗАПРОС/ ОТВЕТ КЛИЕНТА, ЧТО ОН ХОЧЕТ ОТ НОВОГО БИЗНЕСА:"),
            ("money", "3.ДЕНЬГИ/ОТКУДА:"),
            ("urgency", "4.СРОЧНОСТЬ (суперсезон в рынке):"),
            ("reaction", "5.РЕАКЦИЯ КЛИЕНТА (ВОПРОСЫ И ВОЗРАЖЕНИЯ) И КАК Я ОТРАБОТАЛ(А):"),
            ("result", "6.ДОГОВОРЕННОСТЬ ПО ИТОГУ ПЗМ:"),
            ("comment", "комментарии:"),
        ],
    },
    "call_psm": {
        "title": "ПСМ",
        "steps": [
            ("lk", "1. ВЫСТРОЕН ЛИ ЛК ?"),
            ("tgk", "2. ПОДПИСАН НА ТГК ? СМОТРЕЛ ПРЯМЫЕ ЭФИРЫ ?"),
            ("lpr", "3. КТО ЛПР:"),
            ("pain", "4. БОЛЬ:"),
            ("request", "5. ЗАПРОС/ПОТРЕБНОСТЬ ОТВЕТ КЛИЕНТА, ЧТО ОН ХОЧЕТ ОТ НОВОГО БИЗНЕСА:"),
            ("demand", "6.1 СПРОСУ:"),
            ("strengths", "6.2 СИЛЬНЫЕ СТОРОНЫ БИЗНЕСА:"),
            ("finance", "6.3 ПОНИМАНИЕ ФИН МОДЕЛИ:"),
            ("money", "7. ДЕНЬГИ/ОТКУДА:"),
            ("urgency", "8. СРОЧНОСТЬ:"),
            ("reaction", "9.РЕАКЦИЯ КЛИЕНТА (ВОПРОСЫ И ВОЗРАЖЕНИЯ) И КАК Я ОТРАБОТАЛ(А):"),
            ("result", "10. ДОГОВОРЕННОСТЬ ПО ИТОГУ ПСМ:"),
            ("comment", "комменатарии:"),
        ],
    },
}


def start_call_report(context: ContextTypes.DEFAULT_TYPE, report_type: str) -> None:
    template = CALL_REPORT_TEMPLATES[report_type]
    context.user_data["mode"] = "call_report"
    context.user_data["call_report"] = {
        "report_type": report_type,
        "title": template["title"],
        "date": current_report_date(),
        "steps": template["steps"],
        "step_index": 0,
        "values": {},
    }


def current_call_step(context: ContextTypes.DEFAULT_TYPE):
    report = context.user_data.get("call_report")
    if not report:
        return None
    idx = report["step_index"]
    steps = report["steps"]
    if idx >= len(steps):
        return None
    return steps[idx]


def advance_call_step(context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data["call_report"]["step_index"] += 1


def step_back_call(context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data["call_report"]["step_index"] = max(0, context.user_data["call_report"]["step_index"] - 1)


def build_call_progress_text(report: Dict[str, Any]) -> str:
    vals = report["values"]
    idx = report["step_index"]
    _, label = report["steps"][idx]

    lines = [f"{report['title']} {report['date']}", "", "Уже введено:"]
    for existing_key, existing_label in report["steps"]:
        lines.append(f"• {existing_label} {vals.get(existing_key, '—')}")

    lines.extend([
        "",
        f"Текущий вопрос ({idx + 1}/{len(report['steps'])}):",
        label,
        "",
        "Напиши ответ сообщением или используй кнопки навигации.",
    ])
    return "\n".join(lines)


def build_call_report_text(report: Dict[str, Any]) -> str:
    vals = report["values"]

    if report["report_type"] == "call_pzm":
        lines = [
            "ПЗМ",
            f"1.ЭКСПЕРТНОСТЬ: {vals.get('expertise', '')}",
            f"2.ЗАПРОС/ ОТВЕТ КЛИЕНТА, ЧТО ОН ХОЧЕТ ОТ НОВОГО БИЗНЕСА: {vals.get('request', '')}",
            f"3.ДЕНЬГИ/ОТКУДА: {vals.get('money', '')}",
            f"4.СРОЧНОСТЬ (суперсезон в рынке): {vals.get('urgency', '')}",
            f"5.РЕАКЦИЯ КЛИЕНТА (ВОПРОСЫ И ВОЗРАЖЕНИЯ) И КАК Я ОТРАБОТАЛ(А): {vals.get('reaction', '')}",
            f"6.ДОГОВОРЕННОСТЬ ПО ИТОГУ ПЗМ: {vals.get('result', '')}",
            f"комментарии: {vals.get('comment', '')}",
        ]
        return "\n".join(lines)

    lines = [
        "ПСМ",
        f"1. ВЫСТРОЕН ЛИ ЛК ? {vals.get('lk', '')}",
        f"2. ПОДПИСАН НА ТГК ? СМОТРЕЛ ПРЯМЫЕ ЭФИРЫ ? {vals.get('tgk', '')}",
        f"3. КТО ЛПР: {vals.get('lpr', '')}",
        f"4. БОЛЬ: {vals.get('pain', '')}",
        f"5. ЗАПРОС/ПОТРЕБНОСТЬ ОТВЕТ КЛИЕНТА, ЧТО ОН ХОЧЕТ ОТ НОВОГО БИЗНЕСА: {vals.get('request', '')}",
        "6. СЛОЖИЛОСЬ ЛИ ПОНИМАНИЕ У КЛИЕНТА ПО",
        f"6.1 СПРОСУ {vals.get('demand', '')}",
        f"6.2 СИЛЬНЫЕ СТОРОНЫ БИЗНЕСА {vals.get('strengths', '')}",
        f"6.3 ПОНИМАНИЕ ФИН МОДЕЛИ {vals.get('finance', '')}",
        f"7. ДЕНЬГИ/ОТКУДА {vals.get('money', '')}",
        f"8. СРОЧНОСТЬ: {vals.get('urgency', '')}",
        f"9.РЕАКЦИЯ КЛИЕНТА (ВОПРОСЫ И ВОЗРАЖЕНИЯ) И КАК Я ОТРАБОТАЛ(А): {vals.get('reaction', '')}",
        f"10. ДОГОВОРЕННОСТЬ ПО ИТОГУ ПСМ: {vals.get('result', '')}",
        f"комменатарии: {vals.get('comment', '')}",
    ]
    return "\n".join(lines)


# ---------------- Handlers ----------------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    get_user_settings(update.effective_user.id)
    save_settings()
    context.user_data.clear()

    text = (
        "Привет\n\n"
        "Бот умеет собирать ежедневные отчёты, а также отдельные отчёты ПЗМ и ПСМ.\n\n"
        "Сначала один раз заполни настройки.\n"
        "Потом выбирай нужный режим в меню.\n\n"
        "В ежедневных отчётах блоки с планом 0 автоматически пропускаются."
    )
    await send_prompt(update, context, text, MAIN_MENU)


async def text_router(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.message.text:
        return

    user = update.effective_user
    get_user_settings(user.id)
    save_settings()

    text = normalize_text(update.message.text)
    mode = context.user_data.get("mode")

    if text == "Ежедневные отчёты":
        context.user_data.clear()
        await send_prompt(update, context, "Выбери тип ежедневного отчёта.", DAILY_MENU)
        return

    if text == "Назад в главное меню":
        context.user_data.clear()
        await send_prompt(update, context, "Вернул в главное меню.", MAIN_MENU)
        return

    if text == "План":
        await delete_previous_prompt(context)
        await update.message.reply_text(build_plan_text(user.id), reply_markup=DAILY_MENU)
        await send_prompt(update, context, "Готово. Выбери следующее действие кнопками ниже.", DAILY_MENU)
        return

    if text == "Предварительный отчёт":
        start_report(context, user.id, "pred", "ПРЕДВАРИТЕЛЬНЫЙ ОТЧЁТ")
        report = context.user_data["report"]
        step = current_step(context)
        await send_prompt(update, context, build_progress_text(user.id, report, STEP_PROMPTS[step]), NAV_MENU)
        return

    if text == "Итоговый отчёт":
        start_report(context, user.id, "final", "ИТОГОВЫЙ ОТЧЁТ")
        report = context.user_data["report"]
        step = current_step(context)
        await send_prompt(update, context, build_progress_text(user.id, report, STEP_PROMPTS[step]), NAV_MENU)
        return

    if text == "Отчёт ПЗМ":
        start_call_report(context, "call_pzm")
        report = context.user_data["call_report"]
        await send_prompt(update, context, build_call_progress_text(report), NAV_MENU)
        return

    if text == "Отчёт ПСМ":
        start_call_report(context, "call_psm")
        report = context.user_data["call_report"]
        await send_prompt(update, context, build_call_progress_text(report), NAV_MENU)
        return

    if text == "Настройки":
        start_settings_flow(context)
        field = current_settings_field(context)
        await send_prompt(update, context, SETTINGS_PROMPTS[field], CANCEL_MENU)
        return

    if mode == "settings":
        if text == "Отмена":
            context.user_data.clear()
            await send_prompt(update, context, "Отменил. Выбери действие кнопками ниже.", MAIN_MENU)
            return

        field = current_settings_field(context)
        if not field:
            context.user_data.clear()
            await send_prompt(update, context, build_settings_summary(user.id, "Сохранил и вернул в меню."), MAIN_MENU)
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
            await send_prompt(update, context, build_settings_summary(user.id, "Сохранил и вернул в меню."), MAIN_MENU)
            return

        await send_prompt(update, context, SETTINGS_PROMPTS[next_field], CANCEL_MENU)
        return

    if mode == "report":
        if text == "Отмена":
            context.user_data.clear()
            await send_prompt(update, context, "Отменил. Выбери действие кнопками ниже.", DAILY_MENU)
            return

        report = context.user_data.get("report")
        if not report:
            context.user_data.clear()
            await send_prompt(update, context, "Что-то пошло не так. Выбери действие кнопками ниже.", MAIN_MENU)
            return

        if text == "⬅️ Назад":
            step_back(context)
            step = current_step(context)
            if step is None and report["step_order"]:
                step = report["step_order"][0]
            if step is None:
                context.user_data.clear()
                await send_prompt(update, context, "Что-то пошло не так. Выбери действие кнопками ниже.", DAILY_MENU)
                return
            await send_prompt(update, context, build_progress_text(user.id, report, STEP_PROMPTS[step]), NAV_MENU)
            return

        if text == "➡️ Вперёд":
            advance_step(context)
            next_step = current_step(context)
            if next_step is None:
                final_text = build_report_text(user.id, report)
                context.user_data.clear()
                await delete_previous_prompt(context)
                await update.message.reply_text(final_text, reply_markup=DAILY_MENU)
                await send_prompt(update, context, "Готово. Выбери следующее действие кнопками ниже.", DAILY_MENU)
                return
            await send_prompt(update, context, build_progress_text(user.id, report, STEP_PROMPTS[next_step]), NAV_MENU)
            return

        step = current_step(context)
        if not step:
            context.user_data.clear()
            await send_prompt(update, context, "Что-то пошло не так. Выбери действие кнопками ниже.", MAIN_MENU)
            return

        value = text

        if step == "traffic_fact":
            normalized = normalize_time_hms(text)
            if not normalized:
                await send_prompt(
                    update,
                    context,
                    build_progress_text(user.id, report, "Нужен формат Ч:ММ:СС или ЧЧ:ММ:СС, например 3:20:28"),
                    NAV_MENU,
                )
                return
            value = normalized

        if step in {"arrival", "departure"}:
            normalized = normalize_time_hm(text)
            if not normalized:
                await send_prompt(
                    update,
                    context,
                    build_progress_text(user.id, report, "Нужен формат Ч:ММ, например 8:25"),
                    NAV_MENU,
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
            await update.message.reply_text(final_text, reply_markup=DAILY_MENU)
            await send_prompt(update, context, "Готово. Выбери следующее действие кнопками ниже.", DAILY_MENU)
            return

        await send_prompt(update, context, build_progress_text(user.id, report, STEP_PROMPTS[next_step]), NAV_MENU)
        return

    if mode == "call_report":
        if text == "Отмена":
            context.user_data.clear()
            await send_prompt(update, context, "Отменил. Выбери действие кнопками ниже.", MAIN_MENU)
            return

        report = context.user_data.get("call_report")
        if not report:
            context.user_data.clear()
            await send_prompt(update, context, "Что-то пошло не так. Выбери действие кнопками ниже.", MAIN_MENU)
            return

        if text == "⬅️ Назад":
            step_back_call(context)
            await send_prompt(update, context, build_call_progress_text(report), NAV_MENU)
            return

        if text == "➡️ Вперёд":
            advance_call_step(context)
            next_step = current_call_step(context)
            if next_step is None:
                final_text = build_call_report_text(report)
                context.user_data.clear()
                await delete_previous_prompt(context)
                await update.message.reply_text(final_text, reply_markup=MAIN_MENU)
                await send_prompt(update, context, "Готово. Выбери следующее действие кнопками ниже.", MAIN_MENU)
                return
            await send_prompt(update, context, build_call_progress_text(report), NAV_MENU)
            return

        step = current_call_step(context)
        if not step:
            context.user_data.clear()
            await send_prompt(update, context, "Что-то пошло не так. Выбери действие кнопками ниже.", MAIN_MENU)
            return

        step_key, _ = step
        report["values"][step_key] = text
        advance_call_step(context)

        next_step = current_call_step(context)
        if next_step is None:
            final_text = build_call_report_text(report)
            context.user_data.clear()
            await delete_previous_prompt(context)
            await update.message.reply_text(final_text, reply_markup=MAIN_MENU)
            await send_prompt(update, context, "Готово. Выбери следующее действие кнопками ниже.", MAIN_MENU)
            return

        await send_prompt(update, context, build_call_progress_text(report), NAV_MENU)
        return

    await send_prompt(update, context, "Выбери действие кнопками ниже.", MAIN_MENU)


def main() -> None:
    token = os.environ.get("BOT_TOKEN")
    if not token:
        raise RuntimeError("BOT_TOKEN is required")

    app = ApplicationBuilder().token(token).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_router))

    logger.info("Bot started")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
