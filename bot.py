
import os
import json
import re
from datetime import datetime
from telegram import Update, ReplyKeyboardMarkup
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters, ContextTypes, ConversationHandler

BOT_TOKEN = os.environ.get("BOT_TOKEN")

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN не задан.")

DATA_FILE = "data.json"

MAIN_MENU = ReplyKeyboardMarkup(
    [["План", "Предварительный отчёт"], ["Итоговый отчёт", "Настройки"]],
    resize_keyboard=True
)

SETTINGS_MENU = ReplyKeyboardMarkup(
    [["Хештег сотрудника", "Хештег города"],
     ["Упоминание", "Плановый трафик"],
     ["Отмена"]],
    resize_keyboard=True
)

CANCEL_MENU = ReplyKeyboardMarkup([["Отмена"]], resize_keyboard=True)

SETTINGS_SELECT, SETTINGS_INPUT = range(2)

def load_data():
    if not os.path.exists(DATA_FILE):
        return {}
    with open(DATA_FILE, "r") as f:
        return json.load(f)

def save_data(data):
    with open(DATA_FILE, "w") as f:
        json.dump(data, f)

USER_SETTINGS = load_data()

def get_user_settings(user_id):
    return USER_SETTINGS.get(str(user_id), {
        "employee_hashtag": "",
        "city_hashtag": "",
        "mention": "",
        "plan_traffic": "04:00:00"
    })

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = """Привет 👋

Этот бот помогает быстро формировать отчёты.

Как начать:

1. Нажми «Настройки»
2. Заполни:
- хештег сотрудника (например #ГригорийСотников)
- хештег города (например #СПБ)
- упоминание (например @username)
- плановый трафик (например 04:00:00)

⚠️ Это делается один раз

После этого:

— «План» → утренний отчёт  
— «Предварительный отчёт» → дневной  
— «Итоговый отчёт» → вечерний  

Просто вводи цифры и копируй результат."""
    await update.message.reply_text(text, reply_markup=MAIN_MENU)

async def settings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Выбери настройку:", reply_markup=SETTINGS_MENU)
    return SETTINGS_SELECT

async def settings_select(update: Update, context: ContextTypes.DEFAULT_TYPE):
    mapping = {
        "Хештег сотрудника": "employee_hashtag",
        "Хештег города": "city_hashtag",
        "Упоминание": "mention",
        "Плановый трафик": "plan_traffic"
    }
    text = update.message.text
    if text == "Отмена":
        return await cancel(update, context)

    context.user_data["field"] = mapping[text]
    await update.message.reply_text("Введи значение:", reply_markup=CANCEL_MENU)
    return SETTINGS_INPUT

async def settings_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.text == "Отмена":
        return await cancel(update, context)

    user_id = update.effective_user.id
    field = context.user_data.get("field")

    settings = get_user_settings(user_id)

    if field == "plan_traffic":
        if not re.fullmatch(r"\d{2}:\d{2}:\d{2}", update.message.text):
            await update.message.reply_text("Формат ЧЧ:ММ:СС", reply_markup=CANCEL_MENU)
            return SETTINGS_INPUT

    settings[field] = update.message.text
    USER_SETTINGS[str(user_id)] = settings
    save_data(USER_SETTINGS)

    await update.message.reply_text("Сохранено. Возвращаю в меню.", reply_markup=MAIN_MENU)
    return ConversationHandler.END

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Отмена", reply_markup=MAIN_MENU)
    return ConversationHandler.END

def main():
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    conv = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex("^Настройки$"), settings)],
        states={
            SETTINGS_SELECT: [MessageHandler(filters.TEXT & ~filters.COMMAND, settings_select)],
            SETTINGS_INPUT: [MessageHandler(filters.TEXT & ~filters.COMMAND, settings_input)],
        },
        fallbacks=[MessageHandler(filters.Regex("^Отмена$"), cancel)]
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(conv)

    print("Бот запущен")
    app.run_polling()

if __name__ == "__main__":
    main()
