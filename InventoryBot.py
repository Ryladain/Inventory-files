import json
import random
import re
import asyncio
import unicodedata
import subprocess, datetime
import os
from dotenv import load_dotenv
from rapidfuzz import fuzz, process
from pathlib import Path
from telegram.ext import CallbackQueryHandler
from telegram import InlineKeyboardMarkup, InlineKeyboardButton
from telegram import ReplyKeyboardMarkup
from telegram.ext import ConversationHandler
from telegram import Update, constants, ReplyKeyboardMarkup, ReplyKeyboardRemove
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    ConversationHandler,
    ContextTypes,
    filters,
)

# === твоя библиотека описаний ===
from item_catalog import init_catalogs, enrich_item, render_item_card, MAGIC, NONMAGIC
# Глобальные хранилища библиотек
LIBRARY = []
NONMAGIC = []

load_dotenv(dotenv_path=Path(__file__).with_name('.env'), override=True)
TOKEN = os.getenv("BOT_TOKEN")
DATA_FILE = Path("inventory_data.json")
DATA_DIR = (Path(__file__).parent / "data").resolve()

# --------- Таблицы и данные ---------
CATEGORIES_D20 = {
    1: "Одежда",
    range(2, 12): "Снаряжение",
    range(12, 14): "Наборы снаряжения",
    range(14, 16): "Инструменты",
    range(16, 18): "Доспехи",
    range(18, 20): "Оружие",
    20: "Магический предмет",
}

ITEMS = {
    "Одежда": [
        "комплект путешественника","комплект простолюдина",
        "комплект знатного","комплект мага",
    ],
    "Снаряжение": [
        "факел","верёвка (15 м)","рюкзак","бутылка воды","спальник",
        "фляга","мешочек","фляга масла","зеркальце",
    ],
    "Наборы снаряжения": [
        "набор путешественника","набор священника",
        "набор вора","набор исследователя подземелий",
    ],
    "Инструменты": [
        "инструменты кузнеца","инструменты вора",
        "инструменты художника","музыкальный инструмент (лютня)",
    ],
    "Доспехи": [
        "кожаный доспех","кольчужная рубаха","латы","щит",
    ],
    "Оружие": [
        "кинжал","короткий меч","длинный меч","лук","топор","посох",
    ],
    "Магический предмет": [
        "зелье лечения","меч +1","кольцо защиты",
        "плащ защиты","жезл молний","мешок хранения",
    ],
}

RARITY_TABLE = [
    (30, "обычный"),
    (66, "необычный"),
    (81, "редкий"),
    (96, "значимый необычный"),
    (98, "очень редкий"),
    (100, "значимый редкий"),
]

STATE_REMOVE = 1
STATE_ADD_CATEGORY = 10
STATE_ADD_NAME = 11
STATE_ADD_CONFIRM = 12
STATE_REMOVE_CATEGORY = 20
STATE_SIMULATE_DAYS = 30
STATE_INVENTORY_CATEGORY = 40
STATE_INVENTORY_PAGE = 41
STATE_INVENTORY_ITEM = 42


# --------- Хранилище инвентаря ---------
def _load_all():
    if DATA_FILE.exists():
        return json.loads(DATA_FILE.read_text(encoding="utf-8"))
    return {}

def _save_all(data):
    DATA_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

def get_inventory(user_id: int):
    data = _load_all()
    inv = data.get(str(user_id), {})
    for cat in ITEMS.keys():
        inv.setdefault(cat, [])
    return inv

def save_inventory(user_id: int, inv: dict):
    data = _load_all()
    data[str(user_id)] = inv
    _save_all(data)

# --------- Механика выпадения ---------
def _choose_category_by_d20(roll: int) -> str:
    for k in CATEGORIES_D20:
        if isinstance(k, range) and roll in k:
            return CATEGORIES_D20[k]
        if k == roll:
            return CATEGORIES_D20[k]
    return "Снаряжение"

def _random_item(category: str) -> str:
    return random.choice(ITEMS[category])

def _magic_rarity():
    r = random.randint(1, 100)
    for threshold, rarity in RARITY_TABLE:
        if r <= threshold:
            return rarity, r
    return "обычный", r  # на всякий случай

def _lose_item(inv: dict):
    # Крутим до попадания в категорию, где есть что терять
    while True:
        r = random.randint(1, 20)
        cat = _choose_category_by_d20(r)
        if inv[cat]:
            lost = random.choice(inv[cat])
            inv[cat].remove(lost)
            return cat, lost, r

