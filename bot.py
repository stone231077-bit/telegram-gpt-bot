import os, json, logging, threading, time
from datetime import datetime
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder, CommandHandler, CallbackQueryHandler,
    ConversationHandler, MessageHandler, ContextTypes, filters,
)

# ---------- ЛОГИ ----------
logging.basicConfig(
    format="%(asctime)s %(name)s [%(levelname)s] %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("bot")

# ---------- НАСТРОЙКИ ----------
load_dotenv()  # локально возьмёт из .env; на Render переменные задаются в панели

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
if not TELEGRAM_TOKEN:
    raise RuntimeError("Нет TELEGRAM_TOKEN в .env/переменных окружения.")

# твой (ваш) Telegram ID — укажи здесь:
ADMIN_IDS = {981248855}  # <-- замени на свой numeric ID

NUM_SECTIONS = 7
DATA_FILE = "data.json"

# Рабочие часы / зона
WORK_TZ = os.getenv("WORK_TZ", "Europe/Paris")
WORK_START = int(os.getenv("WORK_START", "6"))   # включительно, по умолчанию 06:00
WORK_END   = int(os.getenv("WORK_END",   "22"))  # исключая, по умолчанию 22:00
OFF_MSG = os.getenv(
    "OFF_MSG",
    "⏰ Бот доступен в рабочее время: 06:00–22:00 (Europe/Paris). Попробуйте позже."
)
ADMIN_BYPASS = os.getenv("ADMIN_BYPASS", "1") in {"1", "true", "True"}  # админ может работать ночью

# Keep-alive (самопинг) — задай URL сервиса (например, https://my-bot.onrender.com/)
KEEPALIVE_URL = os.getenv("KEEPALIVE_URL", "").strip()
KEEPALIVE_EVERY_SEC = int(os.getenv("KEEPALIVE_EVERY_SEC", "300"))  # каждые 5 минут

# ---------- МИНИ HTTP-СЕРВЕР ДЛЯ RENDER ----------
# Render ждёт, что приложение слушает порт $PORT.
from flask import Flask
app_http = Flask(__name__)

@app_http.get("/")
def health():
    return "ok", 200

def run_http():
    port = int(os.environ.get("PORT", 10000))
    app_http.run(host="0.0.0.0", port=port)

def keepalive_loop():
    """Периодически пинаем внешний URL, чтобы хостинг не усыплял сервис."""
    if not KEEPALIVE_URL:
        log.info("KEEPALIVE_URL не задан — самопинг отключен.")
        return
    import requests
    log.info("Запущен keepalive-пинг: %s, каждые %s сек", KEEPALIVE_URL, KEEPALIVE_EVERY_SEC)
    while True:
        try:
            requests.get(KEEPALIVE_URL, timeout=10)
        except Exception as e:
            log.warning("Keepalive error: %s", e)
        time.sleep(KEEPALIVE_EVERY_SEC)

# ---------- ВСПОМОГАТЕЛЬНОЕ: ВРЕМЯ РАБОТЫ ----------
def is_admin(uid: int) -> bool:
    return uid in ADMIN_IDS

def is_work_time(dt: datetime | None = None) -> bool:
    """Возвращает True, если сейчас внутри интервала [WORK_START, WORK_END)."""
    tz = ZoneInfo(WORK_TZ)
    now = dt or datetime.now(tz)
    h = now.hour
    # поддержка «перелома суток», если вдруг кто-то захочет END < START
    if WORK_START <= WORK_END:
        return WORK_START <= h < WORK_END
    else:
        # напр. 22..24 и 0..6
        return h >= WORK_START or h < WORK_END

async def guard_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Пускать ли пользователя в обработчик сообщений."""
    uid = update.effective_user.id if update.effective_user else 0
    if is_work_time() or (ADMIN_BYPASS and is_admin(uid)):
        return True
    await update.message.reply_text(OFF_MSG)
    return False

async def guard_callback(q, uid: int) -> bool:
    """Пускать ли пользователя в обработчик callback-кнопок."""
    if is_work_time() or (ADMIN_BYPASS and is_admin(uid)):
        return True
    await q.answer(OFF_MSG, show_alert=True)
    return False

# ---------- СОСТОЯНИЯ ДИАЛОГА ----------
(
    CHOOSING_SECTION,
    SECTION_ACTION,
    EDIT_SECTION_TEXT,
    CHOOSE_SUB_FOR_EDIT,
    EDIT_SUB_TEXT,
    ADD_SUB_TITLE,
    ADD_SUB_TEXT,
    CHOOSE_SUB_FOR_DELETE,
    CONFIRM_DELETE_SUB,
) = range(9)

# ---------- ХРАНИЛИЩЕ ----------
def save_data(data: dict):
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

def load_data() -> dict:
    if not os.path.exists(DATA_FILE):
        titles = {
            "1": "📄 Документы",
            "2": "📌 В важное",
            "3": "📁 Документы Дениса",
            "4": "👋 Мой Вам привет",
            "5": "КуКу",
            "6": "Смешные Истории",
            "7": "Эротика",
        }
        texts = {str(i): "" for i in range(1, NUM_SECTIONS + 1)}
        data = {"titles": titles, "texts": texts, "subsections": {}}
        save_data(data)
        return data
    with open(DATA_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)
    data.setdefault("subsections", {})
    return data

DATA = load_data()

# ---------- ВСПОМОГАТЕЛЬНЫЕ КЛАВИАТУРЫ ----------
def sections_keyboard():
    rows = [[InlineKeyboardButton(f'{sid}. {DATA["titles"][sid]}', callback_data=f"sec:{sid}")]
            for sid in sorted(DATA["titles"], key=lambda x: int(x))]
    rows.append([InlineKeyboardButton("❌ Отмена", callback_data="cancel")])
    return InlineKeyboardMarkup(rows)

def section_actions_keyboard(sec_id: str):
    rows = [
        [InlineKeyboardButton("📝 Изменить текст раздела", callback_data=f"act:set_text:{sec_id}")],
        [InlineKeyboardButton("➕ Добавить подраздел", callback_data=f"act:add_sub:{sec_id}")],
        [InlineKeyboardButton("✏️ Редактировать подраздел", callback_data=f"act:edit_sub:{sec_id}")],
        [InlineKeyboardButton("🗑 Удалить подраздел", callback_data=f"act:del_sub:{sec_id}")],
        [InlineKeyboardButton("⬅️ К разделам", callback_data="back:sections"),
         InlineKeyboardButton("❌ Отмена", callback_data="cancel")],
    ]
    return InlineKeyboardMarkup(rows)

def subs_keyboard(sec_id: str, mode: str):
    subs = DATA["subsections"].get(sec_id, {})
    rows = [[InlineKeyboardButton(f'{sub_id}. {item["title"]}', callback_data=f"{mode}:{sec_id}:{sub_id}")]
            for sub_id, item in sorted(subs.items(), key=lambda x: int(x[0]))]
    if not rows:
        rows = [[InlineKeyboardButton("Подразделов нет", callback_data="noop")]]
    rows.append([InlineKeyboardButton("⬅️ Назад", callback_data=f"act_menu:{sec_id}")])
    return InlineKeyboardMarkup(rows)

def next_sub_id(sec_id: str) -> str:
    subs = DATA["subsections"].get(sec_id, {})
    return str(1 + max([int(i) for i in subs.keys()] or [0]))

# ---------- ПУБЛИЧНОЕ МЕНЮ ----------
def public_sections_keyboard():
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton(f'{sid}. {DATA["titles"][sid]}', callback_data=f"vsec:{sid}")]
         for sid in sorted(DATA["titles"], key=lambda x: int(x))]
    )

def public_subs_keyboard(sec_id: str):
    subs = DATA["subsections"].get(sec_id, {})
    if not subs:
        return InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Назад к меню", callback_data="vback")]])
    rows = [[InlineKeyboardButton(item["title"], callback_data=f"vsub:{sec_id}:{sub_id}")]
            for sub_id, item in sorted(subs.items(), key=lambda x: int(x[0]))]
    rows.append([InlineKeyboardButton("⬅️ Назад к меню", callback_data="vback")])
    return InlineKeyboardMarkup(rows)

# ---------- ОБРАБОТЧИКИ ДЛЯ ВСЕХ ----------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard_message(update, context):
        return
    text = "Меню бота: выбери раздел."
    if is_admin(update.effective_user.id):
        text += "\n\n⚙️ Админу: для редактирования используй /manage."
    await update.message.reply_text(text, reply_markup=public_sections_keyboard())

async def public_view_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    uid = q.from_user.id
    if not await guard_callback(q, uid):
        return
    data = q.data or ""
    await q.answer()

    if data == "vback":
        await q.edit_message_text("Меню бота: выбери раздел.", reply_markup=public_sections_keyboard()); return
    if data.startswith("vsec:"):
        sec_id = data.split(":")[1]
        title = DATA["titles"].get(sec_id, f"Раздел {sec_id}")
        text = DATA["texts"].get(sec_id, "") or "—"
        await q.edit_message_text(f"*{title}*\n\n{text}", parse_mode="Markdown",
                                  reply_markup=public_subs_keyboard(sec_id)); return
    if data.startswith("vsub:"):
        _, sec_id, sub_id = data.split(":")
        item = DATA["subsections"].get(sec_id, {}).get(sub_id)
        if not item:
            await q.answer("Подраздел не найден", show_alert=True); return
        back = InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Назад к разделу", callback_data=f"vsec:{sec_id}")]])
        await q.edit_message_text(f"*{item['title']}*\n\n{item.get('text','—')}", parse_mode="Markdown", reply_markup=back); return

# ---------- АДМИН-РЕЖИМ (/manage) ----------
async def manage(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("Доступ только для администратора.")
        return ConversationHandler.END
    # Админ ночью допускается, если ADMIN_BYPASS = true
    if not is_work_time() and not ADMIN_BYPASS:
        await update.message.reply_text(OFF_MSG)
        return ConversationHandler.END
    await update.message.reply_text("Выбери раздел для управления:", reply_markup=sections_keyboard())
    return CHOOSING_SECTION

async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    uid = q.from_user.id
    # Разрешаем админу ночью (если ADMIN_BYPASS=1)
    if not (is_work_time() or (ADMIN_BYPASS and is_admin(uid))):
        await q.answer(OFF_MSG, show_alert=True)
        return ConversationHandler.END

    data = q.data or ""
    await q.answer()

    if data == "cancel":
        await q.edit_message_text("Вы вышли из режима управления."); return ConversationHandler.END
    if data == "noop": return
    if data == "back:sections":
        await q.edit_message_text("Выбери раздел:", reply_markup=sections_keyboard()); return CHOOSING_SECTION

    if data.startswith("sec:"):
        sec_id = data.split(":")[1]; context.user_data["sec_id"] = sec_id
        title = DATA["titles"].get(sec_id, f"Раздел {sec_id}")
        text = DATA["texts"].get(sec_id, "") or "Текст пуст"
        await q.edit_message_text(f"**{title}**\n\n{text}", parse_mode="Markdown",
                                  reply_markup=section_actions_keyboard(sec_id)); return SECTION_ACTION

    if data.startswith("act_menu:"):
        sec_id = data.split(":")[1]
        await q.edit_message_text("Выбери действие:", reply_markup=section_actions_keyboard(sec_id)); return SECTION_ACTION

    if data.startswith("act:set_text:"):
        sec_id = data.split(":")[2]; context.user_data["sec_id"] = sec_id
        await q.edit_message_text("Отправь новый текст для раздела. Для отмены — /cancel"); return EDIT_SECTION_TEXT

    if data.startswith("act:add_sub:"):
        sec_id = data.split(":")[2]; context.user_data["sec_id"] = sec_id
        await q.edit_message_text("Введи название нового подраздела:"); return ADD_SUB_TITLE

    if data.startswith("act:edit_sub:"):
        sec_id = data.split(":")[2]; context.user_data["sec_id"] = sec_id
        await q.edit_message_reply_markup(reply_markup=subs_keyboard(sec_id, "pick_edit")); return CHOOSE_SUB_FOR_EDIT

    if data.startswith("act:del_sub:"):
        sec_id = data.split(":")[2]; context.user_data["sec_id"] = sec_id
        await q.edit_message_reply_markup(reply_markup=subs_keyboard(sec_id, "pick_del")); return CHOOSE_SUB_FOR_DELETE

    if data.startswith("pick_edit:"):
        _, sec_id, sub_id = data.split(":")
        context.user_data["sec_id"] = sec_id; context.user_data["sub_id"] = sub_id
        sub = DATA["subsections"][sec_id][sub_id]
        await q.edit_message_text(f'Редактирование **{sub["title"]}**.\nОтправь новый текст (или /cancel).',
                                  parse_mode="Markdown"); return EDIT_SUB_TEXT

    if data.startswith("pick_del:"):
        _, sec_id, sub_id = data.split(":")
        context.user_data["sec_id"] = sec_id; context.user_data["sub_id"] = sub_id
        sub = DATA["subsections"].get(sec_id, {}).get(sub_id)
        if not sub:
            await q.edit_message_text("Подраздел не найден.", reply_markup=section_actions_keyboard(sec_id)); return SECTION_ACTION
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("✅ Удалить", callback_data="confirm_del"),
                                    InlineKeyboardButton("❌ Отмена", callback_data=f"act_menu:{sec_id}")]])
        await q.edit_message_text(f'Удалить подраздел **{sub["title"]}**?', parse_mode="Markdown", reply_markup=kb); return CONFIRM_DELETE_SUB

    if data == "confirm_del":
        sec_id = context.user_data.get("sec_id"); sub_id = context.user_data.get("sub_id")
        try:
            del DATA["subsections"][sec_id][sub_id]
            if not DATA["subsections"][sec_id]: del DATA["subsections"][sec_id]
            save_data(DATA)
            await q.edit_message_text("Подраздел удалён.", reply_markup=section_actions_keyboard(sec_id)); return SECTION_ACTION
        except KeyError:
            await q.edit_message_text("Не удалось удалить (уже отсутствует).", reply_markup=section_actions_keyboard(sec_id)); return SECTION_ACTION

async def set_section_text_msg(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return ConversationHandler.END
    if not is_work_time() and not ADMIN_BYPASS:
        await update.message.reply_text(OFF_MSG); return ConversationHandler.END
    sec_id = context.user_data.get("sec_id")
    DATA["texts"][sec_id] = update.message.text; save_data(DATA)
    await update.message.reply_text("Текст раздела обновлён.", reply_markup=section_actions_keyboard(sec_id)); return SECTION_ACTION

async def add_sub_title_msg(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return ConversationHandler.END
    if not is_work_time() and not ADMIN_BYPASS:
        await update.message.reply_text(OFF_MSG); return ConversationHandler.END
    context.user_data["new_sub_title"] = update.message.text.strip()
    await update.message.reply_text("Теперь отправь текст для этого подраздела:"); return ADD_SUB_TEXT

async def add_sub_text_msg(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return ConversationHandler.END
    if not is_work_time() and not ADMIN_BYPASS:
        await update.message.reply_text(OFF_MSG); return ConversationHandler.END
    sec_id = context.user_data.get("sec_id"); sub_id = next_sub_id(sec_id)
    DATA.setdefault("subsections", {}).setdefault(sec_id, {})
    DATA["subsections"][sec_id][sub_id] = {"title": context.user_data["new_sub_title"], "text": update.message.text}
    save_data(DATA)
    await update.message.reply_text(f'Подраздел добавлен: {sub_id}. {context.user_data["new_sub_title"]}',
                                    reply_markup=section_actions_keyboard(sec_id))
    context.user_data.pop("new_sub_title", None); return SECTION_ACTION

async def edit_sub_text_msg(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return ConversationHandler.END
    if not is_work_time() and not ADMIN_BYPASS:
        await update.message.reply_text(OFF_MSG); return ConversationHandler.END
    sec_id = context.user_data.get("sec_id"); sub_id = context.user_data.get("sub_id")
    DATA["subsections"][sec_id][sub_id]["text"] = update.message.text; save_data(DATA)
    await update.message.reply_text("Текст подраздела обновлён.", reply_markup=section_actions_keyboard(sec_id)); return SECTION_ACTION

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Отменено."); return ConversationHandler.END

# ---------- РЕГИСТРАЦИЯ И ЗАПУСК ----------
def main():
    # HTTP + keepalive в отдельных потоках
    threading.Thread(target=run_http, daemon=True).start()
    if KEEPALIVE_URL:
        threading.Thread(target=keepalive_loop, daemon=True).start()

    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

    # Публичные команды/кнопки
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("menu", start))
    app.add_handler(CallbackQueryHandler(public_view_cb, pattern="^v"))  # vsec/vsub/vback

    # Админ-диалог
    manage_conv = ConversationHandler(
        entry_points=[CommandHandler("manage", manage)],
        states={
            CHOOSING_SECTION: [CallbackQueryHandler(on_callback)],
            SECTION_ACTION: [CallbackQueryHandler(on_callback)],
            EDIT_SECTION_TEXT: [MessageHandler(filters.TEXT & ~filters.COMMAND, set_section_text_msg),
                                CallbackQueryHandler(on_callback)],
            ADD_SUB_TITLE: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_sub_title_msg),
                            CallbackQueryHandler(on_callback)],
            ADD_SUB_TEXT: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_sub_text_msg),
                           CallbackQueryHandler(on_callback)],
            CHOOSE_SUB_FOR_EDIT: [CallbackQueryHandler(on_callback)],
            EDIT_SUB_TEXT: [MessageHandler(filters.TEXT & ~filters.COMMAND, edit_sub_text_msg),
                            CallbackQueryHandler(on_callback)],
            CHOOSE_SUB_FOR_DELETE: [CallbackQueryHandler(on_callback)],
            CONFIRM_DELETE_SUB: [CallbackQueryHandler(on_callback)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        allow_reentry=True,
    )
    app.add_handler(manage_conv)

    log.info("Бот запускается… Рабочие часы %02d:00–%02d:00, TZ=%s (админ ночью: %s)",
             WORK_START, WORK_END, WORK_TZ, ADMIN_BYPASS)
    app.run_polling(close_loop=False)

if __name__ == "__main__":
    main()