def _find_item(inv: dict):
    r = random.randint(1, 20)
    cat = _choose_category_by_d20(r)
    found = _random_item(cat)

    # Если это магический предмет
    if cat == "Магический предмет":
        rarity_label, r100 = _magic_rarity()

        # Определяем базовую редкость и значимость
        if "значимый" in rarity_label.lower():
            base_rarity = "Необычный" if "необыч" in rarity_label else "Редкий"
            tier = "Значительный"
        else:
            base_rarity = rarity_label.capitalize()
            tier = "Незначительный"

        # Берем список из библиотеки
        pool = [i for i in LIBRARY if i["rarity"] == base_rarity and i.get("tier") == tier]

        if pool:
            chosen = random.choice(pool)
            found = chosen["name"]

            # enrich_item для описания
            full_info = enrich_item({"name": found, "category": cat})
            desc = full_info.get("description", "")
            if desc:
                found += f" — {desc[:600].strip()}…"  # обрезаем до 200 символов
        else:
            found = f"Не найдено ({base_rarity}, {tier})"

        found = f"{found} ({rarity_label}, d100={r100})"

    inv[cat].append(found)
    return cat, found, r


# --------- Отчёт карточкой ---------
async def send_day_report(update: Update, day_number: int, lost_obj: dict | None, found_obj: dict | None):
    """
    lost_obj / found_obj: {'name': '...', 'category': '...'}
    enrich_item/render_item_card тянут описание/статы из JSON.
    """
    lost_full  = enrich_item(lost_obj)  if lost_obj  else None
    found_full = enrich_item(found_obj) if found_obj else None

    parts = [
        f"*День {day_number}*",
        "— Потеря:",
        render_item_card(lost_full) if lost_full else "_ничего подходящего_",
        "",
        "— Находка:",
        render_item_card(found_full) if found_full else "_ничего не найдено_",
    ]
    await update.message.reply_text(
        "\n".join(parts),
        parse_mode=constants.ParseMode.MARKDOWN,
        disable_web_page_preview=True,
    )
    
# --------- Обработчики команд (async) ---------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Привет! 🧙‍♂️ Я D&D инвентарь-бот.\n\n"
        "Команды:\n"
        "/inventory — показать инвентарь\n"
        "/add <категория> <название> — добавить предмет\n"
        "/remove — удалить предмет (по номеру из списка)\n"
        "/simulate <число> — симулировать дни\n"
        "/categories — показать категории\n"
        "/help — справка"
    )

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("/inventory, /add, /remove, /simulate, /categories")

async def categories(update: Update, context: ContextTypes.DEFAULT_TYPE):
    order = ["Одежда","Снаряжение","Наборы снаряжения","Инструменты","Доспехи","Оружие","Магический предмет"]
    await update.message.reply_text("📚 Категории:\n" + "\n".join(f"• {c}" for c in order))

import re
from telegram import constants

def escape_md(text: str) -> str:
    """Экранирует спецсимволы MarkdownV2 и чистит лишнее."""
    if not text:
        return ""
    return re.sub(r'([_*[\]()~`>#+\-=|{}.!])', r'\\\1', text)

def escape_md(text: str) -> str:
    """
    Экранирует все опасные символы для MarkdownV2, включая точки, скобки и т.п.
    """
    if not text:
        return ""
    # экранируем всё, что Telegram считает спецсимволами
    return re.sub(r'([_*\[\]()~`>#+\-=|{}.!\\])', r'\\\1', text)


import html

async def show_inventory(update, context):
    uid = update.effective_user.id
    inv = get_inventory(uid)

    def esc(s):
        return html.escape(str(s)) if s else ""

    text = ["<b>🎒 Инвентарь:</b>"]
    for cat, lst in inv.items():
        text.append(f"<b>{esc(cat)}:</b>")
        if lst:
            for i, item in enumerate(lst, 1):
                full = enrich_item({"name": item, "category": cat}) or {"name": item}
                name = esc(full.get("name", item))
                desc = esc(full.get("description", "")).strip()

                if len(desc) > 1000:
                    desc = desc[:1000] + "…"

                text.append(f"{i}. {name}")
                if desc:
                    text.append(f"<i>{desc}</i>")
        else:
            text.append("<i>пусто</i>")

    joined = "\n".join(text)
    chunks = [joined[i:i+3900] for i in range(0, len(joined), 3900)]

    for chunk in chunks:
        await update.message.reply_text(
            chunk,
            parse_mode=constants.ParseMode.HTML,
            disable_web_page_preview=True
        )
    await update.message.reply_text(
            "Инвентарь обновлён!",
            reply_markup=default_keyboard()
            )


async def add_item(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    inv = get_inventory(uid)
    if len(context.args) < 2:
        await update.message.reply_text("Используй: /add <категория> <название>")
        return
    cat = context.args[0].strip().capitalize()
    name = " ".join(context.args[1:]).strip()
    if cat not in ITEMS:
        await update.message.reply_text("❌ Такой категории нет. См. /categories")
        return
    inv[cat].append(name)
    save_inventory(uid, inv)
    await update.message.reply_text(f"✅ Добавлено: [{cat}] {name}")

async def remove_item(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Выбери категорию для удаления:", reply_markup=get_category_keyboard())
    return STATE_REMOVE_CATEGORY

async def show_remove_page(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cat = update.message.text.strip()
    valid_cats = [
        "Одежда", "Снаряжение", "Наборы снаряжения",
        "Инструменты", "Доспехи", "Оружие", "Магический предмет"
    ]

    # ✅ Исправлено: теперь работает и с emoji, и без
    if "назад" in cat.lower():
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text="↩️ Возврат в главное меню.",
            reply_markup=default_keyboard()
        )
        return ConversationHandler.END

    if cat.capitalize() not in valid_cats:
        await update.message.reply_text("❌ Такой категории нет. Попробуй ещё раз.")
        return STATE_REMOVE_CATEGORY

    uid = update.effective_user.id
    inv = get_inventory(uid)
    items = inv.get(cat.capitalize(), [])

    if not items:
        await update.message.reply_text(f"📭 В категории {cat} ничего нет. Выбери другую категорию:", 
                                    reply_markup=get_category_keyboard())
        return STATE_REMOVE_CATEGORY


    # Сохраняем контекст
    context.user_data["remove_cat"] = cat.capitalize()
    context.user_data["page"] = 0
    context.user_data["items"] = items

    await send_remove_page(update, context)

async def send_main_menu(update: Update, text: str = "Главное меню:"):
    """Возвращает пользователя в главное меню."""
    keyboard = [
        ["➕ Добавить предмет", "➖ Удалить предмет"],
        ["📦 Инвентарь", "🎲 Симулировать день"],
        ["📚 Категории"]
    ]
    markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

    # Проверяем, откуда пришёл апдейт — из сообщения или callback
    if update.message:
        await update.message.reply_text(text, reply_markup=markup)
    elif update.callback_query:
        await update.callback_query.message.reply_text(text, reply_markup=markup)



async def send_remove_page(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cat = context.user_data["remove_cat"]
    page = context.user_data.get("page", 0)
    items = context.user_data["items"]

    per_page = 10
    start = page * per_page
    end = start + per_page
    page_items = items[start:end]

    buttons = []
    for i, item in enumerate(page_items, start=start + 1):
        buttons.append([InlineKeyboardButton(f"{i}. {item[:35]}", callback_data=f"rm_{i-1}")])

    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("⬅️", callback_data="pg_prev"))
    if end < len(items):
        nav.append(InlineKeyboardButton("➡️", callback_data="pg_next"))
    buttons.append(nav)


    markup = InlineKeyboardMarkup(buttons)
    text = f"🗑️ *{cat}* — страница {page+1}/{(len(items)-1)//per_page+1}\nВыбери предмет для удаления:"
    if update.message:
        await update.message.reply_text(text, reply_markup=markup, parse_mode="Markdown")
    else:
        await update.callback_query.edit_message_text(text, reply_markup=markup, parse_mode="Markdown")

async def on_remove_nav(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if q.data == "pg_prev":
        context.user_data["page"] -= 1
    elif q.data == "pg_next":
        context.user_data["page"] += 1
    elif q.data == "pg_exit":
        await q.edit_message_text("↩️ Возврат в главное меню.")
        await context.bot.send_message(
            chat_id=q.message.chat_id,
            text="Главное меню:",
            reply_markup=default_keyboard()
        )
        return


    await send_remove_page(update, context)

async def on_remove_click(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()

    cat = context.user_data["remove_cat"]
    items = context.user_data["items"]
    idx = int(q.data.replace("rm_", ""))
    if idx < 0 or idx >= len(items):
        await q.answer("Ошибка!")
        return

    uid = update.effective_user.id
    inv = get_inventory(uid)
    item = items[idx]
    inv[cat].remove(item)
    save_inventory(uid, inv)

    await q.edit_message_text(f"❌ Удалено: [{cat}] {item}")
    await asyncio.sleep(1.5)
    await send_remove_page(update, context)


async def on_remove_choice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == "del_more":
        # просто повторяем список
        return await remove_item(update, context)
    else:
        await query.edit_message_text("✅ Возврат в главное меню.")


async def on_remove_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("❎ Отменено.")
    return ConversationHandler.END

async def simulate_days(update, context):
    uid = update.effective_user.id
    inv = get_inventory(uid)
    if not context.args:
        await update.message.reply_text("Используй: /simulate <число>")
        return
    days = max(1, int(context.args[0]))
    out = []
    for d in range(1, days + 1):
        lost_cat, lost_item, r1 = _lose_item(inv)
        found_cat, found_item, r2 = _find_item(inv)

        # enrich_item для описания
        lost_full = enrich_item({"name": lost_item, "category": lost_cat})
        found_full = enrich_item({"name": found_item, "category": found_cat})

        # красиво печатаем
        out.append(
            f"\n📅 *День {d}:*\n"
            f"  Потерял ({r1}) [{lost_cat}] — {lost_full['name']}\n"
            f"  {lost_full.get('description', '')}\n"
            f"  Нашёл  ({r2}) [{found_cat}] — {found_full['name']}\n"
            f"  {found_full.get('description', '')}"
        )

    save_inventory(uid, inv)
    await update.message.reply_text("\n".join(out), parse_mode=constants.ParseMode.MARKDOWN)
    for d in out:
        if "Магический предмет" in d and "—" in d:
            desc = d.split("—", 1)[-1].strip()
            if len(desc) > 600:
                await update.message.reply_text(desc, parse_mode=constants.ParseMode.MARKDOWN)
        # После симуляции возвращаем основное меню
    await update.message.reply_text(
        "🏁 Симуляция завершена! Что делаем дальше?",
        reply_markup=default_keyboard()
    )

    

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    reply_markup = default_keyboard()
    await update.message.reply_text(
        "Привет! 🧙‍♂️ Я D&D инвентарь-бот.\n"
        "Выбери действие из меню ниже:",
        reply_markup=reply_markup
    )

from telegram import ReplyKeyboardMarkup

def default_keyboard():
    return ReplyKeyboardMarkup(
        [
            ["➕ Добавить предмет", "➖ Удалить предмет"],
            ["📦 Инвентарь", "🎲 Симулировать день"],
            ["📚 Категории"]
        ],
        resize_keyboard=True,
        one_time_keyboard=False
    )

# --- Добавление предмета через кнопки ---
STATE_ADD_CATEGORY = 10
STATE_ADD_NAME = 11

def normalize_text(s: str) -> str:
    return (s or "").strip().lower()

def find_closest_item(name: str, category: str | None = None):
    """
    Ищет ближайшее совпадение по названию:
    - если категория содержит 'маг' → MAGIC
    - иначе → NONMAGIC
    Использует RapidFuzz для нечёткого поиска.
    """
    query = normalize_text(name)

    if category and "маг" in category.lower():
        search_space = MAGIC
        print(f"🔮 Ищу магический предмет: {query}")
    else:
        search_space = NONMAGIC
        print(f"⚙️ Ищу немагический предмет: {query}")

    # Список всех имён
    names = [normalize_text(i.get("name")) for i in search_space if i.get("name")]

    # RapidFuzz сам находит ближайшее совпадение
    best = process.extractOne(query, names, scorer=fuzz.WRatio)
    if not best:
        print("⚠️ Совпадений не найдено")
        return None

    best_name, score, _ = best
    print(f"➡ Лучшее совпадение: {best_name} ({score/100:.2f})")

    # если похожесть < 60%, считаем, что не найдено
    if score < 60:
        return None

    # возвращаем исходный объект из библиотеки
    for it in search_space:
        if normalize_text(it.get("name")) == best_name:
            return it

    return None

async def confirm_item_choice(update, context):
    uid = update.effective_user.id
    choice = update.message.text.strip()
    cat = context.user_data.get("add_cat")

    if choice.startswith("✅"):
        found = context.user_data.get("pending_item")
    else:
        found = {"name": context.user_data.get("pending_name"), "description": "— пользовательское описание —"}

    return await finalize_add(update, context, found, cat)

async def finalize_add(update, context, found, cat):
    uid = update.effective_user.id
    inv = get_inventory(uid)

    inv[cat].append(found["name"])
    save_inventory(uid, inv)

    card = render_item_card(found)
    keyboard = [
        ["📦 Инвентарь", "➕ Добавить предмет"],
        ["🗑 Удалить предмет", "📜 Категории"],
        ["🎲 Симулировать день", "❓ Помощь"]
    ]
    markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

    await update.message.reply_text(
        f"✅ Добавлено в [{cat}]:\n\n{card}",
        reply_markup=markup,
        parse_mode=constants.ParseMode.MARKDOWN,
        disable_web_page_preview=True
    )
    return ConversationHandler.END


async def add_item_start(update, context):
    keyboard = [
        ["Одежда", "Снаряжение"],
        ["Наборы снаряжения", "Инструменты"],
        ["Доспехи", "Оружие"],
        ["Магический предмет"],
        ["🔙 Назад"]
    ]
    reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
    await update.message.reply_text("Выбери категорию:", reply_markup=reply_markup)
    return STATE_ADD_CATEGORY


async def add_item_category(update, context):
    cat = update.message.text.strip()

    # 🔙 Если выбрали "Назад" — возвращаем в главное меню
    if cat.lower() == "назад" or cat == "🔙 Назад":
        await update.message.reply_text(
            "↩️ Возврат в главное меню.",
            reply_markup=default_keyboard()
        )
        return ConversationHandler.END

    # 🧾 Проверяем категорию
    if cat not in ITEMS:
        await update.message.reply_text(
            "❌ Такой категории нет. Попробуй ещё раз.",
            reply_markup=get_category_keyboard()
        )
        return STATE_ADD_CATEGORY

    # ✅ Если всё ок — сохраняем и просим название
    context.user_data["add_cat"] = cat
    await update.message.reply_text(
        f"Введи название предмета для категории [{cat}]:\n"
        f"Можно добавить описание через двоеточие, например:\n"
        f"`Языки пламени: меч с огненным клинком`",
        parse_mode=constants.ParseMode.MARKDOWN
    )
    return STATE_ADD_NAME


    context.user_data["add_cat"] = cat
    await update.message.reply_text(
        f"Введи название предмета для категории [{cat}]:\n"
        f"Можно добавить описание через двоеточие, например:\n"
        f"`Языки пламени: меч с огненным клинком`",
        parse_mode=constants.ParseMode.MARKDOWN
    )
    return STATE_ADD_NAME


async def add_item_name(update, context):
    uid = update.effective_user.id
    inv = get_inventory(uid)
    cat = context.user_data.get("add_cat")

    # Получаем ввод пользователя
    raw_text = update.message.text.strip()
    context.user_data["raw_name"] = raw_text
    if ":" in raw_text:
        name, desc = [x.strip() for x in raw_text.split(":", 1)]
    else:
        name, desc = raw_text, None

    # Пробуем найти в библиотеке
    found = enrich_item({"name": name, "category": cat})
    closest = None
    if not found or found.get("name") == name:
        closest = find_closest_item(name, cat)
        if closest:
            found_name = closest["name"]  # ← вот тут был недостающий отступ!

            context.user_data["pending_item"] = (cat, found_name)
            context.user_data["raw_name"] = name

            found_item = enrich_item({"name": found_name, "category": cat})
            desc = found_item.get("description") or found_item.get("desc") or "— нет описания —"
            short_desc = re.sub(r"\s+", " ", desc.strip())
            if len(short_desc) > 350:
                short_desc = short_desc[:350].rstrip() + "…"

            keyboard = InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("✅ Да", callback_data="confirm_yes"),
                    InlineKeyboardButton("❌ Нет", callback_data="confirm_no"),
                ]
            ])

            await update.message.reply_text(
                f"🤔 Похоже, вы имели в виду *{found_name}*?\n\n"
                f"{short_desc}",
                parse_mode=constants.ParseMode.MARKDOWN,
                disable_web_page_preview=True,
                reply_markup=keyboard,
            )
            return ConversationHandler.END



    # Если есть близкое совпадение
    if closest:
        found = closest
        msg = f"🔎 Нашёл похожий предмет: *{found['name']}*"
    elif not found or found.get("description") in ("", None):
        msg = f"⚠️ Не найдено в библиотеке. Добавлен как пользовательский предмет."
        found = {"name": name, "description": desc or "— пользовательское описание —"}
    else:
        msg = f"✅ Найден предмет: *{found['name']}*"

    # Добавляем в инвентарь
    item_entry = f"{found['name']}"
    if desc:
        item_entry += f" — {desc}"
    inv[cat].append(item_entry)
    save_inventory(uid, inv)

    # Готовим карточку
    card = render_item_card(found)
    keyboard = [
        ["📦 Инвентарь", "➕ Добавить предмет"],
        ["🗑 Удалить предмет", "📜 Категории"],
        ["🎲 Симулировать день", "❓ Помощь"]
    ]
    reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

    await update.message.reply_text(
        f"{msg}\n\nДобавлено в [{cat}]:\n\n{card}",
        reply_markup=reply_markup,
        parse_mode=constants.ParseMode.MARKDOWN,
        disable_web_page_preview=True
    )
    return ConversationHandler.END

async def add_item_cancel(update, context):
    await update.message.reply_text("❎ Добавление отменено.", reply_markup=ReplyKeyboardRemove())
    return ConversationHandler.END

async def on_add_confirm_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    uid = update.effective_user.id
    inv = get_inventory(uid)
    data = query.data
    cat, found_name = context.user_data.get("pending_item", (None, None))

    # ✅ Пользователь подтвердил предмет из библиотеки
    if data == "confirm_yes" and found_name:
        found_item = enrich_item({"name": found_name, "category": cat})
        desc = found_item.get("description") or found_item.get("desc") or "— нет описания —"

        inv[cat].append(found_name)
        save_inventory(uid, inv)

        await query.edit_message_text(
            f"✅ Добавлено в {cat}:\n\n"
            f"*{found_name}*\n\n"
            f"{desc}",
            parse_mode=constants.ParseMode.MARKDOWN,
            disable_web_page_preview=True
        )

    # ❌ Пользователь сказал "Нет" — спрашиваем, добавить ли кастомный
    elif data == "confirm_no":
        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("✅ Да", callback_data="add_custom_yes"),
                InlineKeyboardButton("❌ Нет", callback_data="add_custom_no"),
            ]
        ])
        await query.edit_message_text(
            "⚙️ Не найдено в библиотеке.\nДобавить как пользовательский предмет?",
            reply_markup=keyboard
        )

    # ✅ Пользователь согласился добавить кастом
    elif data == "add_custom_yes":
        name = context.user_data.get("raw_name", "Неизвестный предмет")
        cat = context.user_data.get("add_cat", "Снаряжение")

        inv[cat].append(f"⭐ {name}")  # ⭐ пометка, что это пользовательский
        save_inventory(uid, inv)

        await query.edit_message_text(
            f"⚠️ Не найдено в библиотеке. Добавлен как пользовательский предмет.\n\n"
            f"Добавлено в {cat}:\n\n"
            f"*{name}*\n\n— пользовательское описание —",
            parse_mode=constants.ParseMode.MARKDOWN
        )

    # 🚫 Отменяем полностью
    # 🚫 Отменяем полностью
    # 🚫 Отменяем полностью
    elif data == "add_custom_no":
        await query.edit_message_text("🚫 Добавление отменено.")

        # Возврат в главное меню через bot.send_message
        await context.bot.send_message(
            chat_id=query.message.chat_id,
            text="↩️ Возврат в главное меню.",
            reply_markup=default_keyboard()
        )


def get_category_keyboard():
    cats = ["Одежда", "Снаряжение", "Наборы снаряжения",
            "Инструменты", "Доспехи", "Оружие", "Магический предмет"]
    rows = [[c] for c in cats]
    rows.append(["🔙 Назад"])
    return ReplyKeyboardMarkup(rows, resize_keyboard=True)

async def ask_simulation_days(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показывает выбор количества дней."""
    keyboard = [
        ["1", "3", "5"],
        ["7", "10", "📝 Другое"],
        ["🔙 Назад"]
    ]
    markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
    await update.message.reply_text(
        "⏳ На сколько дней симулировать приключение?",
        reply_markup=markup
    )
    return STATE_SIMULATE_DAYS


async def handle_simulation_days(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обрабатывает выбор или ввод количества дней."""
    text = update.message.text.strip()
    if text == "🔙 Назад":
        await send_main_menu(update, "↩️ Возврат в главное меню.")
        return ConversationHandler.END

    if text == "📝 Другое":
        await update.message.reply_text("Введите количество дней числом (например: 12):")
        return STATE_SIMULATE_DAYS

    # Если пользователь ввёл число
    try:
        days = int(text)
        context.args = [str(days)]  # чтобы использовать существующую simulate_days
        await simulate_days(update, context)
        return ConversationHandler.END
    except ValueError:
        await update.message.reply_text("⚠️ Введите число, пожалуйста.")
        return STATE_SIMULATE_DAYS
    
async def show_inventory_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Меню выбора категории для просмотра."""
    keyboard = [
        ["⚔ Оружие", "🛡 Доспехи"],
        ["🧳 Снаряжение", "🧰 Инструменты"],
        ["📚 Наборы снаряжения", "👕 Одежда"],
        ["✨ Магический предмет"],
        ["📜 Весь инвентарь", "🔙 Назад"]
    ]
    markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
    await update.message.reply_text("📦 Выбери категорию для просмотра:", reply_markup=markup)
    return STATE_INVENTORY_CATEGORY

async def show_inventory_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cat = update.message.text.strip()

    # --- Возврат в меню ---
    if cat == "🔙 Назад":
        await send_main_menu(update, "↩️ Возврат в главное меню.")
        return ConversationHandler.END

    uid = update.effective_user.id
    inv = get_inventory(uid)

    # --- Весь инвентарь ---
    if "Весь инвентарь" in cat:
        all_items = [f"[{c}] {i}" for c, lst in inv.items() for i in lst if lst]
        if not all_items:
            await update.message.reply_text("📭 Инвентарь пуст.")
            return STATE_INVENTORY_CATEGORY
        text = "🧾 Весь инвентарь:\n\n" + "\n".join(all_items)
        await update.message.reply_text(text)
        return STATE_INVENTORY_CATEGORY

    # --- Очистка эмодзи из названия категории ---
    cat_clean = cat
    for prefix in ["⚔ ", "🛡 ", "🧳 ", "🧰 ", "📚 ", "👕 ", "✨ ", "📜 "]:
        cat_clean = cat_clean.replace(prefix, "")
    cat_clean = cat_clean.strip()

    items = inv.get(cat_clean, [])

    # --- Если пусто ---
    if not items:
        await update.message.reply_text(f"📭 В категории {cat_clean} нет предметов.")
        return STATE_INVENTORY_CATEGORY

    # --- Сохраняем контекст и отправляем страницу ---
    context.user_data["inv_cat"] = cat_clean
    context.user_data["inv_page"] = 0
    context.user_data["inv_items"] = items

    await send_inventory_page(update, context)


async def send_inventory_page(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cat = context.user_data["inv_cat"]
    page = context.user_data.get("inv_page", 0)
    items = context.user_data["inv_items"]

    per_page = 10
    start = page * per_page
    end = start + per_page
    page_items = items[start:end]

    buttons = []
    for i, item in enumerate(page_items, start=start + 1):
        buttons.append([InlineKeyboardButton(f"{i}. {item[:40]}", callback_data=f"inv_{i-1}")])

    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("⬅️", callback_data="inv_prev"))
    if end < len(items):
        nav.append(InlineKeyboardButton("➡️", callback_data="inv_next"))

    buttons.append(nav)

    markup = InlineKeyboardMarkup(buttons)
    text = f"{cat} — страница {page+1}/{(len(items)-1)//per_page+1}\nВыбери предмет для просмотра:"
    if update.message:
        await update.message.reply_text(text, reply_markup=markup)
    else:
        await update.callback_query.edit_message_text(text, reply_markup=markup)

async def on_inventory_nav(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if q.data == "inv_prev":
        context.user_data["inv_page"] -= 1
    elif q.data == "inv_next":
        context.user_data["inv_page"] += 1
    elif q.data == "inv_exit":
        await show_inventory_menu(update, context)
        return
    await send_inventory_page(update, context)

async def on_inventory_item(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()

    items = context.user_data["inv_items"]
    idx = int(q.data.replace("inv_", ""))
    if idx < 0 or idx >= len(items):
        await q.answer("Ошибка!")
        return

    item_name = items[idx]
    cat = context.user_data["inv_cat"].replace("⚔ ", "").replace("🛡 ", "")
    full = enrich_item({"name": item_name, "category": cat}) or {"name": item_name}
    card = render_item_card(full)

    await q.message.reply_text(
        card,
        parse_mode=constants.ParseMode.MARKDOWN,
        disable_web_page_preview=True
    )

async def backup_inventory_to_github():
    """Коммитит inventory_data.json в репозиторий"""
    ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        subprocess.run(["git", "config", "--global", "user.email", os.getenv("GITHUB_EMAIL")], check=True)
        subprocess.run(["git", "config", "--global", "user.name", os.getenv("GITHUB_NAME")], check=True)
        subprocess.run(["git", "add", "inventory_data.json"], check=True)
        subprocess.run(["git", "commit", "-m", f"auto backup {ts}"], check=False)
        subprocess.run(["git", "push", f"https://{os.getenv('GITHUB_TOKEN')}@github.com/{os.getenv('GITHUB_REPO')}.git", "HEAD:main"], check=False)
        print(f"✅ GitHub backup done at {ts}")
    except Exception as e:
        print(f"⚠️ Backup error: {e}")


    
# --------- Запуск ---------
async def run_bot():
    # Подтянуть каталоги описаний (оружие/доспехи/магия) перед запуском бота
    global MAGIC, NONMAGIC
    MAGIC, NONMAGIC = init_catalogs(str(DATA_DIR))



    # Fix для Python 3.14: нет текущего event loop

    app = ApplicationBuilder().token(TOKEN).build()

    remove_conv = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex("^➖ Удалить предмет$"), remove_item)],
        states={
            STATE_REMOVE_CATEGORY: [MessageHandler(filters.TEXT & ~filters.COMMAND, show_remove_page)],
        },
        fallbacks=[],
    )

    inventory_conv = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex("^📦 Инвентарь$"), show_inventory_menu)],
        states={
            STATE_INVENTORY_CATEGORY: [MessageHandler(filters.TEXT & ~filters.COMMAND, show_inventory_list)],
        },
        fallbacks=[],
    )
    app.add_handler(inventory_conv)

    app.add_handler(CallbackQueryHandler(on_inventory_nav, pattern="^inv_(prev|next|exit)$"))
    app.add_handler(CallbackQueryHandler(on_inventory_item, pattern="^inv_[0-9]+$"))


    simulate_conv = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex("^🎲 Симулировать день$"), ask_simulation_days)],
        states={
            STATE_SIMULATE_DAYS: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_simulation_days)],
        },
        fallbacks=[CommandHandler("cancel", on_remove_cancel)],
    )
    app.add_handler(simulate_conv)


    add_conv = ConversationHandler(
    entry_points=[MessageHandler(filters.Regex("^➕ Добавить предмет$"), add_item_start)],
    states={
        STATE_ADD_CATEGORY: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_item_category)],
        STATE_ADD_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_item_name)],
    },
    fallbacks=[CommandHandler("cancel", on_remove_cancel)],
    )

    app.add_handler(CallbackQueryHandler(on_add_confirm_button, pattern="^(confirm_|add_custom_)"))
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.Regex("^📦 Инвентарь$"), show_inventory_menu))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("categories", categories))
    app.add_handler(CommandHandler("inventory", show_inventory))
    app.add_handler(remove_conv)
    app.add_handler(CallbackQueryHandler(on_remove_click, pattern="^rm_"))
    app.add_handler(CallbackQueryHandler(on_remove_nav, pattern="^pg_"))
    app.add_handler(add_conv)
    app.add_handler(CommandHandler("remove", remove_item))
    app.add_handler(CommandHandler("simulate", simulate_days))
    app.add_handler(MessageHandler(filters.Regex("^📦 Инвентарь$"), show_inventory))
    app.add_handler(MessageHandler(filters.Regex("^➖ Удалить предмет$"), remove_item))
    app.add_handler(MessageHandler(filters.Regex("^🎲 Симулировать день$"), ask_simulation_days))
    app.add_handler(MessageHandler(filters.Regex("^📚 Категории$"), categories))
    app.add_handler(MessageHandler(filters.Regex("^❓ Помощь$"), help_cmd))
    app.add_handler(CallbackQueryHandler(on_add_confirm_button, pattern="^(confirm_|add_custom_)"))
    from apscheduler.schedulers.asyncio import AsyncIOScheduler

    scheduler = AsyncIOScheduler()
    scheduler.add_job(backup_inventory_to_github, "interval", hours=24)
    scheduler.start()

    print("✅ Бот запущен!")  
    await app.run_polling()

if __name__ == "__main__":
    import nest_asyncio
    import asyncio

    nest_asyncio.apply()  # исправляет конфликт циклов
    asyncio.run(run_bot())
